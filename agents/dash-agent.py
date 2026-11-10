#!/usr/bin/env python3
"""Dashboard agent for macOS and Linux.

Posts this machine's CPU, memory, disk and uptime to the homelab dashboard.
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


def disk_pct():
    du = shutil.disk_usage("/")
    return round(100.0 * du.used / du.total, 1) if du.total else None


def collect():
    used, total = mem()
    return {
        "name": NAME,
        "kind": KIND,
        "cpu_pct": cpu_pct(),
        "mem_pct": round(100.0 * used / total, 1) if total else None,
        "mem_used": used,
        "mem_total": total,
        "disk_pct": disk_pct(),
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
