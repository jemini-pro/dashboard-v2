#!/usr/bin/env python3
"""Push Docker container stats from docker01 to the homelab dashboard.

Runs on the Docker host as root, once a minute, from cron. Reads `docker stats`
and `docker inspect`, then POSTs one device per **running** container with
kind="container". The dashboard lists each one in its Docker block and charts
it like any other device.

The dashboard never gets the Docker socket: the host pushes to the container,
which is the direction that already works. A container that stops simply stops
reporting, and the dashboard marks it exited once the heartbeat goes stale.
"""
import calendar
import json
import os
import subprocess
import time
import urllib.request

URL = os.environ.get("DASH_URL", "http://10.10.10.20:8448").rstrip("/")
ENV_FILE = os.environ.get("DASH_ENV_FILE", "/opt/stack/dash/.env")

SIZE_UNITS = {
    "B": 1,
    "KiB": 1024,
    "MiB": 1024 ** 2,
    "GiB": 1024 ** 3,
    "TiB": 1024 ** 4,
    "kB": 1000,
    "KB": 1000,
    "MB": 1000 ** 2,
    "GB": 1000 ** 3,
}


def token():
    value = os.environ.get("DASH_TOKEN", "")
    if value:
        return value
    try:
        with open(ENV_FILE) as handle:
            for line in handle:
                key, _, rest = line.partition("=")
                if key.strip() == "DASH_TOKEN":
                    return rest.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def docker(*args):
    try:
        return subprocess.run(
            ["docker", *args], capture_output=True, text=True, timeout=20
        )
    except (OSError, subprocess.SubprocessError):
        return None


def parse_size(text):
    """'123.4MiB' -> bytes, or None when the field is unusable."""
    text = (text or "").strip()
    for suffix in ("KiB", "MiB", "GiB", "TiB", "kB", "KB", "MB", "GB", "B"):
        if text.endswith(suffix):
            try:
                return int(float(text[: -len(suffix)].strip()) * SIZE_UNITS[suffix])
            except ValueError:
                return None
    return None


def percent(text):
    try:
        return float(str(text or "").strip().rstrip("%"))
    except ValueError:
        return None


def stats():
    """name -> dict of cpu_pct/mem_pct/mem_used/mem_total for running containers."""
    out = {}
    result = docker("stats", "--no-stream", "--format", "{{json .}}")
    if result is None or result.returncode != 0:
        return out
    for line in result.stdout.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = row.get("Name") or row.get("Container")
        if not name:
            continue
        used, _, total = (row.get("MemUsage") or "").partition("/")
        out[name] = {
            "cpu_pct": percent(row.get("CPUPerc")),
            "mem_pct": percent(row.get("MemPerc")),
            "mem_used": parse_size(used),
            "mem_total": parse_size(total),
        }
    return out


def uptime_from(started):
    """Docker reports UTC to the nanosecond; the first 19 chars are enough."""
    try:
        return max(0, int(time.time() - calendar.timegm(
            time.strptime(str(started)[:19], "%Y-%m-%dT%H:%M:%S")
        )))
    except (ValueError, TypeError):
        return None


def describe(name):
    result = docker("inspect", name)
    if result is None or result.returncode != 0:
        return {}
    try:
        data = json.loads(result.stdout)[0]
    except (json.JSONDecodeError, IndexError):
        return {}
    state = data.get("State") or {}
    config = data.get("Config") or {}
    labels = config.get("Labels") or {}
    image = (config.get("Image") or "").split("/")[-1].split(":")[0]
    role = labels.get("dash.role") or image or "container"
    health = (state.get("Health") or {}).get("Status")
    return {
        "state": state.get("Status") or "running",
        "role": f"{role} · {health}" if health else role,
        "uptime_s": uptime_from(state.get("StartedAt")),
    }


def push(name, payload, auth):
    request = urllib.request.Request(
        f"{URL}/api/push/{name}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "X-Dash-Token": auth},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status


def main():
    auth = token()
    if not auth:
        print("no DASH_TOKEN: set it in the environment or in " + ENV_FILE)
        return 1

    measured = stats()
    failures = 0
    for name, metrics in measured.items():
        payload = {"kind": "container", "name": name, **metrics}
        payload.update(describe(name))
        payload = {k: v for k, v in payload.items() if v is not None}
        try:
            push(name, payload, auth)
        except Exception as exc:  # noqa: BLE001 - one container must not stop the rest
            failures += 1
            print(f"push {name} failed: {exc}")
    print(f"pushed {len(measured) - failures}/{len(measured)} containers")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
