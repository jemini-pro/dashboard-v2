#!/usr/bin/env python3
"""Homelab dashboard server.

Serves the static dashboard, the bookmark store, and a device-stats API.
Stdlib only: no pip, no build step.

Endpoints
    GET  /                  static dashboard
    GET  /api/bookmarks     bookmark store (JSON, single source of truth)
    GET  /api/checks        service-probe state, uptimes and verdicts
    GET  /api/stats         device telemetry (live, plus 24 h availability)
    GET  /api/metrics       one device's history for a window
    GET  /api/metrics/summary   every device's derived history summary
    POST /api/hit           record a bookmark click (beacon)
    POST /api/push/<name>   agent heartbeat + metrics (needs X-Dash-Token)
    GET  /api/health        liveness
"""
import hashlib
import hmac
import http.client
import json
import math
import os
import re
import shutil
import sqlite3
import ssl
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"
DATA = Path(os.environ.get("DASH_DATA_DIR") or BASE / "data")
AGENTS_FILE = DATA / "agents.json"
HITS_FILE = DATA / "hits.json"
METRICS_FILE = DATA / "metrics.db"
CHECKS_FILE = DATA / "checks.json"

HALF_LIFE_DAYS = 14.0
HIT_RETENTION_DAYS = 90
HIT_RETENTION_SECONDS = HIT_RETENTION_DAYS * 86400
PORT = int(os.environ.get("DASH_PORT", "8000"))
TOKEN = os.environ.get("DASH_TOKEN", "").strip()

# History is sampled server-side on a timer, so it keeps running whether or
# not a browser is open. One row per device per bucket; the browser poll rate
# has no bearing on it.
SAMPLE_SECONDS = max(5, int(os.environ.get("DASH_SAMPLE_SECONDS", "60")))
HISTORY_DAYS = max(1, int(os.environ.get("DASH_HISTORY_DAYS", "30")))
HISTORY_SECONDS = HISTORY_DAYS * 86400
# Service checks share the sampler's bucketing discipline but not its cadence:
# a per-check interval may be longer, and the raw check history is deliberately
# short (7 days) because the API only reports 24 h and 7 d uptime.
CHECKS_SECONDS = max(10, int(os.environ.get("DASH_CHECKS_SECONDS", "60")))
CHECK_BUCKET_SECONDS = max(10, int(os.environ.get("DASH_CHECK_BUCKET_SECONDS", "60")))
CHECK_RETENTION_DAYS = max(1, int(os.environ.get("DASH_CHECK_HISTORY_DAYS", "7")))
CHECK_RETENTION_SECONDS = CHECK_RETENTION_DAYS * 86400
# Two pre-aggregated tiers keep long windows from scanning raw rows: 5-minute
# buckets for the 7d/30d ranges and hourly buckets for 90d/1y. They are
# recomputed by the sampler from the current bucket, not by a separate job.
ROLLUP_5M_SECONDS = 300
ROLLUP_1H_SECONDS = 3600
ROLLUP_5M_RETENTION = max(
    1, int(os.environ.get("DASH_ROLLUP_5M_DAYS", "30"))
) * 86400
ROLLUP_1H_RETENTION = max(
    1, int(os.environ.get("DASH_ROLLUP_1H_DAYS", "366"))
) * 86400
RAW_MAX_WINDOW = 86400
ROLLUP_5M_MAX_WINDOW = 30 * 86400
PRUNE_INTERVAL = 3600
SUMMARY_CACHE_SECONDS = 30
METRIC_WINDOWS = {
    "1h": 3600,
    "6h": 6 * 3600,
    "24h": 86400,
    "7d": 7 * 86400,
    "30d": 30 * 86400,
    "90d": 90 * 86400,
    "1y": 365 * 86400,
}
DEFAULT_WINDOW = "24h"
# Each default lands at or below MAX_POINTS so a chart never gets a payload it
# cannot draw. An explicit ?step= is clamped the same way.
DEFAULT_STEPS = {
    "1h": 60, "6h": 300, "24h": 300, "7d": 1800, "30d": 7200,
    "90d": 21600, "1y": 86400,
}
MAX_POINTS = 400

_lock = threading.Lock()
_agents_lock = threading.Lock()
_hits_lock = threading.Lock()
_db_lock = threading.Lock()
_cache = {"at": 0.0, "body": None}
_prev_cpu = {"usec": None, "at": None}
_availability_cache = {"at": 0.0, "by_id": {}}
_summary_cache = {"at": 0.0, "window": None, "body": None}
_brief_cache = {"at": 0.0, "window": None, "body": None}
_last_prune = {"at": 0.0}
# Latest probe per check, kept for the cheap `/api/stats` warning surface. The
# check history itself is never scanned there.
_check_state = {"results": {}}


def _invalidate_summary():
    """A write changes what the fleet summary or brief would say; drop both.

    Called after the write lock is released: stats() holds _lock and then
    takes _db_lock, so invalidating while holding _db_lock could deadlock.
    """
    with _lock:
        _summary_cache.update(at=0.0, window=None, body=None)
        _brief_cache.update(at=0.0, window=None, body=None)


# ---------- local machine ----------
# Docker remounts /proc, so /proc/stat and /proc/meminfo show the whole host.
# The cgroup files describe this container, which is what the card should show.

