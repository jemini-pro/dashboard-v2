#!/usr/bin/env python3
"""Shared wire protocol and host collectors for the dashboard agents.

Stdlib only. Deploy publishes this file at /agents/ beside the agent scripts,
and every installer drops it next to the agent it installs, so a script and
its dependencies always live in one directory.

There are two seams:

  * ``push()`` sends through a transport object. Production uses
    ``UrllibTransport``; tests pass a fake that records the request.
  * ``collect_host()`` asks a platform adapter for readings. ``LinuxHost`` and
    ``DarwinHost`` implement the same small interface.

``EXTRA_METRICS`` is the canonical optional-signal vocabulary for the Python
agents. The server validates independently in ``server/app.py``; a test pins
the two lists to each other so they cannot drift.
"""
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.request

DEFAULT_TOKEN_PATH = "~/dash/.token"
AGENTS_PATH = "/agents/"

# The fixed heartbeats every Python agent sends. PowerShell mirrors these
# names but cannot import this file; test_agents.py checks its source.
CORE_METRICS = (
    "name", "kind", "cpu_pct", "mem_pct", "mem_used", "mem_total",
    "disk_pct", "disks", "uptime_s",
)
# Optional signals are omitted when the OS cannot provide them, so nothing
# renders as a fabricated zero. Order matches server/app.py:EXTRA_METRICS.
EXTRA_METRICS = (
    "load_1", "load_5", "load_15",
    "swap_used", "swap_total",
    "net_rx_bps", "net_tx_bps",
    "cpu_iowait", "cpu_steal",
)


# ---------- transport seam ----------

class UrllibTransport:
    """Production adapter: the only code here that touches the network."""

    def post(self, url, body, headers, timeout=15):
        request = urllib.request.Request(
            url, data=body, headers=headers, method="POST"
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode()

    def get(self, url, timeout=10):
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.read().decode()


TRANSPORT = UrllibTransport()


def push(name, payload, token, url, transport=None, timeout=15):
    """POST one device payload to /api/push/<name>; return (status, body)."""
    transport = transport or TRANSPORT
    body = json.dumps(payload).encode()
    return transport.post(
        f"{url.rstrip('/')}/api/push/{name}",
        body,
        {"Content-Type": "application/json", "X-Dash-Token": token},
        timeout,
    )


def update_installed(url, files, transport=None, timeout=10):
    """Replace installed agent files with the served copies when they differ.

    ``files`` is an iterable of ``(served_name, installed_path)``. Returns the
    served names that were replaced. Never raises: an unreachable server or a
    read-only install must not stop the heartbeat. ``DASH_AGENT_UPDATE=0``
    opts a development copy out entirely.
    """
    if os.environ.get("DASH_AGENT_UPDATE") == "0":
        return []
    transport = transport or TRANSPORT
    changed = []
    for served_name, path in files:
        if not path:
            continue
        try:
            served = transport.get(
                f"{url.rstrip('/')}{AGENTS_PATH}{served_name}", timeout
            )
            with open(path) as handle:
                current = handle.read()
        except Exception:  # noqa: BLE001 - offline is normal
            continue
        if not served.strip() or served.strip() == current.strip():
            continue
        tmp = path + ".new"
        try:
            with open(tmp, "w") as handle:
                handle.write(served)
            os.chmod(tmp, 0o755)
            os.replace(tmp, path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            continue
        changed.append(served_name)
    return changed


def installed_files(served_name, installed_path):
    """The served copies an agent and the shared module update together."""
    return [
        (served_name, os.path.abspath(installed_path or "")),
        ("dash_common.py", os.path.abspath(__file__)),
    ]


def restart(script, argv=None):
    """Exec the file on disk, which update_installed may just have replaced."""
    argv = sys.argv[1:] if argv is None else argv
    os.execv(sys.executable, [sys.executable, os.path.abspath(script)] + argv)


# ---------- token loading ----------

def load_token(path=DEFAULT_TOKEN_PATH, env=None):
    """DASH_TOKEN from the environment, else from ``path`` (mode 600).

    A file may hold the bare token or a ``DASH_TOKEN=`` line, so both
    ``~/dash/.token`` and the dashboard container's ``.env`` work.
    """
    environ = os.environ if env is None else env
    value = (environ.get("DASH_TOKEN") or "").strip()
    if value:
        return value
    try:
        with open(os.path.expanduser(path)) as handle:
            text = handle.read()
    except OSError:
        return ""
    for line in text.splitlines():
        key, sep, rest = line.partition("=")
        if sep and key.strip() == "DASH_TOKEN":
            return rest.strip().strip('"').strip("'")
    return text.strip() if "=" not in text else ""


# ---------- shared helpers ----------

def read_file(path):
    """The file's text, or '' when it is missing or unreadable."""
    try:
        with open(path) as handle:
            return handle.read()
    except OSError:
        return ""


def run(cmd, timeout=10):
    """Run a shell command and return stdout, or '' on any failure."""
    try:
        return subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        ).stdout
    except Exception:  # noqa: BLE001
        return ""


SIZE_MULT = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}


