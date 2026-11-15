#!/usr/bin/env python3
"""Push pve host stats (and its guests) to the homelab dashboard.

Runs on the host as root, from a systemd timer. The host readings and the
wire live in dash_common.py, installed beside this file; what stays here is
the Proxmox guest list, which only this agent has. Reads /proc for itself and
`pvesh` for the guests, then POSTs each device to the dashboard API.
No Proxmox API token, no firewall hole: the host reaches the container, which
is the direction that already works.
"""
import json
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(globals().get("__file__") or "dash-pve-agent.py"))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import dash_common as common  # noqa: E402

URL = os.environ.get("DASH_URL", "http://10.10.10.20:8448").rstrip("/")
SKIP = {n.strip() for n in os.environ.get("DASH_SKIP", "docker01").split(",") if n.strip()}
HOST = common.LinuxHost()


def self_update():
    """Pull a changed agent or shared module, so a deploy reaches this host.

    The service runs once a minute, so a replaced file is picked up by the
    next run even without the re-exec below. Never raises.
    """
    files = common.installed_files(
        "pve-host-agent.py", globals().get("__file__") or ""
    )
    return bool(common.update_installed(URL, files))


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


def guest_payload(guest):
    """One LXC guest. Guests have no host-level volumes or signals."""
    name = guest.get("name") or f"ct{guest.get('vmid')}"
    return {
        "name": name,
        "kind": "lxc-guest",
        "cpu_pct": round(100.0 * guest.get("cpu", 0), 1),
        "mem_pct": (round(100.0 * guest["mem"] / guest["maxmem"], 1)
                    if guest.get("maxmem") else None),
        "mem_used": guest.get("mem"),
        "mem_total": guest.get("maxmem"),
        "uptime_s": guest.get("uptime"),
    }


def main():
    if self_update():
        common.restart(__file__)
    token = common.load_token()
    errors = []

    try:
        # The host reports its root filesystem only; /proc/mounts discovery
        # would list container bind mounts that are not host drives.
        common.push("pve", common.collect_host(
            "pve", "host", host=HOST, volumes_from=HOST.root_volume
        ), token, URL)
        print("pushed pve")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"pve: {exc}")

    for guest in guests():
        payload = guest_payload(guest)
        name = payload["name"]
        if name in SKIP:
            continue
        try:
            common.push(name, payload, token, URL)
            print("pushed", name)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {exc}")

    if errors:
        print("errors:", "; ".join(errors))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
