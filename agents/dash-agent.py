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


def mem():
    system = platform.system()
    if system == "Linux":
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, rest = line.partition(":")
                info[k] = int(rest.strip().split()[0]) * 1024
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
    return {
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
    import sys
    if "--loop" in sys.argv:
        while True:
            try:
                print(push(collect()), flush=True)
            except Exception as exc:  # noqa: BLE001
                print("push failed:", exc, flush=True)
            time.sleep(60)
    print(push(collect()))