def _size_bytes(number, unit):
    return int(float(number) * SIZE_MULT[unit.upper()])


def meminfo():
    """Linux /proc/meminfo in bytes, keyed without the trailing colon."""
    info = {}
    for line in read_file("/proc/meminfo").splitlines():
        key, _, rest = line.partition(":")
        try:
            info[key] = int(rest.strip().split()[0]) * 1024
        except (ValueError, IndexError):
            continue
    return info


# Real local filesystems only: pseudo mounts (tmpfs, cgroup, snap squashfs),
# network shares and loop devices are not drives. The 1 GiB floor drops the
# EFI/boot partitions nobody opens in the dashboard, and the cap matches the
# server's per-heartbeat limit.
REAL_FS = {
    "ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "f2fs", "jfs", "reiserfs",
    "vfat", "exfat", "ntfs", "ntfs3", "fuseblk", "msdos",
    "hfs", "hfsplus", "apfs",
}
MIN_DISK_BYTES = 1 << 30
MAX_DISKS = 16


def mounts(system=None):
    """Yield (device, mountpoint, fstype) for every mounted filesystem."""
    system = system or platform.system()
    if system == "Linux":
        for line in read_file("/proc/mounts").splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            # The kernel escapes spaces, tabs and backslashes in \ooo.
            mount = (parts[1].replace("\\040", " ")
                     .replace("\\011", "\t").replace("\\134", "\\"))
            yield parts[0], mount, parts[2]
        return
    # macOS: "/dev/disk3s1s1 on / (apfs, sealed, local, read-only, journaled)"
    for line in run("mount").splitlines():
        # Split off the "(type, opts)" tail first: a mount point may itself
        # contain " on " or " (" (e.g. /Volumes/My (Backup) Drive).
        head, sep, tail = line.rpartition(" (")
        device, on, mount = head.partition(" on ")
        if not sep or not on:
            continue
        yield device, mount, tail.split(",", 1)[0].strip(" )")


def volume_labels():
    """Map a block device to its filesystem label, e.g. /dev/sdb1 -> External."""
    labels = {}
    try:
        names = os.listdir("/dev/disk/by-label")
    except OSError:
        return labels
    for name in names:
        try:
            labels[os.path.realpath(os.path.join("/dev/disk/by-label", name))] = \
                name.replace("\\x20", " ")
        except OSError:
            pass
    return labels


def volumes(system=None):
    """Every real local volume, root first.

    Pseudo filesystems, snap loops and duplicate bind mounts are skipped, and
    the same physical volume mounted twice is reported once.
    """
    labels = volume_labels()
    seen = set()
    found = []
    for device, mount, fstype in mounts(system):
        if fstype not in REAL_FS or device.startswith("/dev/loop"):
            continue
        # macOS mirrors the same APFS container under /System/Volumes/*; those
        # are implementation volumes, not drives a person recognises.
        if mount.startswith("/System/Volumes/"):
            continue
        try:
            st = os.stat(mount)
            usage = shutil.disk_usage(mount)
        except OSError:
            continue
        if st.st_dev in seen or usage.total < MIN_DISK_BYTES:
            continue
        label = labels.get(os.path.realpath(device)) if device.startswith("/dev/") else ""
        if not label:
            label = "System" if mount == "/" else (os.path.basename(mount.rstrip("/")) or mount)
        seen.add(st.st_dev)
        found.append({
            "mount": mount,
            "label": label[:32],
            "total": usage.total,
            "used": usage.used,
            "pct": round(100.0 * usage.used / usage.total, 1),
        })
    found.sort(key=lambda d: (d["mount"] != "/", d["mount"]))
    return found[:MAX_DISKS]


