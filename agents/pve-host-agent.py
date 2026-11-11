#!/usr/bin/env python3
"""Push pve host stats (and its guests) to the homelab dashboard.

Runs on the host as root, from a systemd timer. Reads /proc for itself and
`pvesh` for the guest list, then POSTs each device to the dashboard API.
No Proxmox API token, no firewall hole: the host reaches the container, which
is the direction that already works.
"""
import json
import os
import shutil
import subprocess
import time
import urllib.request

URL = os.environ.get("DASH_URL", "http://10.10.10.20:8448").rstrip("/")
TOKEN = os.environ.get("DASH_TOKEN", "")
SKIP = {n.strip() for n in os.environ.get("DASH_SKIP", "docker01").split(",") if n.strip()}


def push(name, payload):
    req = urllib.request.Request(
        f"{URL}/api/push/{name}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "X-Dash-Token": TOKEN},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status


def cpu_pct():
    def sample():
        with open("/proc/stat") as f:
            v = [float(x) for x in f.readline().split()[1:]]
        return v[3] + (v[4] if len(v) > 4 else 0.0), sum(v)
    i1, t1 = sample()
    time.sleep(1)
    i2, t2 = sample()
    return round(100.0 * (1 - (i2 - i1) / (t2 - t1)), 1) if t2 > t1 else None


def mem():
    d = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, _, rest = line.partition(":")
            d[k] = int(rest.strip().split()[0]) * 1024
    total = d.get("MemTotal", 0)
    return total - d.get("MemAvailable", d.get("MemFree", 0)), total


def uptime():
    with open("/proc/uptime") as f:
        return int(float(f.read().split()[0]))


def guests():
    try:
        out = subprocess.run(
            ["pvesh", "get", "/cluster/resources", "--type", "vm", "--output-format", "json"],
            capture_output=True, text=True, timeout=20,
        ).stdout
        data = json.loads(out) if out.strip() else []
        if isinstance(data, dict):
            data = data.get("data", [])
        return [g for g in data if g.get("type") == "lxc" and g.get("status") == "running"]
    except Exception as exc:  # noqa: BLE001
        print("guest list failed:", exc)
        return []


def root_disks():
    """The host's root filesystem as a volume list.

    LXC guests have no per-volume data from pvesh, so only the host sends
    this. The server rejects a malformed entry rather than storing it.
    """
    try:
        du = shutil.disk_usage("/")
    except OSError:
        return []
    if not du.total:
        return []
    return [{
        "mount": "/",
        "label": "System",
        "total": du.total,
        "used": du.used,
        "pct": round(100.0 * du.used / du.total, 1),
    }]


def main():
    used, total = mem()
    disks = root_disks()
    errors = []

    try:
        push("pve", {
            "name": "pve",
            "kind": "host",
            "cpu_pct": cpu_pct(),
            "mem_pct": round(100.0 * used / total, 1) if total else None,
            "mem_used": used,
            "mem_total": total,
            "disk_pct": disks[0]["pct"] if disks else None,
            "disks": disks,
            "uptime_s": uptime(),
        })
        print("pushed pve")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"pve: {exc}")

    for g in guests():
        name = g.get("name") or f"ct{g.get('vmid')}"
        if name in SKIP:
            continue
        try:
            push(name, {
                "name": name,
                "kind": "lxc-guest",
                "cpu_pct": round(100.0 * g.get("cpu", 0), 1),
                "mem_pct": round(100.0 * g["mem"] / g["maxmem"], 1) if g.get("maxmem") else None,
                "mem_used": g.get("mem"),
                "mem_total": g.get("maxmem"),
                "uptime_s": g.get("uptime"),
            })
            print("pushed", name)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {exc}")

    if errors:
        print("errors:", "; ".join(errors))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
