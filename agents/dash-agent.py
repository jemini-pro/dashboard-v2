#!/usr/bin/env python3
"""Dashboard agent for macOS and Linux.

Posts this machine's CPU, memory, every local volume and uptime to the homelab
dashboard.
Stdlib only. Run it every minute from cron/launchd, or with --loop.

    DASH_URL=https://pve.tail73491d.ts.net:8448 DASH_TOKEN=xxx ./dash-agent.py
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

DASH_URL = os.environ.get("DASH_URL", "https://pve.tail73491d.ts.net:8448").rstrip("/")


def load_token():
    """Token from the environment, else from ~/dash/.token (mode 600).

    The file path exists so launchd and cron jobs do not need the secret
    baked into a world-readable plist or crontab line.
    """
    env = os.environ.get("DASH_TOKEN", "").strip()
    if env:
        return env
    path = os.path.expanduser("~/dash/.token")
    if os.path.exists(path):
        with open(path) as f:
            return f.read().strip()
    return ""


DASH_TOKEN = load_token()
NAME = os.environ.get("DASH_NAME", platform.node() or "device")
KIND = os.environ.get("DASH_KIND", "mac" if platform.system() == "Darwin" else "pc")


def run(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10).stdout
    except Exception:  # noqa: BLE001
        return ""


def cpu_pct():
    system = platform.system()
    if system == "Linux":
        def sample():
            with open("/proc/stat") as f:
                v = [float(x) for x in f.readline().split()[1:]]
            idle = v[3] + (v[4] if len(v) > 4 else 0.0)
            return idle, sum(v)
        i1, t1 = sample()
        time.sleep(1)
        i2, t2 = sample()
        return round(100.0 * (1 - (i2 - i1) / (t2 - t1)), 1) if t2 > t1 else None
    out = run("top -l 2 -n 0 | grep 'CPU usage' | tail -1")
    m = re.search(r"([\d.]+)%\s+idle", out)
    return round(100.0 - float(m.group(1)), 1) if m else None


def meminfo():
    """Linux /proc/meminfo in bytes, keyed without the trailing colon."""
    info = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, rest = line.partition(":")
                try:
                    info[key] = int(rest.strip().split()[0]) * 1024
                except (ValueError, IndexError):
                    continue
    except OSError:
        return {}
    return info


def mem():
    system = platform.system()
    if system == "Linux":
        info = meminfo()
        total = info.get("MemTotal", 0)
        used = total - info.get("MemAvailable", info.get("MemFree", 0))
        return used, total
    total = int(run("sysctl -n hw.memsize").strip() or 0)
    pages = {}
    for line in run("vm_stat").splitlines():
        m = re.match(r'"?([\w\s]+)"?:\s+(\d+)', line)
        if m:
            pages[m.group(1).strip()] = int(m.group(2))
    page = 4096
    used = (pages.get("Anonymous pages", 0) + pages.get("Pages wired down", 0)) * page
    return used, total


SIZE_MULT = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}


def _size_bytes(number, unit):
    return int(float(number) * SIZE_MULT[unit.upper()])


def swap():
    """(used, total) swap bytes, or (None, None) when the OS has no data.

    Windows has no equivalent here; its agent reports the page file instead.
    A machine with swap disabled reports (0, 0), which the server treats as
    absent rather than as a full disk.
    """
    if platform.system() == "Linux":
        info = meminfo()
        total = info.get("SwapTotal", 0)
        if not total:
            return 0, 0
        return max(0, total - info.get("SwapFree", 0)), total
    out = run("sysctl -n vm.swapusage")
    total = re.search(r"total = ([\d.]+)([KMGT])", out)
    used = re.search(r"used = ([\d.]+)([KMGT])", out)
    if not total or not used:
        return None, None
    return (_size_bytes(used.group(1), used.group(2)),
            _size_bytes(total.group(1), total.group(2)))


def load_average():
    """The 1/5/15-minute load averages, or (None, None, None) if unavailable."""
    try:
        one, five, fifteen = os.getloadavg()
    except (OSError, AttributeError):
        return None, None, None
    return round(one, 2), round(five, 2), round(fifteen, 2)


def net_counters():
    """(rx_bytes, tx_bytes) across real interfaces, or (None, None)."""
    if platform.system() == "Linux":
        rx = tx = 0
        try:
            with open("/proc/net/dev") as f:
                for line in f.readlines()[2:]:
                    name, sep, rest = line.partition(":")
                    if not sep or name.strip() == "lo":
                        continue
                    cols = rest.split()
                    if len(cols) < 9:
                        continue
                    rx += int(cols[0])
                    tx += int(cols[8])
        except (OSError, ValueError):
            return None, None
        return rx, tx
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


def net_rates():
    """Bytes per second over a one-second delta, or (None, None)."""
    rx1, tx1 = net_counters()
    at1 = time.time()
    if rx1 is None or tx1 is None:
        return None, None
    time.sleep(1)
    rx2, tx2 = net_counters()
    at2 = time.time()
    if (rx2 is None or tx2 is None or at2 <= at1
            or rx2 < rx1 or tx2 < tx1):
        return None, None
    elapsed = at2 - at1
    return round((rx2 - rx1) / elapsed), round((tx2 - tx1) / elapsed)


def cpu_extra():
    """(iowait %, steal %) over a one-second delta; (None, None) off Linux."""
    if platform.system() != "Linux":
        return None, None

    def sample():
        with open("/proc/stat") as f:
            v = [float(x) for x in f.readline().split()[1:]]
        return (v[4] if len(v) > 4 else 0.0,
                v[7] if len(v) > 7 else 0.0, sum(v))

    i1, s1, t1 = sample()
    time.sleep(1)
    i2, s2, t2 = sample()
    if t2 <= t1 or i2 < i1 or s2 < s1:
        return None, None
    return (round(100.0 * (i2 - i1) / (t2 - t1), 1),
            round(100.0 * (s2 - s1) / (t2 - t1), 1))


def uptime_s():
    system = platform.system()
    if system == "Linux":
        with open("/proc/uptime") as f:
            return int(float(f.read().split()[0]))
    out = run("sysctl -n kern.boottime")
    m = re.search(r"sec = (\d+)", out)
    return int(time.time() - int(m.group(1))) if m else None


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


def mounts():
    """Yield (device, mountpoint, fstype) for every mounted filesystem."""
    if platform.system() == "Linux":
        try:
            with open("/proc/mounts") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) < 3:
                        continue
                    # The kernel escapes spaces, tabs and backslashes in \ooo.
                    mount = (parts[1].replace("\\040", " ")
                             .replace("\\011", "\t").replace("\\134", "\\"))
                    yield parts[0], mount, parts[2]
        except OSError:
            return
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
            labels[os.path.realpath(os.path.join("/dev/disk/by-label", name))] = name.replace("\\x20", " ")
        except OSError:
            pass
    return labels


def disks():
    """Every real local volume, root first.

    Pseudo filesystems, snap loops and duplicate bind mounts are skipped, and
    the same physical volume mounted twice is reported once.
    """
    labels = volume_labels()
    seen = set()
    found = []
    for device, mount, fstype in mounts():
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


def collect():
    used, total = mem()
    volumes = disks()
    load_1, load_5, load_15 = load_average()
    swap_used, swap_total = swap()
    net_rx, net_tx = net_rates()
    iowait, steal = cpu_extra()
    payload = {
        "name": NAME,
        "kind": KIND,
        "cpu_pct": cpu_pct(),
        "mem_pct": round(100.0 * used / total, 1) if total else None,
        "mem_used": used,
        "mem_total": total,
        # One number for the whole device: the fullest volume, so a nearly full
        # data or external drive cannot hide behind a roomy system disk.
        "disk_pct": max((d["pct"] for d in volumes), default=None),
        "disks": volumes,
        "uptime_s": uptime_s(),
    }
    # Optional signals are omitted when this OS cannot provide them. The
    # server omits them from the API too, so nothing renders as a fake zero.
    for key, value in (
        ("load_1", load_1), ("load_5", load_5), ("load_15", load_15),
        ("swap_used", swap_used), ("swap_total", swap_total),
        ("net_rx_bps", net_rx), ("net_tx_bps", net_tx),
        ("cpu_iowait", iowait), ("cpu_steal", steal),
    ):
        if value is not None:
            payload[key] = value
    return payload


AGENT_PATH = "/agents/dash-agent.py"


def self_update():
    """Replace this file with the served agent when the two differ.

    Nobody revisits an installed agent: the cron job or LaunchAgent keeps
    running whatever the installer downloaded, so a fix on the server would
    otherwise only reach machines somebody logs into by hand. The served copy
    is the source of truth. Returns True when the file on disk changed.

    Never raises: an unreachable server or a read-only install must not stop
    the heartbeat. DASH_AGENT_UPDATE=0 opts a development copy out entirely.
    """
    if os.environ.get("DASH_AGENT_UPDATE") == "0":
        return False
    path = os.path.abspath(globals().get("__file__") or "")
    if not path:
        return False
    try:
        with urllib.request.urlopen(f"{DASH_URL}{AGENT_PATH}", timeout=10) as response:
            served = response.read().decode()
        with open(path) as f:
            current = f.read()
    except Exception:  # noqa: BLE001 - offline is normal
        return False
    if not served.strip() or served.strip() == current.strip():
        return False
    tmp = path + ".new"
    try:
        with open(tmp, "w") as f:
            f.write(served)
        os.chmod(tmp, 0o755)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False
    return True


def restart():
    """Exec the file on disk, which self_update may just have replaced."""
    os.execv(sys.executable, [sys.executable, os.path.abspath(__file__)] + sys.argv[1:])


def push(payload):
    req = urllib.request.Request(
        f"{DASH_URL}/api/push/{NAME}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "X-Dash-Token": DASH_TOKEN},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise SystemExit(
                f"401: dashboard rejected the token (sent {len(DASH_TOKEN)} chars, expected 48).\n"
                "  Set it with:  printf '%s' '<token>' > ~/dash/.token && chmod 600 ~/dash/.token\n"
                "  Get it with:  pct exec 200 -- sed -n 's/^DASH_TOKEN=//p' /opt/stack/dash/.env"
            ) from None
        raise


if __name__ == "__main__":
    if self_update():
        restart()
    if "--loop" not in sys.argv:
        print(push(collect()))
        sys.exit(0)
    while True:
        try:
            print(push(collect()), flush=True)
        except Exception as exc:  # noqa: BLE001
            print("push failed:", exc, flush=True)
        time.sleep(60)
        # A loop can run for weeks, so look for a new agent between pushes
        # rather than waiting for a reboot.
        if self_update():
            restart()