# ---------- platform seam ----------

class BaseHost:
    """Readings common to every Unix platform, plus the shared disk rules."""

    system = "Linux"

    def volumes(self):
        return volumes(self.system)

    def root_volume(self):
        """The host's root filesystem as a volume list.

        LXC guests have no per-volume data from pvesh, so only the host sends
        this. The server rejects a malformed entry rather than storing it.
        """
        try:
            usage = shutil.disk_usage("/")
        except OSError:
            return []
        if not usage.total:
            return []
        return [{
            "mount": "/",
            "label": "System",
            "total": usage.total,
            "used": usage.used,
            "pct": round(100.0 * usage.used / usage.total, 1),
        }]

    def load_average(self):
        """The 1/5/15-minute load averages, or (None, None, None)."""
        try:
            one, five, fifteen = os.getloadavg()
        except (OSError, AttributeError):
            return None, None, None
        return round(one, 2), round(five, 2), round(fifteen, 2)

    def net_rates(self):
        """Bytes per second over a one-second delta, or (None, None)."""
        rx1, tx1 = self.net_counters()
        at1 = time.time()
        if rx1 is None or tx1 is None:
            return None, None
        time.sleep(1)
        rx2, tx2 = self.net_counters()
        at2 = time.time()
        if (rx2 is None or tx2 is None or at2 <= at1
                or rx2 < rx1 or tx2 < tx1):
            return None, None
        elapsed = at2 - at1
        return round((rx2 - rx1) / elapsed), round((tx2 - tx1) / elapsed)


class LinuxHost(BaseHost):
    """Linux readings from /proc and /sys."""

    @staticmethod
    def _cpu_values():
        stat = read_file("/proc/stat").splitlines()
        if not stat:
            raise OSError("/proc/stat unavailable")
        return [float(x) for x in stat[0].split()[1:]]

    def cpu_pct(self):
        try:
            v1 = self._cpu_values()
            idle1 = v1[3] + (v1[4] if len(v1) > 4 else 0.0)
            total1 = sum(v1)
            time.sleep(1)
            v2 = self._cpu_values()
            idle2 = v2[3] + (v2[4] if len(v2) > 4 else 0.0)
            total2 = sum(v2)
        except (OSError, ValueError, IndexError):
            return None
        return round(100.0 * (1 - (idle2 - idle1) / (total2 - total1)), 1) \
            if total2 > total1 else None

    def mem(self):
        info = meminfo()
        total = info.get("MemTotal", 0)
        used = total - info.get("MemAvailable", info.get("MemFree", 0))
        return used, total

    def swap(self):
        """(used, total) swap bytes; (0, 0) when the machine has swap off.

        The server treats a zero total as absent rather than as a full disk.
        """
        info = meminfo()
        total = info.get("SwapTotal", 0)
        if not total:
            return 0, 0
        return max(0, total - info.get("SwapFree", 0)), total

    def net_counters(self):
        """(rx_bytes, tx_bytes) across real interfaces, or (None, None)."""
        try:
            with open("/proc/net/dev") as handle:
                rows = handle.readlines()[2:]
        except OSError:
            return None, None
        rx = tx = 0
        try:
            for line in rows:
                name, sep, rest = line.partition(":")
                if not sep or name.strip() == "lo":
                    continue
                cols = rest.split()
                if len(cols) < 9:
                    continue
                rx += int(cols[0])
                tx += int(cols[8])
        except ValueError:
            return None, None
        return rx, tx

    def cpu_extra(self):
        """(iowait %, steal %) over a one-second delta, or (None, None)."""
        try:
            v1 = self._cpu_values()
            i1 = v1[4] if len(v1) > 4 else 0.0
            s1 = v1[7] if len(v1) > 7 else 0.0
            t1 = sum(v1)
            time.sleep(1)
            v2 = self._cpu_values()
            i2 = v2[4] if len(v2) > 4 else 0.0
            s2 = v2[7] if len(v2) > 7 else 0.0
            t2 = sum(v2)
        except (OSError, ValueError, IndexError):
            return None, None
        if t2 <= t1 or i2 < i1 or s2 < s1:
            return None, None
        return (round(100.0 * (i2 - i1) / (t2 - t1), 1),
                round(100.0 * (s2 - s1) / (t2 - t1), 1))

    def uptime_s(self):
        try:
            return int(float(read_file("/proc/uptime").split()[0]))
        except (OSError, ValueError, IndexError):
            return None


