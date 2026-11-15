#!/usr/bin/env python3
"""Dashboard agent for macOS and Linux.

Posts this machine's CPU, memory, every local volume and uptime to the homelab
dashboard.
Stdlib only. Run it every minute from cron/launchd, or with --loop.

    DASH_URL=https://pve.tail73491d.ts.net:8448 DASH_TOKEN=xxx ./dash-agent.py

The host collectors and the wire live in dash_common.py, installed beside this
file. A copy installed before that module existed fetches it on first run. The
failure-mode collectors (SMART, ZFS, UPS, backups, updates) stay in this file
until the planned move into dash_common.py, so this branch stands alone.
"""
import glob
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(globals().get("__file__") or "dash-agent.py"))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    import dash_common as common
except ImportError:  # an old install that predates the shared module
    common = None

DASH_URL = os.environ.get("DASH_URL", "https://pve.tail73491d.ts.net:8448").rstrip("/")
NAME = os.environ.get("DASH_NAME", platform.node() or "device")
KIND = os.environ.get("DASH_KIND", "mac" if platform.system() == "Darwin" else "pc")

# ---------- failure-mode collectors ----------
# Local to this agent on purpose: another session is consolidating host
# collectors into dash_common.py, and this branch must stand alone until that
# lands. Every collector is best effort: a missing command, permission error,
# timeout or unsupported platform returns None and the block is omitted. A
# heartbeat must never fail because SMART or a backup probe did.
HEALTH_TIMEOUT = 4
SMART_BUDGET_S = 12
MAX_HEALTH_ITEMS = 16


def _is_number(value):
    return (not isinstance(value, bool)
            and isinstance(value, (int, float)))


def _which(name):
    return shutil.which(name)


def _run(argv, timeout=HEALTH_TIMEOUT):
    """Run an argv list and return stdout, or '' on any failure."""
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout
        ).stdout
    except Exception:  # noqa: BLE001 - missing tool, permission, timeout
        return ""


def _run_result(argv, timeout=HEALTH_TIMEOUT):
    """(returncode, stdout); a missing command is (1, '')."""
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout
        )
        return proc.returncode, proc.stdout
    except Exception:  # noqa: BLE001
        return 1, ""


def _run_shell(command, timeout=HEALTH_TIMEOUT):
    """Only the opt-in DASH_BACKUP_CMD uses a shell; it is the user's command."""
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
    """Physical devices smartctl reports, falling back to the usual names."""
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

    # A device that reported only its name is not a health reading.
    return entry if len(entry) > 1 else None


def _diskutil_smart():
    """macOS fallback when smartctl is not installed."""
    listing = _run(["diskutil", "list"], timeout=6)
    out = []
    for line in listing.splitlines():
        if not line.startswith("/dev/disk") or "physical" not in line:
            continue
        device = line.split()[0]
        if not re.fullmatch(r"/dev/disk\d+", device):
            continue
        info = _run(["diskutil", "info", device])
        status = ""
        for row in info.splitlines():
            if row.strip().lower().startswith("smart status:"):
                status = row.split(":", 1)[1].strip()
                break
        state = status.lower()
        if state == "verified":
            health = "PASSED"
        elif state == "failing":
            health = "FAILED"
        else:
            # Apple Silicon disks often report "Not Supported"; that is no
            # reading at all, not a failure.
            continue
        out.append({"device": device, "health": health})
        if len(out) >= MAX_HEALTH_ITEMS:
            break
    return out or None


def collect_smart():
    if not _health_enabled():
        return None
    if _which("smartctl"):
        deadline = time.monotonic() + SMART_BUDGET_S
        out = []
        for device in _smart_devices():
            if time.monotonic() > deadline:
                break
            entry = _smart_entry(device)
            if entry:
                out.append(entry)
        return out or None
    if platform.system() == "Darwin" and _which("diskutil"):
        return _diskutil_smart()
    return None


def _zfs_details(text):
    """Errors and last-scrub age from `zpool status` output, best effort."""
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
        pool, state, capacity = parts[0], parts[1], parts[2]
        entry = {"pool": pool, "state": state}
        try:
            entry["capacity_pct"] = round(float(capacity), 1)
        except ValueError:
            pass
        entry.update(_zfs_details(
            _run(["zpool", "status", "-p", pool], timeout=6)
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
    """A unix timestamp from a bare number or a JSON object, or None."""
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
        # A simulation needs no root and no lock.
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
    """All optional failure-mode blocks this host can report."""
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


def agent_path():
    """The path this script was loaded from, for self-update and bootstrap."""
    return os.path.abspath(globals().get("__file__") or "")


def collect():
    payload = common.collect_host(NAME, KIND)
    payload.update(collect_health())
    return payload


def push(payload):
    token = common.load_token()
    try:
        return common.push(NAME, payload, token, DASH_URL)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise SystemExit(
                f"401: dashboard rejected the token (sent {len(token)} chars, expected 48).\n"
                "  Set it with:  printf '%s' '<token>' > ~/dash/.token && chmod 600 ~/dash/.token\n"
                "  Get it with:  pct exec 200 -- sed -n 's/^DASH_TOKEN=//p' /opt/stack/dash/.env"
            ) from None
        raise


def self_update():
    """Replace this file and dash_common.py with the served copies.

    Nobody revisits an installed agent: the cron job or LaunchAgent keeps
    running whatever the installer downloaded, so a fix on the server would
    otherwise only reach machines somebody logs into by hand. The served copy
    is the source of truth, including the shared module this script imports.
    Returns True when a file on disk changed.

    Never raises: an unreachable server or a read-only install must not stop
    the heartbeat. DASH_AGENT_UPDATE=0 opts a development copy out entirely.
    """
    if common is None:
        return False
    files = common.installed_files("dash-agent.py", agent_path())
    return bool(common.update_installed(DASH_URL, files))


def bootstrap_common():
    """Fetch dash_common.py beside this file when an old install lacks it.

    A device installed before the shared module existed self-updates to an
    agent that imports a sibling. Its first run must fetch that sibling
    instead of dying with ImportError. Returns True when the file was written.
    """
    path = agent_path()
    if not path:
        return False
    target = os.path.join(os.path.dirname(path), "dash_common.py")
    try:
        with urllib.request.urlopen(
            f"{DASH_URL}/agents/dash_common.py", timeout=10
        ) as response:
            served = response.read().decode()
    except Exception:  # noqa: BLE001 - offline is normal
        return False
    if not served.strip():
        return False
    tmp = target + ".new"
    try:
        with open(tmp, "w") as handle:
            handle.write(served)
        os.chmod(tmp, 0o755)
        os.replace(tmp, target)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False
    return True


def restart():
    """Exec the file on disk, which self_update may just have replaced."""
    path = os.path.abspath(__file__)
    if common is not None:
        common.restart(path)
        return
    # Bootstrap runs before dash_common can be imported.
    os.execv(sys.executable, [sys.executable, path] + sys.argv[1:])


if __name__ == "__main__":
    if common is None:
        if bootstrap_common():
            restart()
        print("dash_common.py is missing beside the agent and could not be fetched",
              file=sys.stderr)
        raise SystemExit(1)
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