def _cgroup_read(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def _cgroup_cpu_usec():
    txt = _cgroup_read("/sys/fs/cgroup/cpu.stat") or ""
    for line in txt.splitlines():
        if line.startswith("usage_usec"):
            return int(line.split()[1])
    return None


def _parse_size(text):
    if not text:
        return None
    t = text.strip().upper()
    mult = {"": 1, "K": 1000, "M": 1000 ** 2, "G": 1000 ** 3, "T": 1000 ** 4}
    if t[-1] in mult:
        return int(float(t[:-1]) * mult[t[-1]])
    return int(t) if t.isdigit() else None


def _local_cpu_pct():
    usec = _cgroup_cpu_usec()
    if usec is None:
        return None
    now = time.time()
    prev_usec, prev_at = _prev_cpu["usec"], _prev_cpu["at"]
    if prev_usec is None:
        _prev_cpu.update(usec=usec, at=now)
        time.sleep(0.25)
        usec2, now2 = _cgroup_cpu_usec(), time.time()
        if usec2 is None or now2 <= now:
            return None
        _prev_cpu.update(usec=usec2, at=now2)
        return round(100.0 * (usec2 - usec) / ((now2 - now) * 1e6), 1)
    pct = None
    if now > prev_at and usec >= prev_usec:
        pct = round(100.0 * (usec - prev_usec) / ((now - prev_at) * 1e6), 1)
    _prev_cpu.update(usec=usec, at=now)
    return pct


def _mem():
    used = _cgroup_read("/sys/fs/cgroup/memory.current")
    limit = _cgroup_read("/sys/fs/cgroup/memory.max")
    used = int(used) if used and used.isdigit() else None
    total = int(limit) if limit and limit.isdigit() else _parse_size(os.environ.get("DASH_MEM_TOTAL"))
    # memory.current counts page cache. Subtract inactive_file, or a box that
    # pulled images once reads as 94% full while it is mostly idle.
    stat = _cgroup_read("/sys/fs/cgroup/memory.stat") or ""
    for line in stat.splitlines():
        if line.startswith("inactive_file ") and used is not None:
            used = max(0, used - int(line.split()[1]))
            break
    if used is None or total is None:
        info = {}
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    key, _, rest = line.partition(":")
                    info[key] = int(rest.strip().split()[0]) * 1024
        except OSError:
            return None, None
        total = total or info.get("MemTotal")
        used = used if used is not None else total - info.get("MemAvailable", 0)
    return total, used


def _uptime():
    try:
        with open("/proc/uptime") as f:
            return int(float(f.read().split()[0]))
    except OSError:
        return None


def local_device():
    total, used = _mem()
    du = shutil.disk_usage(str(BASE))
    disks = []
    if du.total:
        disks.append({
            "mount": "/",
            "label": "System",
            "total": du.total,
            "used": du.used,
            "pct": round(100.0 * du.used / du.total, 1),
        })
    return {
        "id": "docker",
        "name": "docker",
        "kind": "lxc",
        "source": "local",
        "cpu_pct": _local_cpu_pct(),
        "mem_pct": round(100.0 * used / total, 1) if total and used is not None else None,
        "mem_used": used,
        "mem_total": total,
        "disk_pct": disks[0]["pct"] if disks else None,
        "disks": disks,
        "uptime_s": _uptime(),
        "online": True,
        "last_seen": int(time.time()),
    }


# Static Docker layer. server/docker-compose.yml defines exactly these two
# services, so their names and roles are deployment facts rather than sampled
# telemetry; container-level CPU/RAM would need a Docker socket or an agent.
DOCKER_CONTAINERS = (
    {"name": "caddy", "role": "reverse proxy · :8448", "state": "running"},
    {"name": "dash", "role": "this dashboard · :8000", "state": "running"},
)

# Display-only strings a container agent may report. They are never charted,
# so they get their own short allowlist rather than joining EXTRA_METRICS.
CONTAINER_FIELDS = ("state", "role")
MAX_CONTAINER_TEXT = 64

# Structural facts about a guest, not samples: a vmid and a core count identify
# the box and never move, so they are stored and shown but never charted.
GUEST_INT_FIELDS = ("vmid", "vcpu")
MAX_GUEST_INT = 100_000


# What each pve guest runs. This is a deployment fact like DOCKER_CONTAINERS,
# not a sample: pve reports CPU, RAM and disk, but nothing about the job a
# container does. It lives here rather than on the host so the page is right
# even when the installed host agent is older than the dashboard. A `role` an
# agent does report wins over this table.
LXC_ROLES = {
    # CT 210 was renamed agent -> t3. Both keys name the same guest so the
    # label survives the cutover; "agent" can go once the rename has landed.
    "t3": "T3 Code",
    "agent": "T3 Code",
    "hermes": "Hermes",
}


def guest_role(kind, name):
    """What a pve guest runs, when it is one this dashboard knows."""
    if kind != "lxc-guest":
        return None
    return LXC_ROLES.get(name or "")


def clean_text(value, limit=MAX_CONTAINER_TEXT):
    """Trim an agent string into something safe to re-serve to every browser."""
    if not isinstance(value, str):
        return None
    value = "".join(ch for ch in value if ch.isprintable()).strip()[:limit]
    return value or None


def stack_topology(devices):
    """Describe where this dashboard runs: pve -> docker LXC -> Docker.

    The page itself is served by the dash container through caddy, so both are
    running whenever this payload can be read. Their names and roles are
    static. Any device an agent reports with kind "container" is appended on
    top, carrying its own state, so the Docker block is not a standing claim
    that every container is up.
    """
    host = next(
        (d for d in devices if d.get("kind") == "host" and d.get("name") == "pve"),
        None,
    )
    if host is None:
        host = next((d for d in devices if d.get("kind") == "host"), None)
    docker_host = next((d for d in devices if d.get("id") == "docker"), None)

    containers = [dict(c) for c in DOCKER_CONTAINERS]
    known = {c["name"] for c in containers}
    for device in devices:
        if device.get("kind") != "container":
            continue
        name = device.get("name") or str(device.get("id", "")).split(":")[-1]
        if not name or name in known:
            continue
        known.add(name)
        # A stored state goes stale when the container stops reporting, so
        # freshness wins: a silent container is exited, whatever it last said.
        online = bool(device.get("online"))
        containers.append({
            "name": name,
            "role": device.get("role") or "container",
            "state": (device.get("state") if online else None)
            or ("running" if online else "exited"),
        })

    return {
        "host_id": host.get("id") if host else None,
        "docker_host_id": docker_host.get("id") if docker_host else None,
        "containers": containers if docker_host else [],
    }


# ---------- push agents ----------

def load_agents():
    try:
        with open(AGENTS_FILE) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def save_agents(agents):
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = AGENTS_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(agents, f, indent=2)
    tmp.replace(AGENTS_FILE)


# A device may legitimately have a handful of volumes (boot, data, external).
# The cap keeps a bad or hostile heartbeat from turning the rail into a list of
# a thousand mounts; the page has no room for them anyway.
MAX_DISKS = 16

# Extra signals use one generic table and one allowlist, so adding a metric
# needs no schema migration. Unknown keys are dropped, never stored.
EXTRA_METRICS = (
    "load_1", "load_5", "load_15",
    "swap_used", "swap_total",
    "net_rx_bps", "net_tx_bps",
    "cpu_iowait", "cpu_steal",
)
PERCENT_METRICS = {"cpu_iowait", "cpu_steal"}
# A compact strip does not need a year of history; cap the series query.
SIGNAL_WINDOW_S = 86400

ANNOTATION_KINDS = ("deploy", "restart", "backup", "note")
MAX_ANNOTATION_TEXT = 200
MAX_ANNOTATIONS = 200
# A flapping device can restart every sample. Markers are for humans, so only
# the most recent auto restarts are sent; the Events rail keeps its own limit.
MAX_AUTO_ANNOTATIONS = 20

# Curated warning thresholds. Each warning has a warning and a derived
# critical level; the env vars move the warning line, not the whole rule.
WARN_DISK_PCT = float(os.environ.get("DASH_WARN_DISK_PCT", "85"))
WARN_DAYS_TO_90 = float(os.environ.get("DASH_WARN_DAYS_TO_90", "14"))
WARN_GROWTH = float(os.environ.get("DASH_WARN_GROWTH", "1.0"))
WARN_OFFLINE_S = int(os.environ.get("DASH_WARN_OFFLINE_S", "600"))
WARN_MEM_PCT = float(os.environ.get("DASH_WARN_MEM_PCT", "90"))
# A certificate is a warning inside two weeks and critical inside one.
WARN_CERT_DAYS = float(os.environ.get("DASH_WARN_CERT_DAYS", "14"))
WARN_ZFS_SCRUB_DAYS = float(os.environ.get("DASH_WARN_ZFS_SCRUB_DAYS", "30"))
WARN_BACKUP_HOURS = float(os.environ.get("DASH_WARN_BACKUP_HOURS", "36"))
WARN_UPS_CHARGE_PCT = float(os.environ.get("DASH_WARN_UPS_CHARGE_PCT", "30"))
WARN_UPS_RUNTIME_S = int(os.environ.get("DASH_WARN_UPS_RUNTIME_S", "300"))

# The brief answers "what happened since I last looked?", so it offers only the
# short windows a person actually returns from. It is capped: a list longer than
# one screen stops being a summary.
BRIEF_WINDOWS = ("1h", "6h", "24h", "7d")
BRIEF_MAX_ITEMS = 20
# A move that crosses no configured warning line is still worth an info line
# once the volume has moved this many percentage points over the window.
BRIEF_DISK_JUMP_PCT = 1.0
# Availability is compared with the previous equal-length window; smaller dips
# are sampling noise, not news.
BRIEF_AVAILABILITY_DROP_PCT = 5.0
BRIEF_EVENT_KINDS = ("offline", "restart", "annotation")


def _finite(value):
    """Return a real number or None. JSON can carry NaN/Infinity, and bools
    are ints in Python, so both have to be rejected explicitly."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def clean_disks(raw):
    """Validate the per-volume list an agent sends.

    Agents are token-authenticated, but this is still untrusted input: a bad
    entry must be dropped rather than stored and re-served to every browser.
    """
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw[:MAX_DISKS]:
        if not isinstance(item, dict):
            continue
        mount = item.get("mount")
        if not isinstance(mount, str):
            continue
        mount = mount.strip()[:64]
        if not mount:
            continue
        total = _finite(item.get("total"))
        used = _finite(item.get("used"))
        pct = _finite(item.get("pct"))
        if total is None or total <= 0:
            continue
        if used is None and pct is not None:
            used = pct / 100.0 * total
        if pct is None and used is not None:
            pct = 100.0 * used / total
        if used is None or pct is None:
            continue
        label = item.get("label")
        label = label.strip()[:32] if isinstance(label, str) else ""
        out.append({
            "mount": mount,
            "label": label,
            "total": int(total),
            "used": int(max(0.0, min(used, total))),
            "pct": round(max(0.0, min(100.0, pct)), 1),
        })
    return out


def aggregate_disk_pct(disks, fallback):
    """The DSK cell summarises the whole machine: used over total across every
    volume, not the fullest one. An agent that reports no readable volume keeps
    its own single number."""
    total = used = 0.0
    for disk in disks if isinstance(disks, list) else []:
        if not isinstance(disk, dict):
            continue
        size = _finite(disk.get("total"))
        if size is None or size <= 0:
            continue
        total += size
        used += _finite(disk.get("used")) or 0.0
    if total > 0:
        return round(100.0 * min(used, total) / total, 1)
    return fallback


def fullest_volume(device):
    """(pct, label) for the volume a disk warning should name, or None.

    Only a multi-volume device names one: with a single drive the machine
    gauge is that drive, so the generic "Disk n% full" wording stays. With
    several, averaging can hide a full drive, and that drive is what a person
    has to clear. Callers carry the list in different shapes: the rail has the
    full `disks`, the fleet summary a compact `{count, fullest}`, and the
    single-device view the full `volumes` series.
    """
    entries = device.get("disks")
    count = len(entries) if isinstance(entries, list) else 0
    if not count:
        volumes = device.get("volumes")
        if isinstance(volumes, list):
            entries, count = volumes, len(volumes)
        elif isinstance(volumes, dict) and isinstance(volumes.get("fullest"), dict):
            entries = [volumes["fullest"]]
            count = int(_finite(volumes.get("count")) or len(entries))
        else:
            entries = []
    if count < 2:
        return None
    sized = [entry for entry in entries
             if isinstance(entry, dict) and _finite(entry.get("pct")) is not None]
    if not sized:
        return None
    best = max(sized, key=lambda entry: entry["pct"])
    name = best.get("label") or best.get("mount") or ""
    return _finite(best.get("pct")), name


# ---------- failure-mode health ----------
# SMART, ZFS, UPS, backup freshness and update state arrive as optional
# blocks. Like disks, they are untrusted input from a token holder: every
# entry is validated and clamped, and a malformed block is omitted rather
# than stored and re-served. An agent that sends none of these keeps working.
MAX_HEALTH_ITEMS = 16
MAX_HEALTH_COUNT = 1_000_000_000
# The metrics page reads the latest block plus a short snapshot history; a
# device does not need a year of SMART temperatures on the wire.
HEALTH_HISTORY_LIMIT = 24
HEALTH_KINDS = ("smart", "zfs", "ups", "backup", "updates")


def _health_count(value):
    """A non-negative integer counter, clamped, or None. bool is not a count."""
    number = _metric_number(value, int)
    if number is None or number < 0:
        return None
    return min(number, MAX_HEALTH_COUNT)


def _health_percent(value):
    number = _metric_number(value)
    if number is None or number < 0:
        return None
    return round(min(number, 100.0), 1)


def clean_smart(raw):
    """Validate the optional SMART list; cap it and drop unusable entries."""
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw[:MAX_HEALTH_ITEMS]:
        if not isinstance(item, dict):
            continue
        device = clean_text(item.get("device"), 64)
        if not device:
            continue
        entry = {"device": device}
        health = clean_text(item.get("health"), 32)
        if health:
            entry["health"] = health
        temperature = _metric_number(item.get("temperature_c"))
        if temperature is not None and temperature >= 0:
            entry["temperature_c"] = round(min(temperature, 200.0), 1)
        for key in ("reallocated", "pending", "power_on_hours"):
            value = _health_count(item.get(key))
            if value is not None:
                entry[key] = value
        out.append(entry)
    return out


def clean_zfs(raw):
    """Validate the optional ZFS pool list; cap it and drop unusable entries."""
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw[:MAX_HEALTH_ITEMS]:
        if not isinstance(item, dict):
            continue
        pool = clean_text(item.get("pool"), 64)
        if not pool:
            continue
        entry = {"pool": pool}
        state = clean_text(item.get("state"), 32)
        if state:
            entry["state"] = state
        capacity = _health_percent(item.get("capacity_pct"))
        if capacity is not None:
            entry["capacity_pct"] = capacity
        scrub = _health_count(item.get("scrub_age_s"))
        if scrub is not None:
            entry["scrub_age_s"] = scrub
        errors = _health_count(item.get("errors"))
        if errors is not None:
            entry["errors"] = errors
        out.append(entry)
    return out


def clean_ups(raw):
    """Validate the optional UPS object, or None when it carries no reading."""
    if not isinstance(raw, dict):
        return None
    entry = {}
    status = clean_text(raw.get("status"), 32)
    if status:
        entry["status"] = status
    for key in ("charge_pct", "load_pct"):
        value = _health_percent(raw.get(key))
        if value is not None:
            entry[key] = value
    runtime = _health_count(raw.get("runtime_s"))
    if runtime is not None:
        entry["runtime_s"] = runtime
    if isinstance(raw.get("battery_low"), bool):
        entry["battery_low"] = raw["battery_low"]
    return entry or None


def clean_backup(raw):
    """A backup is only useful with an absolute success timestamp."""
    if not isinstance(raw, dict):
        return None
    ts = _metric_number(raw.get("last_success_ts"), int)
    if ts is None or ts <= 0:
        return None
    entry = {"last_success_ts": ts}
    tool = clean_text(raw.get("tool"), 32)
    if tool:
        entry["tool"] = tool
    return entry


def clean_updates(raw):
    """Validate the optional update object, or None when it carries nothing."""
    if not isinstance(raw, dict):
        return None
    entry = {}
    for key in ("pending", "security"):
        value = _health_count(raw.get(key))
        if value is not None:
            entry[key] = value
    if isinstance(raw.get("reboot_required"), bool):
        entry["reboot_required"] = raw["reboot_required"]
    return entry or None


def clean_health(raw):
    """The optional failure-mode blocks, cleaned; absent kinds stay absent."""
    if not isinstance(raw, dict):
        return {}
    out = {}
    for kind in HEALTH_KINDS:
        if kind not in raw:
            continue
        if kind in ("smart", "zfs"):
            cleaned = clean_smart(raw[kind]) if kind == "smart" else clean_zfs(raw[kind])
        elif kind == "ups":
            cleaned = clean_ups(raw[kind])
        elif kind == "backup":
            cleaned = clean_backup(raw[kind])
        else:
            cleaned = clean_updates(raw[kind])
        if cleaned:
            out[kind] = cleaned
    return out


def agent_devices():
    now = time.time()
    out = []
    for name, rec in load_agents().items():
        age = now - rec.get("last_seen", 0)
        disks = rec.get("disks")
        disks = disks if isinstance(disks, list) else []
        health = clean_health(rec.get("health"))
        role = rec.get("role")
        if not role:
            role = guest_role(rec.get("kind"), rec.get("name") or name)
        out.append({
            "id": f"agent:{name}",
            "name": rec.get("name", name),
            "kind": rec.get("kind", "pc"),
            "source": "agent",
            "cpu_pct": rec.get("cpu_pct"),
            "mem_pct": rec.get("mem_pct"),
            "mem_used": rec.get("mem_used"),
            "mem_total": rec.get("mem_total"),
            "disk_pct": aggregate_disk_pct(disks, rec.get("disk_pct")),
            "disks": disks,
            "uptime_s": rec.get("uptime_s"),
            **{metric: rec.get(metric) for metric in EXTRA_METRICS
               if metric in rec},
            **{key: rec[key] for key in CONTAINER_FIELDS
               if key in rec and key != "role"},
            **({"role": role} if role else {}),
            **{key: rec[key] for key in GUEST_INT_FIELDS if key in rec},
            **({"health": health} if health else {}),
            "online": age < 150,  # agents push every 60 s; allow two misses
            "last_seen": int(rec.get("last_seen", 0)),
        })
    return out


# ---------- service checks ----------
# A check is an HTTP probe with an expected status and/or body. The store is a
# JSON file beside bookmarks.json so a homelab owner edits it without a build
# step or a restart; the sampler re-reads it every pass. Every entry is
# normalised to the same shape here, and one bad entry is dropped rather than
# taking the API down.
DEFAULT_STATUS_RANGES = ((200, 299),)
FAILING_VERDICTS = ("service_down", "path_down", "host_down")
DEFAULT_CHECK_TIMEOUT = 5.0
MAX_CHECK_TIMEOUT = 60.0
MIN_CHECK_INTERVAL = 10
MAX_CHECK_INTERVAL = 86400
MAX_CHECK_URL = 2048
MAX_CHECK_BODY = 500

_STATUS_CLASS = re.compile(r"([1-5])xx")
_STATUS_RANGE = re.compile(r"(\d{3})\s*-\s*(\d{3})")
_STATUS_CODE = re.compile(r"\d{3}")


def _status_ranges(value):
    """Parse `200`, `2xx`, `200-299` or a list of those into inclusive ranges.

    Invalid input returns None so the caller can fall back to the 2xx default;
    a config typo must never turn into an impossible expectation.
    """
    items = value if isinstance(value, list) else [value]
    ranges = []
    for item in items:
        if isinstance(item, bool):
            return None
        if isinstance(item, int):
            if 100 <= item <= 599:
                ranges.append((item, item))
            else:
                return None
            continue
        if not isinstance(item, str):
            return None
        text = item.strip().lower().replace(" ", "")
        if not text:
            return None
        match = _STATUS_CLASS.fullmatch(text)
        if match:
            base = int(match.group(1)) * 100
            ranges.append((base, base + 99))
            continue
        match = _STATUS_RANGE.fullmatch(text)
        if match:
            low, high = int(match.group(1)), int(match.group(2))
            if low > high or low < 100 or high > 599:
                return None
            ranges.append((low, high))
            continue
        if _STATUS_CODE.fullmatch(text):
            code = int(text)
            if 100 <= code <= 599:
                ranges.append((code, code))
                continue
        return None
    return tuple(sorted(set(ranges))) or None


def _normalise_check(raw):
    """One stored entry in the shape the probe and API share, or None."""
    if not isinstance(raw, dict):
        return None
    check_id = raw.get("id")
    if not isinstance(check_id, str) or not check_id.strip():
        return None
    check_id = check_id.strip()[:80]
    url = raw.get("url")
    if not isinstance(url, str):
        return None
    url = url.strip()[:MAX_CHECK_URL]
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    name = raw.get("name")
    name = name.strip()[:80] if isinstance(name, str) and name.strip() else check_id

    method = raw.get("method")
    method = method.upper() if isinstance(method, str) else "GET"
    if method not in ("GET", "HEAD"):
        # A config typo must not issue a mutating request.
        method = "GET"

    expect_body = raw.get("expect_body")
    expect_body = expect_body[:MAX_CHECK_BODY] if isinstance(
        expect_body, str) and expect_body else None

    timeout = _finite(raw.get("timeout_s"))
    if timeout is None or timeout <= 0:
        timeout = DEFAULT_CHECK_TIMEOUT
    else:
        timeout = min(MAX_CHECK_TIMEOUT, max(1.0, timeout))

    interval = _finite(raw.get("interval_s"))
    if interval is None or interval <= 0:
        interval = None
    else:
        interval = int(min(MAX_CHECK_INTERVAL, max(MIN_CHECK_INTERVAL, interval)))

    device_id = raw.get("device_id")
    device_id = device_id.strip()[:80] if isinstance(device_id, str) else ""
    return {
        "id": check_id,
        "name": name,
        "url": url,
        "method": method,
        "status_ranges": _status_ranges(raw.get("expect_status"))
        or DEFAULT_STATUS_RANGES,
        "expect_body": expect_body,
        "timeout_s": float(timeout),
        "interval_s": interval,
        "device_id": device_id or None,
        "external": raw.get("external") is True,
    }


def load_checks():
    """Read and normalise the check store; a missing or corrupt file is empty."""
    try:
        with open(CHECKS_FILE) as f:
            data = json.load(f)
    except Exception:  # noqa: BLE001 - an absent or broken store means no checks
        return []
    raw = data.get("checks") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        return []
    checks, seen = [], set()
    for item in raw:
        check = _normalise_check(item)
        if check is None or check["id"] in seen:
            continue
        seen.add(check["id"])
        checks.append(check)
    return checks


# A probe reads at most this much of a body: enough for an expectation, never
# enough for a large page to turn one slow check into a long read.
MAX_PROBE_BODY = 65536


def _cert_not_after(cert):
    """Epoch seconds from ssl's `notAfter` text, or None if it cannot be read."""
    if not isinstance(cert, dict):
        return None
    raw = cert.get("notAfter")
    if not isinstance(raw, str):
        return None
    try:
        return ssl.cert_time_to_seconds(raw)
    except (ValueError, OverflowError):
        return None


class _CertHTTPSConnection(http.client.HTTPSConnection):
    """An HTTPS connection that keeps the peer certificate for the probe.

    urllib does not expose the socket after the fact, so the connection that
    actually negotiated TLS records `notAfter` and attaches itself to the
    response. Redirects create a new connection, and the final response is the
    one that carries the certificate the user will see.
    """

    cert_not_after = None

    def connect(self):
        super().connect()
        try:
            self.cert_not_after = _cert_not_after(self.sock.getpeercert())
        except (AttributeError, ValueError):
            self.cert_not_after = None

    def getresponse(self):
        response = super().getresponse()
        response.dash_connection = self
        return response


class _CertHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_CertHTTPSConnection, req, context=self._context)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Report the status a service sent instead of following it.

    A probe is an observation: following a redirect would hide the 3xx a
    check may be expecting, and would report the final page's status as if
    the configured URL had answered it.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_PROBE_OPENER = urllib.request.build_opener(
    _CertHTTPSHandler(context=ssl.create_default_context()),
    _NoRedirect(),
)


def _cert_from_response(response):
    """Peer `notAfter` from a response our HTTPS connection annotated."""
    connection = getattr(response, "dash_connection", None)
    return getattr(connection, "cert_not_after", None)


def _urllib_transport(url, method, timeout):
    """Perform one probe: `(status, body, cert_not_after)`, raising on failure.

    HTTP error statuses are responses, not transport failures, so they are
    returned like any other status; the caller decides whether they match.
    urllib's timeout bounds each blocking operation, and the body read is
    capped; the sibling thread is what keeps a trickling server away from
    device sampling.
    """
    request = urllib.request.Request(url, method=method)
    try:
        with _PROBE_OPENER.open(request, timeout=timeout) as response:
            body = response.read(MAX_PROBE_BODY)
            return response.status, body, _cert_from_response(response)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(MAX_PROBE_BODY)
        except Exception:  # noqa: BLE001 - the status is still the answer
            body = b""
        # `HTTPError.fp` is the response our connection annotated, so a 5xx
        # from an https service still carries its certificate lifetime.
        return exc.code, body, _cert_from_response(exc.fp)


def _status_matches(ranges, status):
    return status is not None and any(
        low <= status <= high for low, high in ranges
    )


def _probe_error_text(exc):
    """One line of failure detail, safe to store and re-serve."""
    text = f"{type(exc).__name__}: {exc}".replace("\n", " ").strip()
    return text[:200] or type(exc).__name__


def probe_check(check, now=None, transport=None):
    """Probe one check and return a sample-shaped result.

    `transport` is the seam tests use instead of a real network; it returns
    `(status, body, cert_not_after)` and raises on a connection failure.
    """
    now = time.time() if now is None else now
    transport = transport or _urllib_transport
    started = time.monotonic()
    status, body, not_after = None, b"", None
    error = None
    try:
        status, body, not_after = transport(
            check["url"], check["method"], check["timeout_s"]
        )
    except Exception as exc:  # noqa: BLE001 - a dead URL is a sample, not a crash
        error = _probe_error_text(exc)
    latency_ms = int(round((time.monotonic() - started) * 1000))

    ok = False
    if error is None:
        ok = _status_matches(check["status_ranges"], status)
        if not ok:
            error = f"HTTP {status}"
        elif check["expect_body"]:
            text = body.decode("utf-8", "replace") if isinstance(
                body, bytes) else str(body)
            if check["expect_body"] not in text:
                ok = False
                error = f"body did not contain {check['expect_body']!r}"

    cert_days = (
        None if not_after is None
        else round((float(not_after) - now) / 86400.0, 1)
    )
    return {
        "id": check["id"],
        "ok": ok,
        "status": status,
        "latency_ms": latency_ms,
        "cert_days": cert_days,
        "error": error,
    }


def _device_online(live, device_id):
    """Whether a tied device is online, or None when it is not known."""
    if not device_id:
        return None
    state = live.get(device_id)
    return bool(state.get("online")) if state else None


