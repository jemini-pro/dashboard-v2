#!/usr/bin/env python3
"""Push pve host stats (and its guests) to the homelab dashboard.

Runs on the host as root, from a systemd timer. The host readings and the
wire live in dash_common.py, installed beside this file; what stays here is
the Proxmox guest list, which only this agent has. Reads /proc for itself and
`pvesh` for the guests, then POSTs each device to the dashboard API.
No Proxmox API token, no firewall hole: the host reaches the container, which
is the direction that already works.
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(globals().get("__file__") or "dash-pve-agent.py"))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import dash_common as common  # noqa: E402

URL = os.environ.get("DASH_URL", "http://10.10.10.20:8448").rstrip("/")
SKIP = {n.strip() for n in os.environ.get("DASH_SKIP", "docker01").split(",") if n.strip()}
HOST = common.LinuxHost()

# ---------- failure-mode collectors ----------
# Kept local to this agent on purpose: another session is consolidating host
# collectors into dash_common.py, and this branch must stand alone until that
# lands. Every collector is best effort: a missing command, permission error,
# timeout or unsupported platform returns None and the block is omitted.
HEALTH_TIMEOUT = 4
SMART_BUDGET_S = 12
MAX_HEALTH_ITEMS = 16


def _is_number(value):
    return (not isinstance(value, bool)
            and isinstance(value, (int, float)))


def _which(name):
    return shutil.which(name)


def _run(argv, timeout=HEALTH_TIMEOUT):
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout
        ).stdout
    except Exception:  # noqa: BLE001 - missing tool, permission, timeout
        return ""


def _run_result(argv, timeout=HEALTH_TIMEOUT):
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout
        )
        return proc.returncode, proc.stdout
    except Exception:  # noqa: BLE001
        return 1, ""


def _run_shell(command, timeout=HEALTH_TIMEOUT):
    try:
        return subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=timeout,
        ).stdout
    except Exception:  # noqa: BLE001
        return ""


def _health_enabled():
    return os.environ.get("DASH_HEALTH", "").strip().lower() not in (
        "0", "false", "no", "off"
    )


def _smart_devices():
    scan = _run(["smartctl", "--scan-open"], timeout=6)
    devices = []
    for line in scan.splitlines():
        parts = line.split()
        if parts and parts[0].startswith("/dev/"):
            devices.append(parts[0])
    if not devices:
        for pattern in ("/dev/sd[a-z]", "/dev/nvme[0-9]n[0-9]",
                        "/dev/vd[a-z]"):
            devices.extend(sorted(glob.glob(pattern)))
    unique = []
    for device in devices:
        if device not in unique:
            unique.append(device)
    return unique[:MAX_HEALTH_ITEMS]


def _smart_temperature(info):
    temperature = info.get("temperature")
    if isinstance(temperature, dict):
        temperature = temperature.get("current")
    if _is_number(temperature):
        return int(temperature)
    nvme = info.get("nvme_smart_health_information_log")
    if isinstance(nvme, dict) and _is_number(nvme.get("temperature")):
        return int(nvme["temperature"])
    return None


def _smart_entry(device):
    out = _run(["smartctl", "-H", "-A", "-j", device])
    try:
        info = json.loads(out)
    except (TypeError, ValueError):
        return None
    if not isinstance(info, dict):
        return None
    entry = {"device": device}
    status = info.get("smart_status")
    if isinstance(status, dict) and isinstance(status.get("passed"), bool):
        entry["health"] = "PASSED" if status["passed"] else "FAILED"
    temperature = _smart_temperature(info)
    if temperature is not None:
        entry["temperature_c"] = temperature
    attributes = info.get("ata_smart_attributes")
    table = attributes.get("table") if isinstance(attributes, dict) else None
    if isinstance(table, list):
        for attribute in table:
            if not isinstance(attribute, dict):
                continue
            raw = attribute.get("raw")
            value = raw.get("value") if isinstance(raw, dict) else None
            if not _is_number(value):
                continue
            if attribute.get("id") == 5:
                entry["reallocated"] = int(value)
            elif attribute.get("id") == 197:
                entry["pending"] = int(value)
    power = info.get("power_on_time")
    hours = power.get("hours") if isinstance(power, dict) else None
    if not _is_number(hours):
        nvme = info.get("nvme_smart_health_information_log")
        hours = nvme.get("power_on_hours") if isinstance(nvme, dict) else None
    if _is_number(hours):
        entry["power_on_hours"] = int(hours)
    return entry if len(entry) > 1 else None


def collect_smart():
    if not _health_enabled() or not _which("smartctl"):
        return None
    deadline = time.monotonic() + SMART_BUDGET_S
    out = []
    for device in _smart_devices():
        if time.monotonic() > deadline:
            break
        entry = _smart_entry(device)
        if entry:
            out.append(entry)
    return out or None


def _zfs_details(text):
    details = {}
    match = re.search(r"^\s*errors:\s*(.+)$", text, re.MULTILINE)
    if match:
        value = match.group(1).strip()
        if value.lower().startswith("no known data errors"):
            details["errors"] = 0
        else:
            # "Permanent errors have been detected" has no leading count but
            # is still a nonzero error state; never drop it.
            count = re.match(r"(\d+)", value)
            details["errors"] = int(count.group(1)) if count else 1
    for line in text.splitlines():
        if "scrub repaired" not in line and "resilvered" not in line:
            continue
        stamp = re.search(
            r"on\s+(\w{3}\s+\w{3}\s+\d+\s+\d{2}:\d{2}:\d{2}\s+\d{4})",
            line,
        )
        if stamp:
            try:
                parsed = time.mktime(time.strptime(
                    stamp.group(1), "%a %b %d %H:%M:%S %Y"
                ))
            except ValueError:
                parsed = None
            if parsed:
                details["scrub_age_s"] = max(0, int(time.time() - parsed))
        break
    return details


def collect_zfs():
    if not _health_enabled() or not _which("zpool"):
        return None
    listing = _run(
        ["zpool", "list", "-H", "-p", "-o", "name,health,capacity"],
        timeout=6,
    )
    pools = []
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        entry = {"pool": parts[0], "state": parts[1]}
        try:
            entry["capacity_pct"] = round(float(parts[2]), 1)
        except ValueError:
            pass
        entry.update(_zfs_details(
            _run(["zpool", "status", "-p", parts[0]], timeout=6)
        ))
        pools.append(entry)
        if len(pools) >= MAX_HEALTH_ITEMS:
            break
    return pools or None


def collect_ups():
    name = os.environ.get("DASH_UPS_NAME", "").strip()
    if not _health_enabled() or not name or not _which("upsc"):
        return None
    values = {}
    for line in _run(["upsc", name]).splitlines():
        key, separator, value = line.partition(":")
        if separator:
            values[key.strip()] = value.strip()
    status = values.get("ups.status")
    if not status:
        return None
    entry = {"status": status}
    for key, field in (
        ("battery.charge", "charge_pct"),
        ("battery.runtime", "runtime_s"),
        ("ups.load", "load_pct"),
    ):
        if key not in values:
            continue
        try:
            entry[field] = float(values[key])
        except ValueError:
            continue
    entry["battery_low"] = "LB" in status.upper().split()
    return entry


def _timestamp(text):
    text = (text or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, dict):
        data = data.get("last_success_ts")
    if _is_number(data) and data > 0:
        return int(data)
    token = text.split()[0]
    try:
        value = float(token)
    except ValueError:
        return None
    return int(value) if value > 0 else None


def collect_backup():
    """Opt-in: DASH_BACKUP_TS_FILE, else DASH_BACKUP_CMD, both documented."""
    if not _health_enabled():
        return None
    stamp = None
    path = os.environ.get("DASH_BACKUP_TS_FILE", "").strip()
    if path:
        try:
            with open(os.path.expanduser(path)) as handle:
                stamp = _timestamp(handle.read())
        except OSError:
            stamp = None
    if stamp is None:
        command = os.environ.get("DASH_BACKUP_CMD", "").strip()
        if command:
            stamp = _timestamp(_run_shell(command, timeout=5))
    if stamp is None:
        return None
    entry = {"last_success_ts": stamp}
    tool = os.environ.get("DASH_BACKUP_TOOL", "").strip()
    if tool:
        entry["tool"] = tool
    return entry


def collect_updates():
    if not _health_enabled():
        return None
    entry = {}
    if _which("apt-get"):
        out = _run(
            ["apt-get", "-s", "-o", "Debug::NoLocking=true", "upgrade"],
            timeout=8,
        )
        lines = [line for line in out.splitlines() if line.startswith("Inst ")]
        if lines:
            entry["pending"] = len(lines)
            security = sum(1 for line in lines if "security" in line.lower())
            if security:
                entry["security"] = security
    elif _which("dnf"):
        _code, out = _run_result(["dnf", "-q", "check-update"], timeout=10)
        packages = [
            line for line in out.splitlines()
            if line.strip() and not line.startswith(("Last metadata", "Obsoleting"))
        ]
        if packages:
            entry["pending"] = len(packages)
            _code, out = _run_result(
                ["dnf", "-q", "updateinfo", "list", "security"], timeout=10
            )
            security = len([
                line for line in out.splitlines()
                if line.strip() and not line.startswith(("Last metadata", "Obsoleting"))
            ])
            if security:
                entry["security"] = security
    if os.path.exists("/var/run/reboot-required"):
        entry["reboot_required"] = True
    return entry or None


def collect_health():
    blocks = {}
    if not _health_enabled():
        return blocks
    for kind, collector in (
        ("smart", collect_smart),
        ("zfs", collect_zfs),
        ("ups", collect_ups),
        ("backup", collect_backup),
        ("updates", collect_updates),
    ):
        try:
            block = collector()
        except Exception:  # noqa: BLE001 - one probe must not fail the heartbeat
            block = None
        if block:
            blocks[kind] = block
    return blocks


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
        # would list container bind mounts that are not host drives. Health
        # blocks describe the host, so guests never inherit them.
        payload = common.collect_host(
            "pve", "host", host=HOST, volumes_from=HOST.root_volume
        )
        payload.update(collect_health())
        common.push("pve", payload, token, URL)
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