class DarwinHost(BaseHost):
    """macOS readings from top, sysctl, vm_stat and netstat."""

    system = "Darwin"

    def cpu_pct(self):
        out = run("top -l 2 -n 0 | grep 'CPU usage' | tail -1")
        m = re.search(r"([\d.]+)%\s+idle", out)
        return round(100.0 - float(m.group(1)), 1) if m else None

    def mem(self):
        total = int(run("sysctl -n hw.memsize").strip() or 0)
        pages = {}
        for line in run("vm_stat").splitlines():
            m = re.match(r'"?([\w\s]+)"?:\s+(\d+)', line)
            if m:
                pages[m.group(1).strip()] = int(m.group(2))
        page = 4096
        used = (pages.get("Anonymous pages", 0)
                + pages.get("Pages wired down", 0)) * page
        return used, total

    def swap(self):
        """(used, total) swap bytes, or (None, None) when unavailable."""
        out = run("sysctl -n vm.swapusage")
        total = re.search(r"total = ([\d.]+)([KMGT])", out)
        used = re.search(r"used = ([\d.]+)([KMGT])", out)
        if not total or not used:
            return None, None
        return (_size_bytes(used.group(1), used.group(2)),
                _size_bytes(total.group(1), total.group(2)))

    def net_counters(self):
        """(rx_bytes, tx_bytes) across real interfaces, or (None, None)."""
        # macOS: Name Mtu Network Address Ipkts Ierrs Ibytes Opkts Oerrs Obytes
        # netstat prints one row per address, so count each interface once.
        rx = tx = 0
        seen = set()
        for line in run("netstat -ib").splitlines()[1:]:
            cols = line.split()
            if len(cols) < 10 or cols[0].startswith("lo") or cols[0] in seen:
                continue
            try:
                rx += int(cols[6])
                tx += int(cols[9])
            except ValueError:
                continue
            seen.add(cols[0])
        return (rx, tx) if seen else (None, None)

    def cpu_extra(self):
        """macOS exposes no iowait/steal; never invent a zero."""
        return None, None

    def uptime_s(self):
        out = run("sysctl -n kern.boottime")
        m = re.search(r"sec = (\d+)", out)
        return int(time.time() - int(m.group(1))) if m else None


def host_for():
    """The platform adapter for this machine."""
    return DarwinHost() if platform.system() == "Darwin" else LinuxHost()


def collect_host(name, kind, host=None, volumes_from=None):
    """Build the /api/push payload for this machine.

    ``volumes_from`` overrides the adapter's discovery when a caller needs a
    narrower set (the pve host reports only its root filesystem).
    """
    host = host or host_for()
    used, total = host.mem()
    found = list(volumes_from() if volumes_from is not None else host.volumes())
    load_1, load_5, load_15 = host.load_average()
    swap_used, swap_total = host.swap()
    net_rx, net_tx = host.net_rates()
    iowait, steal = host.cpu_extra()
    core = {
        "name": name,
        "kind": kind,
        "cpu_pct": host.cpu_pct(),
        "mem_pct": round(100.0 * used / total, 1) if total else None,
        "mem_used": used,
        "mem_total": total,
        # One number for the whole device: the fullest volume, so a nearly
        # full data or external drive cannot hide behind a roomy system disk.
        "disk_pct": max((d["pct"] for d in found), default=None),
        "disks": found,
        "uptime_s": host.uptime_s(),
    }
    payload = {key: core[key] for key in CORE_METRICS}
    optional = {
        "load_1": load_1, "load_5": load_5, "load_15": load_15,
        "swap_used": swap_used, "swap_total": swap_total,
        "net_rx_bps": net_rx, "net_tx_bps": net_tx,
        "cpu_iowait": iowait, "cpu_steal": steal,
    }
    for key in EXTRA_METRICS:
        if optional[key] is not None:
            payload[key] = optional[key]
    return payload