def check_verdict(check, ok, device_online):
    """Classify a check for the launcher and the Services card.

    Precedence is deliberate: no sample at all is unknown; a tied device that
    is known offline is host_down even if its last probe passed; then a passing
    sample is ok, an external failure is path_down and every other failure is
    service_down. An unknown device_id falls through to service_down because
    the server cannot honestly claim the host is offline.
    """
    if ok is None:
        return "unknown"
    if check.get("device_id") and device_online is False:
        return "host_down"
    if ok:
        return "ok"
    if check.get("external"):
        return "path_down"
    return "service_down"


def _last_change_ts(con, check_id, ok):
    """Bucket ts at which the current run of `ok` began.

    MAX(ts) of the opposite state plus the first row after it gives the
    transition without scanning the series; when the state never changed, the
    first stored sample is the start.
    """
    previous = con.execute(
        "SELECT MAX(ts) FROM check_samples WHERE check_id = ? AND ok != ?",
        (check_id, int(ok)),
    ).fetchone()[0]
    if previous is None:
        first = con.execute(
            "SELECT MIN(ts) FROM check_samples WHERE check_id = ?", (check_id,)
        ).fetchone()[0]
        return int(first) if first is not None else None
    following = con.execute(
        "SELECT MIN(ts) FROM check_samples WHERE check_id = ? AND ts > ?",
        (check_id, previous),
    ).fetchone()[0]
    return int(following) if following is not None else None


def _uptime_share(con, check_id, since):
    row = con.execute(
        "SELECT AVG(ok) FROM check_samples WHERE check_id = ? AND ts >= ?",
        (check_id, int(since)),
    ).fetchone()
    return round(100.0 * row[0], 1) if row[0] is not None else None


def _check_entry(con, check, live, now):
    latest = None
    if con is not None:
        latest = con.execute(
            "SELECT ok, status, latency_ms, cert_days, error, ts "
            "FROM check_samples WHERE check_id = ? ORDER BY ts DESC LIMIT 1",
            (check["id"],),
        ).fetchone()
    entry = {
        "id": check["id"],
        "name": check["name"],
        "url": check["url"],
        "device_id": check["device_id"],
        "ok": None,
        "status": None,
        "latency_ms": None,
        "cert_days": None,
        "error": None,
        "last_change": None,
        "uptime_24h": None,
        "uptime_7d": None,
        "verdict": "unknown",
    }
    if latest is None:
        return entry
    ok, status, latency, cert_days, error, _ts = latest
    entry.update({
        "ok": bool(ok),
        "status": int(status) if status is not None else None,
        "latency_ms": int(round(latency)) if latency is not None else None,
        "cert_days": round(float(cert_days), 1) if cert_days is not None else None,
        "error": error,
        "last_change": _last_change_ts(con, check["id"], ok),
        "uptime_24h": _uptime_share(con, check["id"], now - 86400),
        "uptime_7d": _uptime_share(con, check["id"], now - 7 * 86400),
    })
    entry["verdict"] = check_verdict(
        check, entry["ok"], _device_online(live, check["device_id"])
    )
    return entry


def checks_payload(now=None):
    """Per-check current state, uptimes and verdict for the launcher."""
    now = time.time() if now is None else now
    checks = load_checks()
    payload = {"generated": int(now), "checks": []}
    if not checks:
        return payload
    live = _live_state(now)
    con = _connect_metrics() if METRICS_FILE.exists() else None
    try:
        for check in checks:
            payload["checks"].append(_check_entry(con, check, live, now))
    finally:
        if con is not None:
            con.close()
    return payload


def run_checks_once(now=None, transport=None):
    """Probe every due check once and store the samples.

    Reads the due times under `_db_lock`, probes without holding it, then
    writes through the single-writer path. `transport` is the test seam; the
    production caller uses urllib.
    """
    now = time.time() if now is None else now
    checks = load_checks()
    if not checks:
        with _lock:
            _check_state["results"] = {}
        return 0

    last_seen, latest = {}, {}
    if METRICS_FILE.exists():
        with _db_lock:
            con = _connect_metrics()
            try:
                last_seen = dict(con.execute(
                    "SELECT check_id, MAX(ts) FROM check_samples GROUP BY check_id"
                ).fetchall())
                # One row per check: the sample the verdict and warnings use
                # while the check is not due for another probe.
                latest = {
                    row[0]: row[1:]
                    for row in con.execute(
                        "SELECT c.check_id, c.ok, c.status, c.cert_days, c.error "
                        "FROM check_samples c WHERE c.ts = ("
                        "  SELECT MAX(ts) FROM check_samples "
                        "  WHERE check_id = c.check_id)"
                    ).fetchall()
                }
            finally:
                con.close()

    live = _live_state(now)
    results, carried = [], {}
    for check in checks:
        interval = check["interval_s"] or CHECKS_SECONDS
        last = last_seen.get(check["id"])
        if last is not None and now - last < interval:
            # Not due, but the state a restart lost still has to feed the
            # warning surface, and the verdict still follows the live host.
            sample = latest.get(check["id"])
            if sample is not None:
                ok, status, cert_days, error = sample
                carried[check["id"]] = {
                    "name": check["name"],
                    "ok": bool(ok),
                    "status": status,
                    "cert_days": cert_days,
                    "error": error,
                    "verdict": check_verdict(
                        check, bool(ok), _device_online(live, check["device_id"])
                    ),
                    "ts": int(last),
                }
            continue
        result = probe_check(check, now=now, transport=transport)
        result["name"] = check["name"]
        result["device_id"] = check["device_id"]
        result["verdict"] = check_verdict(
            check, result["ok"], _device_online(live, check["device_id"])
        )
        results.append(result)

    if results:
        record_check_samples(results, now=now)
        prune_metrics(now)

    known = {check["id"] for check in checks}
    with _lock:
        state = {
            check_id: result
            for check_id, result in _check_state["results"].items()
            if check_id in known
        }
        state.update(carried)
        for result in results:
            state[result["id"]] = {
                "name": result["name"],
                "ok": result["ok"],
                "status": result["status"],
                "cert_days": result["cert_days"],
                "error": result["error"],
                "verdict": result["verdict"],
                "ts": int(now),
            }
        _check_state["results"] = state
    return len(results)


def checks_loop(interval=None):
    """Probe checks forever on its own thread, never delaying device sampling."""
    interval = CHECKS_SECONDS if interval is None else max(1, interval)
    while True:
        started = time.time()
        try:
            run_checks_once()
        except Exception as exc:  # noqa: BLE001 - a probe bug must not kill the API
            print(f"service checks failed: {type(exc).__name__}: {exc}", flush=True)
        time.sleep(max(1.0, interval - (time.time() - started)))


def start_checks(interval=None):
    thread = threading.Thread(target=checks_loop, args=(interval,), daemon=True,
                              name="service-checks")
    thread.start()
    return thread


# ---------- metrics history ----------
# SQLite is stdlib and a single file on the same writable data mount as the
# bookmark and click stores. The whole JSON stores are rewritten per write,
# which is fine for occasional clicks but not for a sample every minute.

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    kind       TEXT NOT NULL,
    first_seen INTEGER NOT NULL,
    last_seen  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS samples (
    device_id TEXT NOT NULL,
    ts        INTEGER NOT NULL,
    cpu_pct   REAL,
    mem_pct   REAL,
    mem_used  INTEGER,
    mem_total INTEGER,
    disk_pct  REAL,
    uptime_s  INTEGER,
    online    INTEGER NOT NULL,
    PRIMARY KEY (device_id, ts)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS samples_ts ON samples (ts);

-- One row per device per mount per bucket. The aggregate disk_pct on
-- samples keeps its old shape; this table is what makes "which volume is
-- filling up?" answerable. A repeat inside a bucket replaces the bucket's
-- rows for that device, so history never double-counts a heartbeat.
CREATE TABLE IF NOT EXISTS disk_samples (
    device_id TEXT NOT NULL,
    mount     TEXT NOT NULL,
    ts        INTEGER NOT NULL,
    label     TEXT,
    total     INTEGER,
    used      INTEGER,
    pct       REAL,
    PRIMARY KEY (device_id, mount, ts)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS disk_samples_ts ON disk_samples (ts);

-- Pre-aggregated tiers. The sampler recomputes the current bucket after each
-- write, so these are ordinary derived tables: deleting them and restarting
-- loses long history but never corrupts the live path. Averages are stored
-- with their sample count so a coarser step can be re-weighted exactly.
CREATE TABLE IF NOT EXISTS rollup_5m (
    device_id      TEXT NOT NULL,
    ts             INTEGER NOT NULL,
    samples        INTEGER NOT NULL,
    online_samples INTEGER NOT NULL,
    cpu_avg        REAL,
    cpu_min        REAL,
    cpu_max        REAL,
    mem_avg        REAL,
    mem_min        REAL,
    mem_max        REAL,
    disk_avg       REAL,
    disk_min       REAL,
    disk_max       REAL,
    uptime_first   INTEGER,
    uptime_last    INTEGER,
    uptime_min     INTEGER,
    PRIMARY KEY (device_id, ts)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS rollup_5m_ts ON rollup_5m (ts);

CREATE TABLE IF NOT EXISTS rollup_1h (
    device_id      TEXT NOT NULL,
    ts             INTEGER NOT NULL,
    samples        INTEGER NOT NULL,
    online_samples INTEGER NOT NULL,
    cpu_avg        REAL,
    cpu_min        REAL,
    cpu_max        REAL,
    mem_avg        REAL,
    mem_min        REAL,
    mem_max        REAL,
    disk_avg       REAL,
    disk_min       REAL,
    disk_max       REAL,
    uptime_first   INTEGER,
    uptime_last    INTEGER,
    uptime_min     INTEGER,
    PRIMARY KEY (device_id, ts)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS rollup_1h_ts ON rollup_1h (ts);

CREATE TABLE IF NOT EXISTS disk_rollup_5m (
    device_id TEXT NOT NULL,
    mount     TEXT NOT NULL,
    ts        INTEGER NOT NULL,
    samples   INTEGER NOT NULL,
    pct_avg   REAL,
    pct_min   REAL,
    pct_max   REAL,
    label     TEXT,
    total     INTEGER,
    used      INTEGER,
    PRIMARY KEY (device_id, mount, ts)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS disk_rollup_5m_ts ON disk_rollup_5m (ts);

CREATE TABLE IF NOT EXISTS disk_rollup_1h (
    device_id TEXT NOT NULL,
    mount     TEXT NOT NULL,
    ts        INTEGER NOT NULL,
    samples   INTEGER NOT NULL,
    pct_avg   REAL,
    pct_min   REAL,
    pct_max   REAL,
    label     TEXT,
    total     INTEGER,
    used      INTEGER,
    PRIMARY KEY (device_id, mount, ts)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS disk_rollup_1h_ts ON disk_rollup_1h (ts);

-- Generic extra signals (load, swap bytes, network rates, CPU pressure).
-- The allowlist lives in EXTRA_METRICS; an existing agent that sends none of
-- these simply has no rows and the API omits the signal rather than zeroing it.
CREATE TABLE IF NOT EXISTS metric_samples (
    device_id TEXT NOT NULL,
    metric    TEXT NOT NULL,
    ts        INTEGER NOT NULL,
    value     REAL,
    PRIMARY KEY (device_id, metric, ts)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS metric_samples_ts ON metric_samples (ts);

-- One row per check per bucket. The sampler replaces a repeat inside a bucket,
-- so a probe retry never double-counts uptime. cert_days is the peer
-- certificate's remaining lifetime for an https probe, NULL for http.
CREATE TABLE IF NOT EXISTS check_samples (
    check_id   TEXT NOT NULL,
    ts         INTEGER NOT NULL,
    ok         INTEGER NOT NULL,
    status     INTEGER,
    latency_ms REAL,
    cert_days  REAL,
    error      TEXT,
    PRIMARY KEY (check_id, ts)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS check_samples_ts ON check_samples (ts);

-- Failure-mode telemetry (SMART, ZFS, UPS, backup freshness, updates). One
-- row per device per kind per bucket; a repeat heartbeat replaces the rows it
-- touches, exactly like disk_samples. payload_json is the cleaned block, so an
-- old agent has no rows and the API omits the block rather than inventing one.
CREATE TABLE IF NOT EXISTS device_health (
    device_id    TEXT NOT NULL,
    ts           INTEGER NOT NULL,
    kind         TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (device_id, kind, ts)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS device_health_ts ON device_health (ts);

-- Manual deploy/restart/backup/note annotations. device_id = '' means
-- fleet-wide, so a deploy note appears on every device chart.
CREATE TABLE IF NOT EXISTS annotations (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    ts        INTEGER NOT NULL,
    kind      TEXT NOT NULL,
    text      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS annotations_ts ON annotations (ts);
"""


def _connect_metrics():
    DATA.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(METRICS_FILE), timeout=5)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=5000")
    # Idempotent: also repairs a zero-byte or half-created file after a crash.
    con.executescript(SCHEMA)
    return con


def _metric_number(value, cast=float):
    """A finite number from an agent payload, or None. bool is not a sample."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return cast(value)


def _extra_metric_value(metric, value):
    """Validate one generic signal. Negative rates/loads are meaningless, and
    a CPU percentage above 100 is clamped rather than stored as-is."""
    value = _metric_number(value)
    if value is None or value < 0:
        return None
    if metric in PERCENT_METRICS:
        value = min(value, 100.0)
    return value


def _device_identity(device):
    raw_id = device.get("id") or device.get("name") or "unknown"
    raw_name = device.get("name") or raw_id
    return str(raw_id)[:80], str(raw_name)[:80], str(device.get("kind") or "other")[:40]


def record_samples(devices, now=None):
    """Write one bucketed row per device, replacing a duplicate in the bucket."""
    now = time.time() if now is None else now
    ts = int(now) - (int(now) % SAMPLE_SECONDS)
    wrote = 0
    with _db_lock:
        con = _connect_metrics()
        try:
            for device in devices:
                device_id, name, kind = _device_identity(device)
                online = 1 if device.get("online") else 0
                con.execute(
                    "INSERT INTO devices (id, name, kind, first_seen, last_seen) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET "
                    "name = excluded.name, kind = excluded.kind, "
                    "last_seen = MAX(devices.last_seen, excluded.last_seen)",
                    (device_id, name, kind, ts, ts),
                )
                con.execute(
                    "INSERT INTO samples (device_id, ts, cpu_pct, mem_pct, mem_used, "
                    "mem_total, disk_pct, uptime_s, online) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(device_id, ts) DO UPDATE SET "
                    "cpu_pct = excluded.cpu_pct, mem_pct = excluded.mem_pct, "
                    "mem_used = excluded.mem_used, mem_total = excluded.mem_total, "
                    "disk_pct = excluded.disk_pct, uptime_s = excluded.uptime_s, "
                    "online = excluded.online",
                    (
                        device_id, ts,
                        _metric_number(device.get("cpu_pct")),
                        _metric_number(device.get("mem_pct")),
                        _metric_number(device.get("mem_used"), int),
                        _metric_number(device.get("mem_total"), int),
                        _metric_number(device.get("disk_pct")),
                        _metric_number(device.get("uptime_s"), int),
                        online,
                    ),
                )
                # Per-volume history shares the bucket timestamp and the same
                # transaction, so a volume set can never be half-written. The
                # delete makes a heartbeat the source of truth for its bucket:
                # a mount that vanished does not linger, and a repeat replaces
                # rather than appends. clean_disks already caps and validates.
                con.execute(
                    "DELETE FROM disk_samples WHERE device_id = ? AND ts = ?",
                    (device_id, ts),
                )
                for disk in clean_disks(device.get("disks")):
                    con.execute(
                        "INSERT INTO disk_samples "
                        "(device_id, mount, ts, label, total, used, pct) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (device_id, disk["mount"], ts, disk["label"],
                         disk["total"], disk["used"], disk["pct"]),
                    )
                # Generic signals share the bucket and the replacement rule.
                # Only present, valid values are written; an absent metric is
                # omitted from the API rather than rendered as zero.
                con.execute(
                    "DELETE FROM metric_samples WHERE device_id = ? AND ts = ?",
                    (device_id, ts),
                )
                signals = {
                    metric: _extra_metric_value(metric, device.get(metric))
                    for metric in EXTRA_METRICS
                }
                if (signals.get("swap_used") is not None
                        and signals.get("swap_total") is not None):
                    signals["swap_used"] = min(
                        signals["swap_used"], signals["swap_total"]
                    )
                for metric, value in signals.items():
                    if value is not None:
                        con.execute(
                            "INSERT INTO metric_samples (device_id, metric, ts, value) "
                            "VALUES (?, ?, ?, ?)",
                            (device_id, metric, ts, value),
                        )
                # Failure-mode blocks share the bucket and the replacement
                # rule. An agent that sends none of them writes no rows; a
                # repeat heartbeat is the source of truth for its bucket.
                con.execute(
                    "DELETE FROM device_health WHERE device_id = ? AND ts = ?",
                    (device_id, ts),
                )
                for kind, block in clean_health(device.get("health")).items():
                    con.execute(
                        "INSERT INTO device_health "
                        "(device_id, ts, kind, payload_json) VALUES (?, ?, ?, ?)",
                        (device_id, ts, kind,
                         json.dumps(block, separators=(",", ":"), sort_keys=True)),
                    )
                # Recompute only the buckets this heartbeat touches. Later
                # heartbeats in the same bucket replace the row rather than
                # appending, so a rollup is always a pure function of raw rows.
                _refresh_rollups(con, device_id, ts)
                wrote += 1
            con.commit()
        finally:
            con.close()
    _invalidate_summary()
    prune_metrics(now)
    return wrote


def sample_all(now=None):
    """Sample the local device and every known agent once."""
    now = time.time() if now is None else now
    return record_samples([local_device()] + agent_devices(), now=now)


def record_check_samples(results, now=None):
    """Write one bucketed check row per result, replacing a duplicate bucket.

    This is the same single-writer path as device samples: it takes `_db_lock`
    and never holds it while waiting on a network probe.
    """
    now = time.time() if now is None else now
    ts = int(now) - (int(now) % CHECK_BUCKET_SECONDS)
    wrote = 0
    with _db_lock:
        con = _connect_metrics()
        try:
            for result in results:
                check_id = result.get("id")
                if not isinstance(check_id, str) or not check_id:
                    continue
                con.execute(
                    "INSERT INTO check_samples "
                    "(check_id, ts, ok, status, latency_ms, cert_days, error) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(check_id, ts) DO UPDATE SET "
                    "ok = excluded.ok, status = excluded.status, "
                    "latency_ms = excluded.latency_ms, "
                    "cert_days = excluded.cert_days, error = excluded.error",
                    (
                        check_id, ts, 1 if result.get("ok") else 0,
                        _metric_number(result.get("status"), int),
                        _metric_number(result.get("latency_ms")),
                        _metric_number(result.get("cert_days")),
                        clean_text(result.get("error"), 200),
                    ),
                )
                wrote += 1
            con.commit()
        finally:
            con.close()
    return wrote


def prune_metrics(now=None):
    """Drop samples outside the retention window, at most once an hour."""
    now = time.time() if now is None else now
    with _db_lock:
        if _last_prune["at"] and now - _last_prune["at"] < PRUNE_INTERVAL:
            return 0
        _last_prune["at"] = now
        if not METRICS_FILE.exists():
            return 0
        # Keep the delete on the same single-writer path as the sampler.
        con = _connect_metrics()
        try:
            removed = 0
            for table, retention in (
                ("samples", HISTORY_SECONDS),
                ("disk_samples", HISTORY_SECONDS),
                ("metric_samples", HISTORY_SECONDS),
                ("check_samples", CHECK_RETENTION_SECONDS),
                ("device_health", HISTORY_SECONDS),
                ("rollup_5m", ROLLUP_5M_RETENTION),
                ("rollup_1h", ROLLUP_1H_RETENTION),
                ("disk_rollup_5m", ROLLUP_5M_RETENTION),
                ("disk_rollup_1h", ROLLUP_1H_RETENTION),
            ):
                cur = con.execute(
                    f"DELETE FROM {table} WHERE ts < ?",  # fixed internal names
                    (int(now) - retention,),
                )
                removed += cur.rowcount
            con.commit()
            return removed
        finally:
            con.close()


# ---------- rollups ----------
# A rollup row is a pure function of the raw rows in its bucket, so the
# sampler deletes and rewrites just the touched bucket. No background job, no
# incremental drift: re-running it is always safe.

def _bucket_start(ts, size):
    return int(ts) - (int(ts) % size)


def _weighted(pairs):
    """Weighted mean of (value, weight) pairs, ignoring missing values."""
    pairs = [(value, weight) for value, weight in pairs if value is not None]
    total = sum(weight for _value, weight in pairs)
    if not total:
        return None
    return sum(value * weight for value, weight in pairs) / total


def _bounds(values):
    values = [value for value in values if value is not None]
    return (min(values), max(values)) if values else (None, None)


def _raw_aggregate_parts(rows):
    return [{
        "samples": 1,
        "online_samples": 1 if online else 0,
        "cpu_avg": cpu, "cpu_min": cpu, "cpu_max": cpu,
        "mem_avg": mem, "mem_min": mem, "mem_max": mem,
        "disk_avg": disk, "disk_min": disk, "disk_max": disk,
        "uptime_first": uptime, "uptime_last": uptime, "uptime_min": uptime,
    } for _ts, cpu, mem, disk, uptime, online in rows]


def _rollup_aggregate_parts(rows):
    return [{
        "samples": samples,
        "online_samples": online_samples,
        "cpu_avg": cpu_avg, "cpu_min": cpu_min, "cpu_max": cpu_max,
        "mem_avg": mem_avg, "mem_min": mem_min, "mem_max": mem_max,
        "disk_avg": disk_avg, "disk_min": disk_min, "disk_max": disk_max,
        "uptime_first": uptime_first, "uptime_last": uptime_last,
        "uptime_min": uptime_min,
    } for (samples, online_samples, cpu_avg, cpu_min, cpu_max,
           mem_avg, mem_min, mem_max, disk_avg, disk_min, disk_max,
           uptime_first, uptime_last, uptime_min) in rows]


def _merge_aggregate(parts):
    """Fold ordered parts into one aggregate row."""
    return {
        "samples": sum(part["samples"] for part in parts),
        "online_samples": sum(part["online_samples"] for part in parts),
        "cpu_avg": _weighted((part["cpu_avg"], part["samples"]) for part in parts),
        "cpu_min": _bounds(part["cpu_min"] for part in parts)[0],
        "cpu_max": _bounds(part["cpu_max"] for part in parts)[1],
        "mem_avg": _weighted((part["mem_avg"], part["samples"]) for part in parts),
        "mem_min": _bounds(part["mem_min"] for part in parts)[0],
        "mem_max": _bounds(part["mem_max"] for part in parts)[1],
        "disk_avg": _weighted((part["disk_avg"], part["samples"]) for part in parts),
        "disk_min": _bounds(part["disk_min"] for part in parts)[0],
        "disk_max": _bounds(part["disk_max"] for part in parts)[1],
        "uptime_first": next(
            (part["uptime_first"] for part in parts
             if part["uptime_first"] is not None), None),
        "uptime_last": next(
            (part["uptime_last"] for part in reversed(parts)
             if part["uptime_last"] is not None), None),
        "uptime_min": min(
            (part["uptime_min"] for part in parts
             if part["uptime_min"] is not None), default=None),
    }


def _merge_disk(parts):
    return {
        "samples": sum(part["samples"] for part in parts),
        "pct_avg": _weighted((part["pct_avg"], part["samples"]) for part in parts),
        "pct_min": _bounds(part["pct_min"] for part in parts)[0],
        "pct_max": _bounds(part["pct_max"] for part in parts)[1],
        "label": next(
            (part["label"] for part in reversed(parts) if part["label"]), ""),
        "total": next(
            (part["total"] for part in reversed(parts)
             if part["total"] is not None), None),
        "used": next(
            (part["used"] for part in reversed(parts)
             if part["used"] is not None), None),
    }


_AGGREGATE_COLUMNS = (
    "device_id, ts, samples, online_samples, cpu_avg, cpu_min, cpu_max, "
    "mem_avg, mem_min, mem_max, disk_avg, disk_min, disk_max, "
    "uptime_first, uptime_last, uptime_min"
)


def _write_aggregate(con, table, device_id, ts, merged):
    con.execute(
        f"INSERT INTO {table} ({_AGGREGATE_COLUMNS}) VALUES "
        "(" + ",".join("?" * 16) + ") "
        "ON CONFLICT(device_id, ts) DO UPDATE SET "
        "samples=excluded.samples, online_samples=excluded.online_samples, "
        "cpu_avg=excluded.cpu_avg, cpu_min=excluded.cpu_min, "
        "cpu_max=excluded.cpu_max, mem_avg=excluded.mem_avg, "
        "mem_min=excluded.mem_min, mem_max=excluded.mem_max, "
        "disk_avg=excluded.disk_avg, disk_min=excluded.disk_min, "
        "disk_max=excluded.disk_max, uptime_first=excluded.uptime_first, "
        "uptime_last=excluded.uptime_last, uptime_min=excluded.uptime_min",
        (device_id, int(ts), merged["samples"], merged["online_samples"],
         merged["cpu_avg"], merged["cpu_min"], merged["cpu_max"],
         merged["mem_avg"], merged["mem_min"], merged["mem_max"],
         merged["disk_avg"], merged["disk_min"], merged["disk_max"],
         merged["uptime_first"], merged["uptime_last"], merged["uptime_min"]),
    )


_DISK_COLUMNS = (
    "device_id, mount, ts, samples, pct_avg, pct_min, pct_max, label, total, used"
)


def _write_disk(con, table, device_id, mount, ts, merged):
    con.execute(
        f"INSERT INTO {table} ({_DISK_COLUMNS}) VALUES "
        "(" + ",".join("?" * 10) + ") "
        "ON CONFLICT(device_id, mount, ts) DO UPDATE SET "
        "samples=excluded.samples, pct_avg=excluded.pct_avg, "
        "pct_min=excluded.pct_min, pct_max=excluded.pct_max, "
        "label=excluded.label, total=excluded.total, used=excluded.used",
        (device_id, mount, int(ts), merged["samples"], merged["pct_avg"],
         merged["pct_min"], merged["pct_max"], merged["label"],
         merged["total"], merged["used"]),
    )


def _refresh_rollups(con, device_id, ts):
    """Recompute the 5-minute and hourly buckets that contain `ts`."""
    five = _bucket_start(ts, ROLLUP_5M_SECONDS)
    raw = con.execute(
        "SELECT ts, cpu_pct, mem_pct, disk_pct, uptime_s, online FROM samples "
        "WHERE device_id = ? AND ts >= ? AND ts < ? ORDER BY ts",
        (device_id, five, five + ROLLUP_5M_SECONDS),
    ).fetchall()
    con.execute("DELETE FROM rollup_5m WHERE device_id = ? AND ts = ?",
                (device_id, five))
    if raw:
        _write_aggregate(con, "rollup_5m", device_id, five,
                         _merge_aggregate(_raw_aggregate_parts(raw)))

    hour = _bucket_start(ts, ROLLUP_1H_SECONDS)
    parts = con.execute(
        "SELECT samples, online_samples, cpu_avg, cpu_min, cpu_max, "
        "mem_avg, mem_min, mem_max, disk_avg, disk_min, disk_max, "
        "uptime_first, uptime_last, uptime_min FROM rollup_5m "
        "WHERE device_id = ? AND ts >= ? AND ts < ? ORDER BY ts",
        (device_id, hour, hour + ROLLUP_1H_SECONDS),
    ).fetchall()
    con.execute("DELETE FROM rollup_1h WHERE device_id = ? AND ts = ?",
                (device_id, hour))
    if parts:
        _write_aggregate(con, "rollup_1h", device_id, hour,
                         _merge_aggregate(_rollup_aggregate_parts(parts)))

    # Volume rollups are online-only, matching the per-volume series. Offline
    # agents keep stale disks in agents.json and must not enter a trend.
    disk_rows = con.execute(
        "SELECT d.ts, d.mount, d.label, d.total, d.used, d.pct "
        "FROM disk_samples d "
        "JOIN samples s ON s.device_id = d.device_id AND s.ts = d.ts "
        "WHERE d.device_id = ? AND d.ts >= ? AND d.ts < ? AND s.online = 1 "
        "ORDER BY d.ts",
        (device_id, five, five + ROLLUP_5M_SECONDS),
    ).fetchall()
    by_mount = {}
    for _ts, mount, label, total, used, pct in disk_rows:
        by_mount.setdefault(mount, []).append({
            "samples": 1, "pct_avg": pct, "pct_min": pct, "pct_max": pct,
            "label": label, "total": total, "used": used,
        })
    con.execute("DELETE FROM disk_rollup_5m WHERE device_id = ? AND ts = ?",
                (device_id, five))
    for mount, parts in by_mount.items():
        _write_disk(con, "disk_rollup_5m", device_id, mount, five,
                    _merge_disk(parts))

    sub = con.execute(
        "SELECT ts, mount, samples, pct_avg, pct_min, pct_max, label, total, used "
        "FROM disk_rollup_5m WHERE device_id = ? AND ts >= ? AND ts < ? "
        "ORDER BY ts",
        (device_id, hour, hour + ROLLUP_1H_SECONDS),
    ).fetchall()
    by_mount = {}
    for _ts, mount, samples, pct_avg, pct_min, pct_max, label, total, used in sub:
        by_mount.setdefault(mount, []).append({
            "samples": samples, "pct_avg": pct_avg, "pct_min": pct_min,
            "pct_max": pct_max, "label": label, "total": total, "used": used,
        })
    con.execute("DELETE FROM disk_rollup_1h WHERE device_id = ? AND ts = ?",
                (device_id, hour))
    for mount, parts in by_mount.items():
        _write_disk(con, "disk_rollup_1h", device_id, mount, hour,
                    _merge_disk(parts))


def monitor_loop(interval=None):
    """Sample forever. The first pass runs immediately, before the first sleep."""
    interval = SAMPLE_SECONDS if interval is None else max(1, interval)
    while True:
        started = time.time()
        try:
            sample_all()
        except Exception as exc:  # noqa: BLE001 - a sampler bug must not kill the API
            print(f"metrics sampler failed: {type(exc).__name__}: {exc}", flush=True)
        time.sleep(max(1.0, interval - (time.time() - started)))


def start_monitor(interval=None):
    thread = threading.Thread(target=monitor_loop, args=(interval,), daemon=True,
                              name="metrics-monitor")
    thread.start()
    return thread


def availability_24h(now=None):
    """Mean online share per device over the last 24 h, cached for a minute."""
    now = time.time() if now is None else now
    with _db_lock:
        if _availability_cache["at"] and now - _availability_cache["at"] < 60:
            return _availability_cache["by_id"]
    if not METRICS_FILE.exists():
        return {}
    con = _connect_metrics()
    try:
        rows = con.execute(
            "SELECT device_id, AVG(online) FROM samples WHERE ts >= ? GROUP BY device_id",
            (int(now) - 86400,),
        ).fetchall()
    finally:
        con.close()
    by_id = {
        device_id: round(100.0 * share, 1)
        for device_id, share in rows
        if share is not None
    }
    with _db_lock:
        _availability_cache["at"] = now
        _availability_cache["by_id"] = by_id
    return by_id


def _live_state(now):
    """Current online flag and last-seen for every device we know about.

    The local container is always up; an agent is online when its last push is
    younger than the 150 s grace the live stats card uses. This keeps the
    history page from lagging a full sample interval behind /api/stats.
    """
    state = {
        d["id"]: {"online": bool(d["online"]), "last_seen": int(d["last_seen"])}
        for d in agent_devices()
    }
    state["docker"] = {"online": True, "last_seen": int(now)}
    return state


def _history_rows(con, device_id, since, until):
    return con.execute(
        "SELECT ts, cpu_pct, mem_pct, disk_pct, uptime_s, online FROM samples "
        "WHERE device_id = ? AND ts >= ? AND ts <= ? ORDER BY ts",
        (device_id, int(since), int(until)),
    ).fetchall()


def _events(parts):
    """Offline/recovery/restart transitions, newest first.

    A part is one sample for the raw store and one rollup bucket otherwise,
    but every part knows how many samples it holds, how many were online and
    the first and last uptime it saw. That makes the transition rules below
    one implementation for every tier. A partly online bucket counts as
    offline for transitions, matching the raw MIN(online) rule.
    """
def _part_online(part):
    """A part is online only when every sample in it was online."""
    return bool(part["samples"] and part["online_samples"] >= part["samples"])


def _events(parts):
    """Offline/recovery/restart transitions, newest first.

    A part is one sample for the raw store and one rollup bucket otherwise,
    but every part knows how many samples it holds, how many were online and
    the first and last uptime it saw. That makes the transition rules below
    one implementation for every tier. A partly online bucket counts as
    offline for transitions, matching the raw MIN(online) rule.
    """
    events = []
    prev_online = None
    offline_since = None
    prev_uptime_last = None
    for part in parts:
        ts = int(part["ts"])
        online = _part_online(part)
        if prev_online is True and not online:
            offline_since = ts
            events.append({"ts": ts, "type": "offline"})
        elif prev_online is False and online:
            events.append({
                "ts": ts, "type": "online",
                "down_s": int(ts - offline_since) if offline_since is not None else None,
            })
            offline_since = None
        elif prev_online is None and not online:
            offline_since = ts

        uptime_first = part["uptime_first"]
        uptime_last = part["uptime_last"]
        # A reboot resets uptime; a one-sample wobble should not count.
        # Compare even across an offline run: a machine that rebooted and
        # then reconnected has a lower uptime than its last pre-outage part.
        restart = False
        if online and uptime_last is not None:
            if prev_uptime_last is not None and uptime_last < prev_uptime_last - 60:
                restart = True
            if uptime_first is not None and uptime_last < uptime_first - 60:
                restart = True
        if restart:
            events.append({"ts": ts, "type": "restart"})
        if uptime_last is not None:
            prev_uptime_last = uptime_last
        prev_online = online
    events.sort(key=lambda event: event["ts"], reverse=True)
    return events


def _trend_per_day(points):
    """Least-squares slope in units per day from (ts, value) pairs.

    Fewer than three points is noise, not a trend, so it reports None rather
    than a line through two points. The same rule serves the aggregate disk
    series and each per-volume series.
    """
    points = [(ts, value) for ts, value in points if value is not None]
    if len(points) < 3:
        return None
    t0 = points[0][0]
    xs = [ts - t0 for ts, _ in points]
    ys = [value for _, value in points]
    count = len(xs)
    mean_x = sum(xs) / count
    mean_y = sum(ys) / count
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom <= 0:
        return None
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denom
    return slope * 86400


def _days_to_90(pct_now, growth):
    """Project when pct reaches 90 at the current trend, or None.

    A flat or falling volume never fills, and a volume already at or over 90
    has no meaningful countdown. This is the rule the aggregate disk gauge has
    always used; per-volume projections reuse it exactly.
    """
    if growth is None or growth <= 0.01 or pct_now is None or pct_now >= 90:
        return None
    return round((90.0 - pct_now) / growth, 1)


def _high_warning(value, warning_at, critical_at):
    if value is None:
        return None
    if value >= critical_at:
        return "critical"
    return "warning" if value >= warning_at else None


def _low_warning(value, warning_at, critical_at):
    if value is None:
        return None
    if value <= critical_at:
        return "critical"
    return "warning" if value <= warning_at else None


def _duration_label(seconds):
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    return f"{max(1, seconds // 60)}m"


def _age_label(seconds):
    """A short duration for backup age, scrub age and UPS runtime: 31d, 40h, 12m."""
    seconds = max(0, int(seconds))
    if seconds >= 3 * 86400:
        return f"{seconds // 86400}d"
    if seconds >= 3600:
        return f"{seconds // 3600}h"
    return f"{max(1, seconds // 60)}m"


def _ups_on_battery(status):
    return "OB" in status.upper().split() if isinstance(status, str) else False


def _smart_reason(disk):
    """Why this disk is failing, or None. One rule for summary and warnings."""
    health = disk.get("health")
    if isinstance(health, str) and health.strip() and health.upper() != "PASSED":
        return "health", health.strip()
    pending = _finite(disk.get("pending"))
    if pending is not None and pending > 0:
        return "pending", int(pending)
    return None


def _smart_failing(disk):
    return _smart_reason(disk) is not None


def _zfs_reason(pool):
    """Why this pool is unhealthy, or None. Scrub age is a separate warning."""
    state = pool.get("state")
    if isinstance(state, str) and state.strip() and state.upper() != "ONLINE":
        return "state", state.strip()
    errors = _finite(pool.get("errors"))
    if errors is not None and errors > 0:
        return "errors", int(errors)
    return None


def _zfs_unhealthy(pool):
    return _zfs_reason(pool) is not None


def health_warnings(health, now):
    """Warnings derived from the failure-mode blocks, in the usual shape.

    The rules are deliberately conservative: a missing reading is not a
    warning (the agent could not run the command), only a reported bad value
    is. Pending updates are ``info`` so an available package does not turn the
    rail amber.
    """
    if not isinstance(health, dict):
        return []
    warnings = []

    for disk in health.get("smart") or []:
        if not isinstance(disk, dict):
            continue
        reason = _smart_reason(disk)
        if reason is None:
            continue
        field, value = reason
        device = disk.get("device") or "disk"
        if field == "health":
            warnings.append({
                "code": "smart", "level": "critical", "value": value,
                "threshold": "PASSED", "text": f"SMART {device}: {value}",
            })
        else:
            warnings.append({
                "code": "smart", "level": "critical", "value": value,
                "threshold": 0,
                "text": f"SMART {device}: {value} pending sectors",
            })

    for pool in health.get("zfs") or []:
        if not isinstance(pool, dict):
            continue
        name = pool.get("pool") or "pool"
        reason = _zfs_reason(pool)
        if reason is not None:
            field, value = reason
            if field == "state":
                warnings.append({
                    "code": "zfs", "level": "critical", "value": value,
                    "threshold": "ONLINE", "text": f"ZFS {name}: {value}",
                })
            else:
                warnings.append({
                    "code": "zfs", "level": "critical", "value": value,
                    "threshold": 0, "text": f"ZFS {name}: {value} errors",
                })
        scrub = _finite(pool.get("scrub_age_s"))
        limit = WARN_ZFS_SCRUB_DAYS * 86400
        if scrub is not None and scrub > limit:
            warnings.append({
                "code": "zfs_scrub", "level": "warning", "value": int(scrub),
                "threshold": limit,
                "text": f"ZFS {name}: scrub {_age_label(scrub)} old",
            })

    ups = health.get("ups")
    if isinstance(ups, dict):
        status = ups.get("status")
        charge = _finite(ups.get("charge_pct"))
        runtime = _finite(ups.get("runtime_s"))
        if _ups_on_battery(status):
            warnings.append({
                "code": "ups", "level": "critical", "value": status.strip(),
                "threshold": "OL",
                "text": f"UPS on battery ({status.strip()})",
            })
        elif ups.get("battery_low") is True:
            warnings.append({
                "code": "ups", "level": "critical", "value": True,
                "threshold": False, "text": "UPS battery low",
            })
        elif charge is not None and charge < WARN_UPS_CHARGE_PCT:
            warnings.append({
                "code": "ups", "level": "critical", "value": charge,
                "threshold": WARN_UPS_CHARGE_PCT,
                "text": f"UPS charge {charge:g}%",
            })
        elif runtime is not None and runtime < WARN_UPS_RUNTIME_S:
            warnings.append({
                "code": "ups", "level": "critical", "value": int(runtime),
                "threshold": WARN_UPS_RUNTIME_S,
                "text": f"UPS runtime {_age_label(runtime)}",
            })

    backup = health.get("backup")
    if isinstance(backup, dict):
        last = _finite(backup.get("last_success_ts"))
        if last is not None:
            age = now - last
            limit = WARN_BACKUP_HOURS * 3600
            if age > limit:
                tool = backup.get("tool")
                label = f"Backup ({tool})" if isinstance(tool, str) and tool else "Backup"
                warnings.append({
                    "code": "backup", "level": "warning", "value": int(age),
                    "threshold": limit,
                    "text": f"{label} {_age_label(age)} old",
                })

    updates = health.get("updates")
    if isinstance(updates, dict):
        if updates.get("reboot_required") is True:
            warnings.append({
                "code": "reboot", "level": "warning", "value": True,
                "threshold": False, "text": "Reboot required",
            })
        pending = _finite(updates.get("pending"))
        if pending is not None and pending > 0:
            security = _finite(updates.get("security"))
            suffix = (f" · {int(security)} security"
                      if security is not None and security > 0 else "")
            warnings.append({
                "code": "updates", "level": "info", "value": int(pending),
                "threshold": 0,
                "text": f"{int(pending)} updates pending{suffix}",
            })

    return warnings


def _health_summary(health, now):
    """The compact per-device health block for the fleet summary."""
    blocks = health if isinstance(health, dict) else {}
    smart = [disk for disk in blocks.get("smart") or [] if isinstance(disk, dict)]
    zfs = [pool for pool in blocks.get("zfs") or [] if isinstance(pool, dict)]
    ups = blocks.get("ups") if isinstance(blocks.get("ups"), dict) else None
    backup = blocks.get("backup") if isinstance(blocks.get("backup"), dict) else None
    updates = blocks.get("updates") if isinstance(blocks.get("updates"), dict) else None

    warnings = health_warnings(blocks, now)
    levels = {warning["level"] for warning in warnings}
    if not any(blocks.get(kind) for kind in HEALTH_KINDS):
        status = "unknown"
    elif "critical" in levels:
        status = "critical"
    elif "warning" in levels:
        status = "warning"
    else:
        status = "ok"

    backup_age = None
    if backup is not None:
        last = _finite(backup.get("last_success_ts"))
        if last is not None:
            backup_age = max(0, int(now - last))
    charge = _finite(ups.get("charge_pct")) if ups else None
    return {
        "status": status,
        "issues": sum(1 for warning in warnings if warning["level"] != "info"),
        "smart_failing": sum(1 for disk in smart if _smart_failing(disk)),
        "zfs_unhealthy": sum(1 for pool in zfs if _zfs_unhealthy(pool)),
        "ups": ({
            "status": ups.get("status"),
            "on_battery": _ups_on_battery(ups.get("status")),
            "charge_pct": charge,
        } if ups else None),
        "backup_age_s": backup_age,
        "updates_pending": _finite(updates.get("pending")) if updates else None,
        "reboot_required": bool(
            updates and updates.get("reboot_required") is True
        ),
        "ts": blocks.get("ts"),
    }


def evaluate_warnings(device, now):
    """Curated per-device warnings from the summary fields.

    The same rule runs for the metrics page and the fleet summary. The rail
    uses `live_warnings`, which feeds only the cheap live fields and therefore
    skips the history-derived days-to-90/growth checks.
    """
    warnings = []

    # The gauge is the machine total, but the warning names the fullest volume
    # on a multi-volume device: a 99%-full C: must not hide in a 60% average.
    disk = _finite(device.get("disk_pct"))
    fullest = fullest_volume(device)
    if fullest and fullest[1] and (disk is None or fullest[0] > disk):
        value, text = fullest[0], f"{fullest[1]} {fullest[0]:.0f}% full"
    else:
        value = disk
        text = f"Disk {disk:.0f}% full" if disk is not None else ""
    level = _high_warning(value, WARN_DISK_PCT,
                          min(99.0, WARN_DISK_PCT + 10))
    if level:
        warnings.append({
            "code": "disk", "level": level, "value": round(value, 1),
            "threshold": WARN_DISK_PCT, "text": text,
        })

    days = _finite(device.get("days_to_90"))
    level = _low_warning(days, WARN_DAYS_TO_90,
                         max(1.0, WARN_DAYS_TO_90 / 4))
    if level:
        warnings.append({
            "code": "days_to_90", "level": level, "value": days,
            "threshold": WARN_DAYS_TO_90, "text": f"About {days:g}d to 90%",
        })

    growth = _finite(device.get("disk_growth_per_day"))
    level = _high_warning(growth, WARN_GROWTH, WARN_GROWTH * 5)
    if level:
        warnings.append({
            "code": "growth", "level": level, "value": growth,
            "threshold": WARN_GROWTH,
            "text": f"Disk growing {growth:g}%/day",
        })

    mem = _finite(device.get("mem_pct"))
    level = _high_warning(mem, WARN_MEM_PCT, min(99.0, WARN_MEM_PCT + 7))
    if level:
        warnings.append({
            "code": "mem", "level": level, "value": round(mem, 1),
            "threshold": WARN_MEM_PCT, "text": f"RAM {mem:.0f}%",
        })

    if not device.get("online", True):
        last_seen = _finite(device.get("last_seen"))
        offline = max(0, int(now) - int(last_seen)) if last_seen is not None else 0
        level = _high_warning(offline, WARN_OFFLINE_S, WARN_OFFLINE_S * 6)
        if level:
            warnings.append({
                "code": "offline", "level": level, "value": offline,
                "threshold": WARN_OFFLINE_S,
                "text": f"Offline for {_duration_label(offline)}",
            })

    warnings.extend(health_warnings(device.get("health"), now))
    return warnings


def _alert_counts(warnings):
    """(count, level) excluding info; an available update is not an alert."""
    alerts = [warning for warning in warnings if warning.get("level") != "info"]
    level = ("critical" if any(warning["level"] == "critical" for warning in alerts)
             else ("warning" if alerts else None))
    return len(alerts), level


def live_warnings(device, now):
    """The cheap dashboard-rail subset: live disk, RAM, offline and health.

    Health blocks are already in the in-memory agent record, so this stays a
    pure arithmetic check and never scans history on the 5 s stats poll.
    """
    return evaluate_warnings({
        "disk_pct": device.get("disk_pct"),
        "disks": device.get("disks"),
        "mem_pct": device.get("mem_pct"),
        "online": device.get("online", True),
        "last_seen": device.get("last_seen"),
        "health": device.get("health"),
    }, now)


def _check_failure_text(result):
    verdict = result.get("verdict")
    if verdict == "host_down":
        return "unreachable (host offline)"
    if verdict == "path_down":
        return "unreachable (path down)"
    detail = result.get("error")
    if not detail and result.get("status") is not None:
        detail = f"HTTP {result['status']}"
    return f"down ({detail or 'probe failed'})"


def _cert_warning_text(name, days):
    if days <= 0:
        return f"{name} certificate expired {abs(days):g}d ago"
    return f"{name} certificate expires in {days:g}d"


def check_warnings():
    """Failures and expiring certificates in the shared warning shape.

    This reads only the latest per-check state the sampler keeps in memory, so
    `/api/stats` never scans the check history on its 5 s poll.
    """
    with _lock:
        results = list(_check_state["results"].items())
    warnings = []
    for check_id, result in results:
        name = result.get("name") or check_id
        if result.get("verdict") in FAILING_VERDICTS:
            warnings.append({
                "code": "service",
                "level": "critical",
                "value": result.get("status"),
                "threshold": None,
                "text": f"{name} {_check_failure_text(result)}",
            })
        cert_days = _finite(result.get("cert_days"))
        if cert_days is not None and cert_days <= WARN_CERT_DAYS:
            warnings.append({
                "code": "cert",
                "level": "critical" if cert_days <= WARN_CERT_DAYS / 2
                else "warning",
                "value": round(cert_days, 1),
                "threshold": WARN_CERT_DAYS,
                "text": _cert_warning_text(name, cert_days),
            })
    return warnings


def _empty_summary():
    return {
        "samples": 0, "availability_pct": None, "downtime_s": 0,
        "offline_events": 0, "last_offline": None, "restarts": 0,
        "cpu_pct": None, "cpu_avg": None, "cpu_max": None,
        "mem_pct": None, "mem_avg": None, "disk_pct": None,
        "disk_growth_per_day": None, "days_to_90": None,
    }


def _round1(value):
    return round(float(value), 1) if value is not None else None


def _window_downtime(parts, until, part_s):
    """Seconds of the window a device spent offline.

    A part spans `part_s` and knows what share of its samples were online, so
    its offline wall time is that share of the span. For the raw store a part
    is one nominal sample interval, which follows the same rule the rollup
    tiers use and keeps a gap in the server itself (absent rows) out of the
    downtime. An offline run still open at the last part keeps counting to
    `until`, the way the raw path always treated its trailing sample.
    """
    downtime = 0.0
    for part in parts:
        if part["samples"]:
            offline = part["samples"] - part["online_samples"]
            downtime += part_s * offline / part["samples"]
    last = parts[-1] if parts else None
    if last and last["samples"] and last["online_samples"] == 0:
        downtime += max(0, int(until) - (int(last["ts"]) + part_s))
    return downtime


def _derive_summary(parts, until, part_s, events):
    """Availability, averages, offline events and disk trend for one window.

    Every part is weighted by its sample count, exactly as the rollup tiers
    store their aggregates; a raw part holds one sample. Both therefore follow
    one rule, including averaging every sample the tier recorded rather than
    only the online ones: after the fact a rollup cannot separate an offline
    sample's last-known CPU from a live one. Callers pass the window's events
    so the transition scan runs once per window.
    """
    if not parts:
        return _empty_summary()
    samples = sum(part["samples"] for part in parts)
    online_samples = sum(part["online_samples"] for part in parts)
    cpu_avg = _weighted((part["cpu_avg"], part["samples"]) for part in parts)
    cpu_max = _bounds(part["cpu_max"] for part in parts)[1]
    mem_avg = _weighted((part["mem_avg"], part["samples"]) for part in parts)
    disk_now = next(
        (part["disk_avg"] for part in reversed(parts)
         if part["disk_avg"] is not None), None)
    growth = _trend_per_day([
        (part["ts"], part["disk_avg"]) for part in parts
        if part["online_samples"] > 0 and part["disk_avg"] is not None
    ])
    offline_events = [event for event in events if event["type"] == "offline"]
    latest = parts[-1]
    return {
        "samples": samples,
        "availability_pct": round(100.0 * online_samples / samples, 1)
        if samples else None,
        "downtime_s": int(round(_window_downtime(parts, until, part_s))),
        "offline_events": len(offline_events),
        "last_offline": int(offline_events[0]["ts"]) if offline_events else None,
        "restarts": sum(1 for event in events if event["type"] == "restart"),
        "cpu_pct": _round1(latest["cpu_avg"]),
        "cpu_avg": _round1(cpu_avg),
        "cpu_max": _round1(cpu_max),
        "mem_pct": _round1(latest["mem_avg"]),
        "mem_avg": _round1(mem_avg),
        "disk_pct": _round1(disk_now),
        "disk_growth_per_day": round(growth, 2) if growth is not None else None,
        "days_to_90": _days_to_90(disk_now, growth),
    }


def _history_parts(rows):
    """Raw sample rows in the aggregate shape the rollup tiers store."""
    return [
        {"ts": int(ts), **part}
        for ts, part in zip((row[0] for row in rows), _raw_aggregate_parts(rows))
    ]


def _rollup_parts(rows):
    """Rollup rows, whose leading column is the only thing the shape lacks."""
    return [
        {"ts": int(row[0]), **part}
        for row, part in zip(rows, _rollup_aggregate_parts(
            [row[1:] for row in rows]))
    ]


def _rollup_rows(con, source, device_id, since, until):
    table = "rollup_5m" if source == "5m" else "rollup_1h"
    return con.execute(
        "SELECT ts, samples, online_samples, cpu_avg, cpu_min, cpu_max, "
        "mem_avg, mem_min, mem_max, disk_avg, disk_min, disk_max, "
        "uptime_first, uptime_last, uptime_min "
        f"FROM {table} WHERE device_id = ? AND ts >= ? AND ts <= ? ORDER BY ts",
        (device_id, int(since), int(until)),
    ).fetchall()


def _window_parts(con, device_id, since, until, source):
    """Every row that answers this window, in one shape for every tier."""
    if source == "raw":
        return _history_parts(_history_rows(con, device_id, since, until))
    return _rollup_parts(_rollup_rows(con, source, device_id, since, until))


def _window_series(parts, step):
    """Bucketed points and min/max bands, merged across the tier's rows."""
    buckets = {}
    for part in parts:
        buckets.setdefault(_bucket_start(part["ts"], step), []).append(part)
    points = []
    bands = {"cpu": [], "mem": [], "disk": []}
    for bucket, group in buckets.items():
        merged = _merge_aggregate(group)
        online = int(all(_part_online(part) for part in group))
        points.append([
            int(bucket), _round1(merged["cpu_avg"]), _round1(merged["mem_avg"]),
            _round1(merged["disk_avg"]), online,
        ])
        for key in ("cpu", "mem", "disk"):
            bands[key].append([
                int(bucket), _round1(merged[f"{key}_min"]),
                _round1(merged[f"{key}_max"]),
            ])
    return points, bands


def _read_parts(con, device_id, since, until):
    """The tier, its parts and a part's span for one window.

    This is the only place routing happens. Parts arrive ordered by `ts`;
    every rule below relies on that for transitions, latest values and band
    order.
    """
    source = _window_source(int(until) - int(since))
    part_s = {
        "raw": SAMPLE_SECONDS,
        "5m": ROLLUP_5M_SECONDS,
        "1h": ROLLUP_1H_SECONDS,
    }[source]
    return source, _window_parts(con, device_id, since, until, source), part_s


def read_window(con, device_id, since, until, step):
    """One device's metrics window, read from whichever store answers it.

    The window length alone selects raw samples, 5-minute rollups or hourly
    rollups. All three adapt to the same aggregate-part shape, so the summary,
    event, downtime, series and volume rules are each written once and no
    caller chooses a table. `until` doubles as "now" for the cap on the signal
    strip, whose own store is always raw.
    """
    source, parts, part_s = _read_parts(con, device_id, since, until)
    events = _events(parts)
    points, bands = _window_series(parts, step)
    last = parts[-1] if parts else None
    return {
        "source": source,
        "online": bool(last and _part_online(last)),
        "summary": _derive_summary(parts, until, part_s, events),
        "events": events,
        "points": points,
        "bands": bands,
        "volumes": _volume_series(con, device_id, since, until, step, source),
        "signals": _signal_series(
            con, device_id, until, int(until) - int(since)),
    }


def read_window_summary(con, device_id, since, until, step):
    """The fleet card's slice of a window: no chart data, no signal strip.

    The dashboard summary polls every device every 30 s, so it asks the same
    reader for only the facts it renders rather than building series it throws
    away.
    """
    source, parts, part_s = _read_parts(con, device_id, since, until)
    events = _events(parts)
    last = parts[-1] if parts else None
    return {
        "source": source,
        "online": bool(last and _part_online(last)),
        "summary": _derive_summary(parts, until, part_s, events),
        "events": events,
        "volumes": _volume_series(con, device_id, since, until, step, source),
    }


def _spark(rows, now, minutes=60, buckets=12):
    """Small CPU sparkline for the rail: one value per 5-minute bucket."""
    since = now - minutes * 60
    size = (minutes * 60) / float(buckets)
    totals = [0.0] * buckets
    counts = [0] * buckets
    for row in rows:
        ts, cpu, _mem, _disk, _uptime, online = row
        if ts < since or not online or cpu is None:
            continue
        index = min(buckets - 1, int((ts - since) // size))
        totals[index] += cpu
        counts[index] += 1
    return [round(totals[i] / counts[i], 1) if counts[i] else None for i in range(buckets)]


def _volume_rows(con, device_id, since, until, step, source):
    """One tier's bucketed per-volume rows and its latest row per mount.

    Storage tiers differ only here: the raw path joins samples for the online
    flag, while the rollup tables were written online-only. Both return the
    same columns, so every rule below is shared.
    """
    if source == "raw":
        rows = con.execute(
            "SELECT d.mount, d.ts - (d.ts % ?) AS bucket, ROUND(AVG(d.pct), 1) "
            "FROM disk_samples d "
            "JOIN samples s ON s.device_id = d.device_id AND s.ts = d.ts "
            "WHERE d.device_id = ? AND d.ts >= ? AND d.ts <= ? AND s.online = 1 "
            "GROUP BY d.mount, bucket ORDER BY d.mount, bucket",
            (step, device_id, int(since), int(until)),
        ).fetchall()
        latest = con.execute(
            "SELECT d.mount, d.label, d.total, d.used, d.pct "
            "FROM disk_samples d "
            "JOIN samples s ON s.device_id = d.device_id AND s.ts = d.ts "
            "WHERE d.device_id = ? AND d.ts >= ? AND d.ts <= ? AND s.online = 1 "
            "AND d.ts = ("
            "  SELECT MAX(d2.ts) FROM disk_samples d2 "
            "  JOIN samples s2 ON s2.device_id = d2.device_id AND s2.ts = d2.ts "
            "  WHERE d2.device_id = d.device_id AND d2.mount = d.mount "
            "    AND d2.ts >= ? AND d2.ts <= ? AND s2.online = 1) "
            "ORDER BY d.mount",
            (device_id, int(since), int(until), int(since), int(until)),
        ).fetchall()
        return rows, latest

    table = "disk_rollup_5m" if source == "5m" else "disk_rollup_1h"
    rows = con.execute(
        "SELECT mount, ts - (ts % ?) AS bucket, "
        "ROUND(SUM(pct_avg * samples) / NULLIF(SUM(samples), 0), 1) "
        f"FROM {table} WHERE device_id = ? AND ts >= ? AND ts <= ? "
        "GROUP BY mount, bucket ORDER BY mount, bucket",
        (step, device_id, int(since), int(until)),
    ).fetchall()
    latest = con.execute(
        "SELECT d.mount, d.label, d.total, d.used, d.pct_avg "
        f"FROM {table} d "
        "WHERE d.device_id = ? AND d.ts >= ? AND d.ts <= ? "
        "AND d.ts = ("
        f"  SELECT MAX(d2.ts) FROM {table} d2 "
        "  WHERE d2.device_id = d.device_id AND d2.mount = d.mount "
        "    AND d2.ts >= ? AND d2.ts <= ?) "
        "ORDER BY d.mount",
        (device_id, int(since), int(until), int(since), int(until)),
    ).fetchall()
    return rows, latest


def _volume_series(con, device_id, since, until, step, source):
    """Per-volume bucketed pct series, latest size and a growth trend.

    Online-only, matching the aggregate disk gauge: an agent that stopped
    pushing keeps its last values in agents.json, but those stale values must
    not look like fresh volume history. Long windows route here too, otherwise
    the 90d/1y ranges would show no volume history once raw rows age out.
    """
    rows, latest = _volume_rows(con, device_id, since, until, step, source)
    series = {}
    for mount, bucket, value in rows:
        series.setdefault(mount, []).append([int(bucket), value])

    volumes = []
    for mount, label, total, used, pct in latest:
        points = series.get(mount, [])
        growth = _trend_per_day(points)
        volumes.append({
            "mount": mount,
            "label": label or "",
            "total": total,
            "used": used,
            "pct": round(float(pct), 1) if pct is not None else None,
            "growth_per_day": round(growth, 2) if growth is not None else None,
            "days_to_90": _days_to_90(pct, growth),
            "points": points,
        })
    # Fullest first; a missing pct sorts last. Mount breaks ties so the order
    # is stable across requests.
    volumes.sort(key=lambda volume: (
        volume["pct"] is None, -(volume["pct"] or 0.0), volume["mount"],
    ))
    return volumes


def _volume_summary(volumes):
    """The compact per-device volume summary: count and the fullest volume.

    The fullest volume is the one the disk warning names on a multi-volume
    device, so it is the mount worth projecting days-to-90 for without
    shipping every series in the fleet summary.
    """
    if not volumes:
        return {"count": 0, "fullest": None}
    fullest = volumes[0]
    return {
        "count": len(volumes),
        "fullest": {
            "mount": fullest["mount"],
            "label": fullest["label"],
            "pct": fullest["pct"],
            "total": fullest["total"],
            "used": fullest["used"],
            "growth_per_day": fullest["growth_per_day"],
            "days_to_90": fullest["days_to_90"],
        },
    }


def _signal_series(con, device_id, now, window_s):
    """Recent extra-signal series for the compact strip.

    Signals are raw-only and the query is capped at SIGNAL_WINDOW_S, so a 90d
    or 1y range never turns into a large scan just to draw a sparkline. Missing
    metrics are omitted entirely; they are never synthesised as zero.
    """
    window_s = min(window_s, SIGNAL_WINDOW_S)
    step = _step_for(window_s, None)
    rows = con.execute(
        "SELECT metric, ts - (ts % ?) AS bucket, ROUND(AVG(value), 2) "
        "FROM metric_samples WHERE device_id = ? AND ts >= ? AND ts <= ? "
        "GROUP BY metric, bucket ORDER BY metric, bucket",
        (step, device_id, int(now - window_s), int(now)),
    ).fetchall()
    series = {}
    for metric, bucket, value in rows:
        series.setdefault(metric, []).append([int(bucket), value])

    signals = {}
    for metric in ("load_1", "load_5", "load_15", "net_rx_bps",
                   "net_tx_bps", "cpu_iowait", "cpu_steal"):
        points = series.get(metric)
        if points:
            signals[metric] = {"value": points[-1][1], "points": points}

    used = dict(series.get("swap_used", []))
    total = dict(series.get("swap_total", []))
    swap_points = []
    for bucket in sorted(set(used) & set(total)):
        if total[bucket] and total[bucket] > 0:
            swap_points.append([
                bucket,
                round(100.0 * min(used[bucket], total[bucket]) / total[bucket], 1),
            ])
    if swap_points:
        latest = swap_points[-1][0]
        signals["swap"] = {
            "used": used[latest],
            "total": total[latest],
            "pct": swap_points[-1][1],
            "points": swap_points,
        }
    return signals


def _latest_signals(con, device_id):
    """Latest value per extra metric, for the compact fleet summary."""
    rows = con.execute(
        "SELECT m.metric, m.value FROM metric_samples m "
        "WHERE m.device_id = ? AND m.ts = ("
        "  SELECT MAX(m2.ts) FROM metric_samples m2 "
        "  WHERE m2.device_id = m.device_id AND m2.metric = m.metric)",
        (device_id,),
    ).fetchall()
    values = {metric: value for metric, value in rows}
    signals = {}
    for metric in ("load_1", "load_5", "load_15", "net_rx_bps",
                   "net_tx_bps", "cpu_iowait", "cpu_steal"):
        if values.get(metric) is not None:
            signals[metric] = round(float(values[metric]), 2)
    used, total = values.get("swap_used"), values.get("swap_total")
    if used is not None and total and total > 0:
        signals["swap_pct"] = round(100.0 * min(used, total) / total, 1)
    return signals


def _health_block(kind, blob):
    """One stored block, re-validated on read; None when unusable."""
    if kind not in HEALTH_KINDS:
        return None
    try:
        payload = json.loads(blob)
    except (TypeError, ValueError):
        return None
    cleaned = clean_health({kind: payload})
    return cleaned.get(kind)


def _latest_health(con, device_id):
    """Latest cleaned block per kind, plus the newest snapshot timestamp.

    Kinds are read independently: a heartbeat may report SMART but omit ZFS
    because the command was missing, so the last known pool state is still
    useful. A device with no health rows gets ``{"ts": None}``.
    """
    rows = con.execute(
        "SELECT h.kind, h.ts, h.payload_json FROM device_health h "
        "WHERE h.device_id = ? AND h.ts = ("
        "  SELECT MAX(h2.ts) FROM device_health h2 "
        "  WHERE h2.device_id = h.device_id AND h2.kind = h.kind)",
        (device_id,),
    ).fetchall()
    out = {"ts": None}
    for kind, ts, blob in rows:
        block = _health_block(kind, blob)
        if block is None:
            continue
        out[kind] = block
        out["ts"] = ts if out["ts"] is None else max(out["ts"], int(ts))
    return out


def _health_history(con, device_id, since, until, limit=HEALTH_HISTORY_LIMIT):
    """The last ``limit`` health snapshots in the window, oldest first."""
    rows = con.execute(
        "SELECT ts, kind, payload_json FROM device_health "
        "WHERE device_id = ? AND ts >= ? AND ts <= ? AND ts IN ("
        "  SELECT DISTINCT ts FROM device_health "
        "  WHERE device_id = ? AND ts >= ? AND ts <= ? "
        "  ORDER BY ts DESC LIMIT ?) "
        "ORDER BY ts, kind",
        (device_id, int(since), int(until),
         device_id, int(since), int(until), int(limit)),
    ).fetchall()
    snapshots = {}
    for ts, kind, blob in rows:
        block = _health_block(kind, blob)
        if block is not None:
            snapshots.setdefault(int(ts), {})[kind] = block
    return [{"ts": ts, **snapshots[ts]} for ts in sorted(snapshots)]


def _stored_annotations(con, device_id, since, until):
    """Device-specific plus fleet-wide annotations inside the window."""
    rows = con.execute(
        "SELECT id, device_id, ts, kind, text FROM annotations "
        "WHERE ts >= ? AND ts <= ? AND (device_id = ? OR device_id = '') "
        "ORDER BY ts DESC LIMIT ?",
        (int(since), int(until), device_id, MAX_ANNOTATIONS),
    ).fetchall()
    # The limit keeps the newest notes; restore chronological order for markers.
    rows = rows[::-1]
    return [{
        "id": row[0], "device": row[1], "ts": int(row[2]),
        "kind": row[3], "text": row[4], "auto": False,
    } for row in rows]


def _window_source(window_s):
    """Which store answers this window: raw, 5-minute or hourly rollups."""
    if window_s <= RAW_MAX_WINDOW:
        return "raw"
    if window_s <= ROLLUP_5M_MAX_WINDOW:
        return "5m"
    return "1h"


def _window_seconds(window):
    return METRIC_WINDOWS.get(window)


def _step_for(window_s, requested):
    step = requested if requested is not None else DEFAULT_STEPS.get(
        next((name for name, seconds in METRIC_WINDOWS.items() if seconds == window_s), None),
        300,
    )
    step = max(60, min(int(step), window_s))
    if window_s / step > MAX_POINTS:
        step = int(math.ceil(window_s / MAX_POINTS))
    return step


def _known_devices(con):
    return con.execute(
        "SELECT id, name, kind, first_seen, last_seen FROM devices ORDER BY kind, name"
    ).fetchall()


def metrics_summary(window=DEFAULT_WINDOW, now=None):
    """Every known device with its derived window summary and a CPU sparkline.

    Long windows are answered from rollups, not by scanning every raw row per
    device. Live requests are also cached for SUMMARY_CACHE_SECONDS: the page
    polls once a minute, so one shared scan serves every open tab. Explicit
    `now` (tests, backfills) bypasses the cache and stays deterministic.
    """
    live_request = now is None
    now = time.time() if now is None else now
    window_s = _window_seconds(window) or METRIC_WINDOWS[DEFAULT_WINDOW]
    if live_request:
        with _lock:
            if (_summary_cache["body"] is not None
                    and _summary_cache["window"] == window
                    and now - _summary_cache["at"] < SUMMARY_CACHE_SECONDS):
                return _summary_cache["body"]
    since = now - window_s
    payload = {"generated": int(now), "window": window, "window_s": window_s,
               "source": None, "devices": [], "topology": stack_topology([])}
    if not METRICS_FILE.exists():
        payload["source"] = _window_source(window_s)
        return payload
    live = _live_state(now)
    step = _step_for(window_s, None)
    con = _connect_metrics()
    try:
        for device_id, name, kind, _first, last_seen in _known_devices(con):
            # The sparkline is always the last hour, so it stays a cheap raw
            # query regardless of which store answers the window.
            spark = _spark(_history_rows(con, device_id, now - 3600, now), now)
            view = read_window_summary(con, device_id, since, now, step)
            payload["source"] = view["source"]
            state = live.get(device_id, {})
            health = _latest_health(con, device_id)
            role = guest_role(kind, name)
            entry = {
                "id": device_id,
                "name": name,
                "kind": kind,
                # The metrics page draws the same rail, so a guest needs the same
                # role label here; this store only keeps id, name and kind.
                **({"role": role} if role else {}),
                "online": state.get("online", view["online"]),
                "last_seen": state.get("last_seen", int(last_seen)),
                "spark": spark,
                "volumes": _volume_summary(view["volumes"]),
                "signals": _latest_signals(con, device_id),
                "health": _health_summary(health, now),
                **view["summary"],
                "source": view["source"],
            }
            # Evaluate against the full blocks, then serialise only the compact
            # summary so the fleet payload does not grow with disk count.
            entry["warnings"] = evaluate_warnings(
                {**entry, "health": health}, now
            )
            entry["warning_count"], entry["warning_level"] = _alert_counts(
                entry["warnings"]
            )
            payload["devices"].append(entry)
    finally:
        con.close()
    if payload["source"] is None:  # an existing database with no devices yet
        payload["source"] = _window_source(window_s)
    payload["topology"] = stack_topology(payload["devices"])
    if live_request:
        with _lock:
            _summary_cache.update(at=now, window=window, body=payload)
    return payload


def metrics_series(device_id, window=DEFAULT_WINDOW, requested_step=None, now=None):
    """One device's bucketed time series, min/max bands, summary and events."""
    now = time.time() if now is None else now
    window_s = _window_seconds(window) or METRIC_WINDOWS[DEFAULT_WINDOW]
    step = _step_for(window_s, requested_step)
    since = now - window_s
    payload = {
        "generated": int(now), "window": window, "window_s": window_s,
        "step": step, "source": None,
        "device": None, "summary": _empty_summary(), "points": [],
        "bands": {"cpu": [], "mem": [], "disk": []}, "volumes": [],
        "signals": {}, "annotations": [], "warnings": [], "events": [],
        "health": {"ts": None}, "health_history": [],
    }
    if not METRICS_FILE.exists():
        if device_id:
            return None
        payload["source"] = _window_source(window_s)
        return payload
    live = _live_state(now)
    con = _connect_metrics()
    try:
        meta = con.execute(
            "SELECT id, name, kind, last_seen FROM devices WHERE id = ?", (device_id,)
        ).fetchone()
        if meta is None:
            return None
        view = read_window(con, device_id, since, now, step)
        stored = _stored_annotations(con, device_id, since, now)
        health = _latest_health(con, device_id)
        health_history = _health_history(con, device_id, since, now)
    finally:
        con.close()
    state = live.get(device_id, {})
    payload["device"] = {
        "id": meta[0], "name": meta[1], "kind": meta[2],
        "online": state.get("online", view["online"]),
        "last_seen": state.get("last_seen", int(meta[3])),
    }
    # Annotations are chart markers; manual ones also become Events rows.
    events = view["events"] + [
        {"ts": annotation["ts"], "type": "annotation",
         "kind": annotation["kind"], "text": annotation["text"]}
        for annotation in stored
    ]
    events.sort(key=lambda event: event["ts"], reverse=True)
    annotations = stored + [
        {"id": None, "device": device_id, "ts": event["ts"],
         "kind": "restart", "text": "Restart detected", "auto": True}
        for event in events if event["type"] == "restart"
    ][:MAX_AUTO_ANNOTATIONS]
    annotations.sort(key=lambda annotation: annotation["ts"])

    payload["source"] = view["source"]
    payload["summary"] = view["summary"]
    payload["points"] = view["points"]
    payload["bands"] = view["bands"]
    payload["volumes"] = view["volumes"]
    payload["signals"] = view["signals"]
    payload["annotations"] = annotations
    payload["events"] = events[:20]
    payload["health"] = health
    payload["health_history"] = health_history
    payload["warnings"] = evaluate_warnings({
        **view["summary"],
        "volumes": view["volumes"],
        "online": payload["device"]["online"],
        "last_seen": payload["device"]["last_seen"],
        "health": health,
    }, now)
    return payload


# ---------- brief ----------
# One item per thing a person would want named: an offline run, a restart, a
# warning, a note, a stale agent, or a volume that moved. Every source is
# derived from the same window view the charts use, so the brief cannot invent
# a fact the device page does not also show.

def _brief_clock(ts):
    """Wall-clock time on the server's own clock, matching the dashboard."""
    return time.strftime("%H:%M", time.localtime(int(ts)))


def _brief_href(device_id):
    """The device's page. The id is URL-encoded because agent ids carry a colon."""
    return "metrics.html?device=" + quote(device_id, safe="")


def _brief_item(kind, level, device_id, name, ts, text, detail=None, href=None):
    """One item in the documented shape, omitting optional fields when absent."""
    item = {"kind": kind, "level": level, "device_id": device_id,
            "device_name": name, "ts": int(ts), "text": text}
    if detail:
        item["detail"] = detail
    if href:
        item["href"] = href
    return item


def _brief_bytes(value):
    """Gigabyte-scale sizes read as '1.6 GB'; smaller units stay whole."""
    size = float(value)
    units = ("B", "KB", "MB", "GB", "TB")
    index = 0
    while size >= 1024 and index < len(units) - 1:
        size /= 1024
        index += 1
    digits = 1 if index >= 3 else 0
    return f"{size:.{digits}f} {units[index]}"


def _brief_volume_item(name, device_id, volumes, now):
    """The one volume worth naming: the most severe move over the window.

    Growth is measured from the first to the last online point of the same
    window the chart draws, so the line and the brief agree. A volume that is
    merely steady but nearly full is still named; a flat, roomy one is not.
    """
    candidates = []
    for volume in volumes:
        pct = _finite(volume.get("pct"))
        if pct is None:
            continue
        points = [value for _ts, value in volume.get("points", [])
                  if _finite(value) is not None]
        first = points[0] if points else None
        delta = pct - first if first is not None else None
        days = _finite(volume.get("days_to_90"))
        growth = _finite(volume.get("growth_per_day"))
        # The same thresholds as evaluate_warnings, so a volume and the device
        # warning never disagree about what counts as critical. A move that
        # crosses no configured line is still worth a line, but only info.
        levels = (
            _high_warning(pct, WARN_DISK_PCT, min(99.0, WARN_DISK_PCT + 10)),
            _low_warning(days, WARN_DAYS_TO_90, max(1.0, WARN_DAYS_TO_90 / 4)),
            _high_warning(growth, WARN_GROWTH, WARN_GROWTH * 5),
        )
        if "critical" in levels:
            level = "critical"
        elif "warning" in levels:
            level = "warning"
        elif delta is not None and delta >= BRIEF_DISK_JUMP_PCT:
            level = "info"
        else:
            continue
        label = volume.get("label") or volume.get("mount") or "disk"
        total = volume.get("total")
        if delta is not None and delta > 0 and total:
            text = (f"{name} {label} grew {_brief_bytes(total * delta / 100.0)} "
                    f"({first:g}% → {pct:g}%)")
        elif pct >= WARN_DISK_PCT:
            text = f"{name} {label} is {pct:g}% full"
        elif growth is not None:
            text = f"{name} {label} is growing {growth:g}%/day"
        else:
            text = f"{name} {label} is at {pct:g}%"
        detail = (f"{days:g}d to 90%" if days is not None
                  else (f"{growth:g}%/day" if growth is not None else None))
        candidates.append(({"critical": 0, "warning": 1, "info": 2}[level],
                           -(delta or 0.0),
                           _brief_item("disk", level, device_id, name, now, text,
                                       detail, _brief_href(device_id))))
    if not candidates:
        return None
    candidates.sort(key=lambda candidate: (candidate[0], candidate[1]))
    return candidates[0][2]


def _brief_previous_availability(con, device_id, since, window_s):
    """Online share of the equal-length window before this one, or None.

    One aggregate query, routed through the same tier rule as the chart, so the
    comparison never doubles the per-device scan.
    """
    start, end = int(since - window_s), int(since)
    source = _window_source(window_s)
    if source == "raw":
        row = con.execute(
            "SELECT AVG(online), COUNT(*) FROM samples "
            "WHERE device_id = ? AND ts >= ? AND ts < ?",
            (device_id, start, end),
        ).fetchone()
        share = row[0] if row and row[1] else None
    else:
        table = "rollup_5m" if source == "5m" else "rollup_1h"
        row = con.execute(
            f"SELECT SUM(online_samples), SUM(samples) FROM {table} "
            "WHERE device_id = ? AND ts >= ? AND ts < ?",
            (device_id, start, end),
        ).fetchone()
        share = (row[0] / row[1]) if row and row[1] else None
    return round(100.0 * share, 1) if share is not None else None


def _brief_availability_item(name, device_id, summary, previous, now):
    """A drop worth naming, or None when either window has no samples."""
    current = _finite(summary.get("availability_pct"))
    if current is None or previous is None:
        return None
    drop = previous - current
    if drop < BRIEF_AVAILABILITY_DROP_PCT:
        return None
    return _brief_item(
        "availability",
        "warning" if drop >= 2 * BRIEF_AVAILABILITY_DROP_PCT else "info",
        device_id, name, now,
        f"{name} availability fell to {current:g}% (was {previous:g}%)",
        "Compared with the previous equal-length window",
        _brief_href(device_id))


def _brief_device_items(device_id, name, device, view, now):
    """Offline runs, restarts and current warnings for one device."""
    items = []
    href = _brief_href(device_id)
    downtime = max(0, int(round(device.get("downtime_s") or 0)))

    # Events arrive newest first; walk them chronologically so an offline run
    # pairs with the recovery that closed it. A recovery with no offline event
    # began before the window, so only its end is certain.
    episode_start = None
    for event in reversed(view["events"]):
        if event["type"] == "offline":
            episode_start = int(event["ts"])
        elif event["type"] == "online" and event.get("down_s") is not None:
            down = max(0, int(event["down_s"]))
            total = _duration_label(downtime)
            if episode_start is None:
                items.append(_brief_item(
                    "offline", "info", device_id, name, event["ts"],
                    f"{name} recovered at {_brief_clock(event['ts'])}",
                    f"Outage began before this window; {total} total in it",
                    href))
            else:
                items.append(_brief_item(
                    "offline",
                    _high_warning(down, WARN_OFFLINE_S, WARN_OFFLINE_S * 6) or "info",
                    device_id, name, episode_start,
                    f"{name} was offline for {_duration_label(down)} "
                    f"at {_brief_clock(episode_start)}",
                    f"Recovered at {_brief_clock(event['ts'])}; {total} total in this window",
                    href))
                episode_start = None
    if episode_start is not None:
        down = max(0, int(now) - episode_start)
        items.append(_brief_item(
            "offline",
            _high_warning(down, WARN_OFFLINE_S, WARN_OFFLINE_S * 6) or "info",
            device_id, name, episode_start,
            f"{name} has been offline since {_brief_clock(episode_start)}",
            f"Still offline at {_brief_clock(now)}; "
            f"{_duration_label(downtime)} total in this window",
            href))

    # A detected restart is the codebase's auto annotation, so it appears once
    # here rather than as a second annotation item.
    for event in view["events"]:
        if event["type"] == "restart":
            items.append(_brief_item(
                "restart", "info", device_id, name, event["ts"],
                f"{name} restarted at {_brief_clock(event['ts'])}",
                "Detected restart (auto annotation); uptime reset",
                href))

    # Warnings describe the state at the window end. The offline warning is
    # represented by the stale-agent item instead, with last-seen wording, and
    # a named volume supersedes the aggregate disk/days/growth warnings.
    volume_item = _brief_volume_item(name, device_id, view["volumes"], now)
    if volume_item:
        items.append(volume_item)
    covered = {"disk", "days_to_90", "growth"} if volume_item else set()
    for warning in evaluate_warnings(device, now):
        if warning["code"] == "offline" or warning["code"] in covered:
            continue
        items.append(_brief_item(
            "warning", warning["level"], device_id, name, now,
            f"{name}: {warning['text']}",
            f"{warning['code']} crossing {warning['threshold']:g}", href))

    # An agent that has been quiet for the whole window has no offline
    # transition to report, so its last-seen age is the only evidence. An open
    # episode above already says it better, so the two never both appear.
    if not device.get("online", True) and episode_start is None:
        last_seen = _finite(device.get("last_seen"))
        age = max(0, int(now) - int(last_seen)) if last_seen is not None else 0
        level = _high_warning(age, WARN_OFFLINE_S, WARN_OFFLINE_S * 6)
        if level and last_seen is not None:
            items.append(_brief_item(
                "stale", level, device_id, name, last_seen,
                f"{name} last reported {_duration_label(age)} ago",
                f"Last seen at {_brief_clock(last_seen)}", href))

    return items


def _brief_annotations(con, since, until, names):
    """Manual annotations once each, fleet-wide notes included exactly once.

    The chart path reads annotations per device, so a fleet-wide note shows on
    every chart. A single ranked list would duplicate it, so this reads the
    table directly and resolves the device name from the same map the charts
    use.
    """
    rows = con.execute(
        "SELECT device_id, ts, kind, text FROM annotations "
        "WHERE ts >= ? AND ts <= ? ORDER BY ts DESC LIMIT ?",
        (int(since), int(until), MAX_ANNOTATIONS),
    ).fetchall()
    items = []
    for device_id, ts, kind, text in rows:
        name = names.get(device_id, device_id) if device_id else "fleet"
        items.append(_brief_item(
            "annotation", "info", device_id, name, ts,
            f"{name}: {text}",
            f"Manual {kind} annotation at {_brief_clock(ts)}",
            _brief_href(device_id) if device_id else None))
    return items


def brief(window=DEFAULT_WINDOW, now=None):
    """One ranked list of what changed over a window, newest concerns first.

    Every device is read through `read_window_summary`, so a 7d brief uses the
    same rollup routing as a 7d chart and never scans raw rows. The result is
    cached for live requests exactly like the fleet summary and invalidated by
    the same writes.
    """
    live_request = now is None
    now = time.time() if now is None else now
    window_s = _window_seconds(window) or METRIC_WINDOWS[DEFAULT_WINDOW]
    if live_request:
        with _lock:
            if (_brief_cache["body"] is not None
                    and _brief_cache["window"] == window
                    and now - _brief_cache["at"] < SUMMARY_CACHE_SECONDS):
                return _brief_cache["body"]
    payload = {
        "generated": int(now), "window": window, "window_s": window_s,
        "quiet": True,
        "counts": {"critical": 0, "warning": 0, "info": 0},
        "items": [],
    }
    if METRICS_FILE.exists():
        since = now - window_s
        live = _live_state(now)
        step = _step_for(window_s, None)
        items = []
        names = {}
        con = _connect_metrics()
        try:
            for device_id, name, _kind, _first, last_seen in _known_devices(con):
                names[device_id] = name
                state = live.get(device_id, {})
                view = read_window_summary(con, device_id, since, now, step)
                device = {
                    **view["summary"],
                    "online": state.get("online", view["online"]),
                    "last_seen": state.get("last_seen", int(last_seen)),
                }
                items.extend(_brief_device_items(device_id, name, device, view, now))
                availability_item = _brief_availability_item(
                    name, device_id, view["summary"],
                    _brief_previous_availability(con, device_id, since, window_s),
                    now)
                if availability_item:
                    items.append(availability_item)
            items.extend(_brief_annotations(con, since, now, names))
        finally:
            con.close()
        # Severity first, then recency: a critical warning outranks a newer
        # restart, and equal levels read newest first.
        rank = {"critical": 0, "warning": 1, "info": 2}
        items.sort(key=lambda item: (rank.get(item["level"], 3), -int(item["ts"])))
        counts = {
            level: sum(1 for item in items if item["level"] == level)
            for level in ("critical", "warning", "info")
        }
        payload["counts"] = counts
        payload["quiet"] = (
            counts["warning"] == 0 and counts["critical"] == 0
            and not any(item["kind"] in BRIEF_EVENT_KINDS for item in items)
        )
        payload["items"] = items[:BRIEF_MAX_ITEMS]
    if live_request:
        with _lock:
            _brief_cache.update(at=now, window=window, body=payload)
    return payload


# ---------- aggregate ----------

def stats():
    now = time.time()
    with _lock:
        if _cache["body"] and now - _cache["at"] < 5:
            return _cache["body"]
    devices = [local_device()] + agent_devices()
    # The rail shows 24 h availability next to each device; the query is cached
    # for a minute so the 5 s stats cache is not backed by a 60 s table scan.
    availability = availability_24h(now)
    for device in devices:
        device["availability_24h"] = availability.get(device["id"])
        # The rail indicator is deliberately the cheap live subset: no history
        # scan on a 5 s poll. The metrics page carries the full warning set.
        # An available update is info, not an alert: it must not turn the chip
        # on for every healthy machine.
        device["warning_count"], device["warning_level"] = _alert_counts(
            live_warnings(device, now)
        )
    body = {
        "generated": int(now),
        "devices": devices,
        "topology": stack_topology(devices),
        # Additive: the launcher's service warnings ride the same 5 s payload.
        "warnings": check_warnings(),
    }
    with _lock:
        _cache["at"] = now
        _cache["body"] = body
    return body


def with_scores(store, scores):
    """Stamp each bookmark item with its decayed click score."""
    if isinstance(store, dict):
        for categories in store.values():
            if not isinstance(categories, list):
                continue
            for category in categories:
                if not isinstance(category, dict):
                    continue
                for item in category.get("items", []):
                    if isinstance(item, dict) and isinstance(item.get("url"), str):
                        item["score"] = scores.get(item["url"], 0.0)
    return store


def bookmarks():
    try:
        with open(DATA / "bookmarks.json") as f:
            store = json.load(f)
    except (OSError, ValueError):  # a fresh install has no store yet
        return {"work": []}
    return with_scores(store, hit_scores())


# ---------- click history ----------

def _valid_hit_timestamp(ts, cutoff):
    """A real POSIX timestamp at or after cutoff. bool is an int subclass, so reject it."""
    return not isinstance(ts, bool) and isinstance(ts, (int, float)) and ts >= cutoff


def load_hits(now):
    """Read the click store, dropping malformed entries and expired stamps."""
    try:
        with open(HITS_FILE) as f:
            data = json.load(f)
    except Exception:  # noqa: BLE001 - a missing store means no clicks yet
        return {}
    if not isinstance(data, dict):
        return {}
    cutoff = now - HIT_RETENTION_SECONDS
    hits = {}
    for url, stamps in data.items():
        if not isinstance(stamps, list):
            continue
        live = [ts for ts in stamps if _valid_hit_timestamp(ts, cutoff)]
        if live:
            hits[url] = live
    return hits


def save_hits(hits):
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = HITS_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(hits, f)
    tmp.replace(HITS_FILE)


def trim_hits(hits, now):
    """Drop timestamps older than the retention window, per URL."""
    cutoff = now - HIT_RETENTION_SECONDS
    kept = {}
    for url, stamps in hits.items():
        if not isinstance(stamps, list):
            continue
        live = [ts for ts in stamps if _valid_hit_timestamp(ts, cutoff)]
        if live:
            kept[url] = live
    return kept


def record_hit(url):
    now = time.time()
    with _hits_lock:
        hits = load_hits(now)
        hits.setdefault(url, []).append(now)
        save_hits(trim_hits(hits, now))


def hit_scores():
    """Decayed click weight per URL: sum(exp(-age_days / HALF_LIFE_DAYS)).

    Every URL in the hit store is scored, not just rendered categories. The
    browser strip then filters to the `work` list it renders (see selectMostUsed
    in app.js), so clicks on personal links are kept but never shown there.
    """
    now = time.time()
    scores = {}
    for url, stamps in load_hits(now).items():
        total = 0.0
        for ts in stamps:
            if ts > now:  # clock skew: not scorable until the clock catches up
                continue
            age_days = (now - ts) / 86400.0
            total += math.exp(-age_days / HALF_LIFE_DAYS)
        if total:
            scores[url] = round(total, 2)
    return scores


# ---------- http ----------

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    # Agents fetch these over HTTP and, in the PowerShell case, compare the
    # response text with the installed copy. octet-stream would hand it a byte
    # array instead of a string, so keep them textual.
    ".ps1": "text/plain; charset=utf-8",
    ".py": "text/plain; charset=utf-8",
    ".sh": "text/plain; charset=utf-8",
    ".plist": "text/plain; charset=utf-8",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "dash/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quieter logs
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)

    def _send(self, code, body, ctype="application/json; charset=utf-8",
              cache="no-store", etag=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        if etag:
            self.send_header("ETag", etag)
        self.send_header("Vary", "Accept-Encoding")
        self.end_headers()
        self.wfile.write(body)

    def _not_modified(self, etag, cache):
        self.send_response(304)
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", cache)
        self.send_header("Vary", "Accept-Encoding")
        self.end_headers()

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj))

    def _json_cached(self, obj):
        body = json.dumps(obj).encode()
        etag = f'W/"{len(body)}-{hashlib.sha1(body).hexdigest()[:12]}"'
        if self.headers.get("If-None-Match") == etag:
            return self._not_modified(etag, "no-cache")
        return self._send(200, body, cache="no-cache", etag=etag)

    def _render_index(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        try:
            payload = json.dumps(bookmarks())
        except Exception:  # noqa: BLE001 - an unreadable store must not break the page
            payload = '{"work": []}'
        # Never let a bookmark title close the JSON script element.
        payload = payload.replace("<", "\\u003c")
        return html.replace("__BOOKMARKS_JSON__", payload)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            if path == "/api/stats":
                return self._json(stats())
            if path == "/api/bookmarks":
                return self._json_cached(bookmarks())
            if path == "/api/brief":
                window = (query.get("window") or [DEFAULT_WINDOW])[0]
                if window not in BRIEF_WINDOWS:
                    return self._json({"error": "unknown window"}, 400)
                return self._json(brief(window))
            if path == "/api/metrics/summary":
                window = (query.get("window") or [DEFAULT_WINDOW])[0]
                if window not in METRIC_WINDOWS:
                    return self._json({"error": "unknown window"}, 400)
                return self._json(metrics_summary(window))
            if path == "/api/metrics":
                window = (query.get("window") or [DEFAULT_WINDOW])[0]
                if window not in METRIC_WINDOWS:
                    return self._json({"error": "unknown window"}, 400)
                device_id = (query.get("device") or [""])[0].strip()[:200]
                if not device_id:
                    return self._json({"error": "missing device"}, 400)
                raw_step = (query.get("step") or [None])[0]
                try:
                    step = int(raw_step) if raw_step is not None else None
                except ValueError:
                    return self._json({"error": "bad step"}, 400)
                payload = metrics_series(device_id, window, step)
                if payload is None:
                    return self._json({"error": "unknown device"}, 404)
                return self._json(payload)
            if path == "/api/checks":
                return self._json(checks_payload())
            if path == "/api/health":
                return self._json({"ok": True})
        except Exception as exc:  # noqa: BLE001
            return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

        if path in ("", "/", "/index.html"):
            try:
                body = self._render_index().encode()
            except Exception as exc:  # noqa: BLE001
                return self._send(500, f"render failed: {exc}", "text/plain; charset=utf-8")
            etag = f'W/"{len(body)}-{hashlib.sha1(body).hexdigest()[:12]}"'
            if self.headers.get("If-None-Match") == etag:
                return self._not_modified(etag, "no-cache")
            return self._send(200, body, "text/html; charset=utf-8",
                              cache="no-cache", etag=etag)

        rel = path.lstrip("/")
        target = (STATIC / rel).resolve()
        if not str(target).startswith(str(STATIC)) or not target.is_file():
            return self._send(404, "not found", "text/plain; charset=utf-8")
        ctype = CONTENT_TYPES.get(target.suffix, "application/octet-stream")
        st = target.stat()
        etag = f'W/"{st.st_mtime_ns}-{st.st_size}"'
        cache = (
            "public, max-age=31536000, immutable"
            if target.suffix == ".woff2"
            else "public, max-age=0, must-revalidate"
        )
        if self.headers.get("If-None-Match") == etag:
            return self._not_modified(etag, cache)
        return self._send(200, target.read_bytes(), ctype, cache=cache, etag=etag)

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/api/hit":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length > 4096:
                    return self._json({"error": "payload too large"}, 413)
                payload = json.loads(self.rfile.read(length))
            except Exception:  # noqa: BLE001 - malformed input is the client's fault
                return self._json({"error": "bad payload"}, 400)
            url = payload.get("url") if isinstance(payload, dict) else None
            if not isinstance(url, str) or not url.strip():
                return self._json({"error": "missing or empty url"}, 400)
            record_hit(url)
            return self._send(204, b"")
        if path == "/api/annotations":
            sent = self.headers.get("X-Dash-Token", "")
            if TOKEN and not hmac.compare_digest(sent.encode(), TOKEN.encode()):
                return self._json({"error": "bad token"}, 401)
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length > 4096:
                    return self._json({"error": "payload too large"}, 413)
                payload = json.loads(self.rfile.read(length) or b"{}")
            except Exception as exc:  # noqa: BLE001
                return self._json({"error": f"bad payload: {exc}"}, 400)
            if not isinstance(payload, dict):
                return self._json({"error": "bad payload"}, 400)
            kind = payload.get("kind", "note")
            if kind not in ANNOTATION_KINDS:
                return self._json({"error": "bad kind"}, 400)
            text = payload.get("text")
            if not isinstance(text, str) or not text.strip():
                return self._json({"error": "missing text"}, 400)
            text = text.strip()[:MAX_ANNOTATION_TEXT]
            device_id = payload.get("device", "")
            if device_id is None:
                device_id = ""
            if not isinstance(device_id, str):
                return self._json({"error": "bad device"}, 400)
            device_id = device_id.strip()[:80]
            raw_ts = payload.get("ts")
            if raw_ts is None:
                ts = int(time.time())
            else:
                ts = _metric_number(raw_ts, int)
                if ts is None:
                    return self._json({"error": "bad ts"}, 400)
            with _db_lock:
                con = _connect_metrics()
                try:
                    cur = con.execute(
                        "INSERT INTO annotations (device_id, ts, kind, text) "
                        "VALUES (?, ?, ?, ?)",
                        (device_id, ts, kind, text),
                    )
                    con.commit()
                    annotation_id = cur.lastrowid
                finally:
                    con.close()
            # A note is brief content too, so the cached read model is stale.
            _invalidate_summary()
            return self._json({"ok": True, "id": annotation_id}, 201)
        if not path.startswith("/api/push/"):
            return self._send(404, "not found", "text/plain; charset=utf-8")
        sent = self.headers.get("X-Dash-Token", "")
        # Constant-time compare: a plain != leaks how many leading bytes matched,
        # which lets a caller recover the token a byte at a time.
        if TOKEN and not hmac.compare_digest(sent.encode(), TOKEN.encode()):
            return self._json({"error": "bad token"}, 401)
        name = path.rsplit("/", 1)[-1][:40] or "unknown"
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 65536:
                return self._json({"error": "payload too large"}, 413)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception as exc:  # noqa: BLE001
            return self._json({"error": f"bad payload: {exc}"}, 400)
        with _agents_lock:
            agents = load_agents()
            rec = dict(agents.get(name, {}))
            rec.update({k: payload[k] for k in (
                "cpu_pct", "mem_pct", "mem_used", "mem_total",
                "disk_pct", "uptime_s", "kind", "name") + EXTRA_METRICS
                if k in payload})
            if "disks" in payload:
                rec["disks"] = clean_disks(payload["disks"])
            # Health blocks replace per kind, only when that key was sent. A
            # command that failed is omitted by the agent, so the last known
            # reading (and a stale backup age) stays visible instead of
            # silently disappearing. An explicit empty block clears it.
            if isinstance(payload, dict):
                present = [kind for kind in HEALTH_KINDS if kind in payload]
                if present:
                    cleaned = clean_health(payload)
                    current = dict(rec.get("health") or {})
                    for kind in present:
                        if kind in cleaned:
                            current[kind] = cleaned[kind]
                        else:
                            current.pop(kind, None)
                    if current:
                        rec["health"] = current
                    else:
                        rec.pop("health", None)
            for key in CONTAINER_FIELDS:
                cleaned = clean_text(payload.get(key))
                if cleaned:
                    rec[key] = cleaned
            # Guest identity: a positive integer inside the sane range, or the
            # stored value stays as it was.
            for key in GUEST_INT_FIELDS:
                value = _metric_number(payload.get(key), int)
                if value is not None and 0 < value <= MAX_GUEST_INT:
                    rec[key] = value
            rec["last_seen"] = int(time.time())
            agents[name] = rec
            save_agents(agents)
        _invalidate_summary()
        return self._json({"ok": True, "stored": name})

if __name__ == "__main__":
    _local_cpu_pct()  # prime the cgroup CPU delta
    DATA.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    start_monitor()
    # Checks run on a sibling thread: a slow or dead URL must never delay the
    # device sampler, whose cadence history and availability depend on.
    start_checks()
    print(f"dash listening on :{PORT}  token={'set' if TOKEN else 'open'}", flush=True)
    server.serve_forever()
