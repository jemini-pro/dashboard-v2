#!/usr/bin/env python3
"""Homelab dashboard server.

Serves the static dashboard, the bookmark store, and a device-stats API.
Stdlib only: no pip, no build step.

Endpoints
    GET  /                  static dashboard
    GET  /api/bookmarks     bookmark store (JSON, single source of truth)
    GET  /api/stats         device telemetry (live, plus 24 h availability)
    GET  /api/metrics       one device's history for a window
    GET  /api/metrics/summary   every device's derived history summary
    POST /api/hit           record a bookmark click (beacon)
    POST /api/push/<name>   agent heartbeat + metrics (needs X-Dash-Token)
    GET  /api/health        liveness
"""
import hashlib
import hmac
import json
import math
import os
import shutil
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"
DATA = Path(os.environ.get("DASH_DATA_DIR") or BASE / "data")
AGENTS_FILE = DATA / "agents.json"
HITS_FILE = DATA / "hits.json"
METRICS_FILE = DATA / "metrics.db"

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
PRUNE_INTERVAL = 3600
METRIC_WINDOWS = {
    "1h": 3600,
    "6h": 6 * 3600,
    "24h": 86400,
    "7d": 7 * 86400,
    "30d": 30 * 86400,
}
DEFAULT_WINDOW = "24h"
# Each default lands at or below MAX_POINTS so a chart never gets a payload it
# cannot draw. An explicit ?step= is clamped the same way.
DEFAULT_STEPS = {"1h": 60, "6h": 300, "24h": 300, "7d": 1800, "30d": 7200}
MAX_POINTS = 400

_lock = threading.Lock()
_agents_lock = threading.Lock()
_hits_lock = threading.Lock()
_db_lock = threading.Lock()
_cache = {"at": 0.0, "body": None}
_prev_cpu = {"usec": None, "at": None}
_availability_cache = {"at": 0.0, "by_id": {}}
_last_prune = {"at": 0.0}


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
        "id": "docker01",
        "name": "docker01",
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


def worst_disk_pct(disks, fallback):
    """The DSK cell summarises the fullest volume, so a nearly full data or
    external drive cannot hide behind a roomy root filesystem."""
    values = [value for d in disks if (value := _finite(d.get("pct"))) is not None]
    return max(values) if values else fallback


def agent_devices():
    now = time.time()
    out = []
    for name, rec in load_agents().items():
        age = now - rec.get("last_seen", 0)
        disks = rec.get("disks")
        disks = disks if isinstance(disks, list) else []
        out.append({
            "id": f"agent:{name}",
            "name": rec.get("name", name),
            "kind": rec.get("kind", "pc"),
            "source": "agent",
            "cpu_pct": rec.get("cpu_pct"),
            "mem_pct": rec.get("mem_pct"),
            "mem_used": rec.get("mem_used"),
            "mem_total": rec.get("mem_total"),
            "disk_pct": worst_disk_pct(disks, rec.get("disk_pct")),
            "disks": disks,
            "uptime_s": rec.get("uptime_s"),
            "online": age < 150,  # agents push every 60 s; allow two misses
            "last_seen": int(rec.get("last_seen", 0)),
        })
    return out


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
                wrote += 1
            con.commit()
        finally:
            con.close()
    prune_metrics(now)
    return wrote


def sample_all(now=None):
    """Sample the local device and every known agent once."""
    now = time.time() if now is None else now
    return record_samples([local_device()] + agent_devices(), now=now)


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
            cutoff = int(now) - HISTORY_SECONDS
            cur = con.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
            removed = cur.rowcount
            cur = con.execute("DELETE FROM disk_samples WHERE ts < ?", (cutoff,))
            con.commit()
            return removed + cur.rowcount
        finally:
            con.close()


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
    state["docker01"] = {"online": True, "last_seen": int(now)}
    return state


def _history_rows(con, device_id, since, until):
    return con.execute(
        "SELECT ts, cpu_pct, mem_pct, disk_pct, uptime_s, online FROM samples "
        "WHERE device_id = ? AND ts >= ? AND ts <= ? ORDER BY ts",
        (device_id, int(since), int(until)),
    ).fetchall()


def _events(rows, limit=None):
    """Offline/recovery/restart transitions, newest first."""
    events = []
    offline_since = None
    prev = None
    for row in rows:
        ts, _cpu, _mem, _disk, uptime, online = row
        if prev is None:
            if not online:
                offline_since = ts
        else:
            if prev[5] and not online:
                offline_since = ts
                events.append({"ts": int(ts), "type": "offline"})
            elif not prev[5] and online:
                down = int(ts - offline_since) if offline_since is not None else None
                events.append({"ts": int(ts), "type": "online", "down_s": down})
                offline_since = None
            # A reboot resets uptime; a one-sample wobble should not count.
            # Compare even across an offline run: a machine that rebooted and
            # then reconnected has a lower uptime than its last pre-outage row.
            if uptime is not None and prev[4] is not None and online:
                if uptime < prev[4] - 60:
                    events.append({"ts": int(ts), "type": "restart"})
        prev = row
    events.sort(key=lambda event: event["ts"], reverse=True)
    return events[:limit] if limit else events


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


def _disk_growth_per_day(rows):
    """Least-squares disk slope in percentage points per day, online only."""
    return _trend_per_day([
        (ts, disk) for ts, _cpu, _mem, disk, _uptime, online in rows
        if online and disk is not None
    ])


def _derive_summary(rows, until):
    """Availability, averages, offline events and disk trend for one window."""
    count = len(rows)
    if not count:
        return {
            "samples": 0, "availability_pct": None, "downtime_s": 0,
            "offline_events": 0, "last_offline": None, "restarts": 0,
            "cpu_avg": None, "cpu_max": None, "mem_avg": None,
            "disk_pct": None, "disk_growth_per_day": None, "days_to_90": None,
        }
    online_rows = [row for row in rows if row[5]]
    cpu_values = [row[1] for row in online_rows if row[1] is not None]
    mem_values = [row[2] for row in online_rows if row[2] is not None]

    events = _events(rows)
    offline_events = [event for event in events if event["type"] == "offline"]

    downtime = 0
    offline_since = None
    for row in rows:
        ts, online = row[0], row[5]
        if not online and offline_since is None:
            offline_since = ts
        elif online and offline_since is not None:
            downtime += ts - offline_since
            offline_since = None
    if offline_since is not None:
        downtime += max(0, int(until) - offline_since)

    latest = rows[-1]
    disk_now = latest[3]
    growth = _disk_growth_per_day(rows)
    days_to_90 = _days_to_90(disk_now, growth)

    return {
        "samples": count,
        "availability_pct": round(100.0 * len(online_rows) / count, 1),
        "downtime_s": int(downtime),
        "offline_events": len(offline_events),
        "last_offline": int(offline_events[0]["ts"]) if offline_events else None,
        "restarts": sum(1 for event in events if event["type"] == "restart"),
        "cpu_pct": round(float(latest[1]), 1) if latest[1] is not None else None,
        "cpu_avg": round(sum(cpu_values) / len(cpu_values), 1) if cpu_values else None,
        "cpu_max": round(max(cpu_values), 1) if cpu_values else None,
        "mem_pct": round(float(latest[2]), 1) if latest[2] is not None else None,
        "mem_avg": round(sum(mem_values) / len(mem_values), 1) if mem_values else None,
        "disk_pct": round(float(disk_now), 1) if disk_now is not None else None,
        "disk_growth_per_day": round(growth, 2) if growth is not None else None,
        "days_to_90": days_to_90,
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


def _volume_series(con, device_id, since, until, step):
    """Per-volume bucketed pct series, latest size and a growth trend.

    Online-only, matching the aggregate disk gauge: an agent that stopped
    pushing keeps its last values in agents.json, but those stale values must
    not look like fresh volume history. One grouped query for the series plus
    one latest-row query per mount (at most MAX_DISKS) keeps this bounded.
    """
    rows = con.execute(
        "SELECT d.mount, d.ts - (d.ts % ?) AS bucket, ROUND(AVG(d.pct), 1) "
        "FROM disk_samples d "
        "JOIN samples s ON s.device_id = d.device_id AND s.ts = d.ts "
        "WHERE d.device_id = ? AND d.ts >= ? AND d.ts <= ? AND s.online = 1 "
        "GROUP BY d.mount, bucket ORDER BY d.mount, bucket",
        (step, device_id, int(since), int(until)),
    ).fetchall()
    series = {}
    for mount, bucket, value in rows:
        series.setdefault(mount, []).append([int(bucket), value])

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

    The fullest volume is the one the aggregate DSK gauge already reports, so
    this is the mount worth projecting days-to-90 for without shipping every
    series in the fleet summary.
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
    """Every known device with its derived window summary and a CPU sparkline."""
    now = time.time() if now is None else now
    window_s = _window_seconds(window) or METRIC_WINDOWS[DEFAULT_WINDOW]
    since = now - window_s
    payload = {"generated": int(now), "window": window, "window_s": window_s,
               "devices": []}
    if not METRICS_FILE.exists():
        return payload
    live = _live_state(now)
    volume_step = _step_for(window_s, None)
    con = _connect_metrics()
    try:
        for device_id, name, kind, _first, last_seen in _known_devices(con):
            rows = _history_rows(con, device_id, since, now)
            summary = _derive_summary(rows, now)
            state = live.get(device_id, {})
            payload["devices"].append({
                "id": device_id,
                "name": name,
                "kind": kind,
                "online": state.get("online", bool(rows[-1][5]) if rows else False),
                "last_seen": state.get("last_seen", int(last_seen)),
                "spark": _spark(rows, now),
                "volumes": _volume_summary(
                    _volume_series(con, device_id, since, now, volume_step)
                ),
                **summary,
            })
    finally:
        con.close()
    return payload


def metrics_series(device_id, window=DEFAULT_WINDOW, requested_step=None, now=None):
    """One device's bucketed time series, derived summary and events."""
    now = time.time() if now is None else now
    window_s = _window_seconds(window) or METRIC_WINDOWS[DEFAULT_WINDOW]
    step = _step_for(window_s, requested_step)
    since = now - window_s
    payload = {
        "generated": int(now), "window": window, "window_s": window_s, "step": step,
        "device": None, "summary": _derive_summary([], now), "points": [],
        "volumes": [], "events": [],
    }
    if not METRICS_FILE.exists():
        return None if device_id else payload
    live = _live_state(now)
    con = _connect_metrics()
    try:
        meta = con.execute(
            "SELECT id, name, kind, last_seen FROM devices WHERE id = ?", (device_id,)
        ).fetchone()
        if meta is None:
            return None
        rows = _history_rows(con, device_id, since, now)
        points = con.execute(
            "SELECT ts - (ts % ?) AS bucket, "
            "ROUND(AVG(cpu_pct), 1), ROUND(AVG(mem_pct), 1), "
            "ROUND(AVG(disk_pct), 1), MIN(online) "
            "FROM samples WHERE device_id = ? AND ts >= ? AND ts <= ? "
            "GROUP BY bucket ORDER BY bucket",
            (step, device_id, int(since), int(now)),
        ).fetchall()
        volumes = _volume_series(con, device_id, since, now, step)
    finally:
        con.close()
    state = live.get(device_id, {})
    payload["device"] = {
        "id": meta[0], "name": meta[1], "kind": meta[2],
        "online": state.get("online", bool(rows[-1][5]) if rows else False),
        "last_seen": state.get("last_seen", int(meta[3])),
    }
    payload["summary"] = _derive_summary(rows, now)
    payload["points"] = [list(point) for point in points]
    payload["volumes"] = volumes
    payload["events"] = _events(rows, limit=20)
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
    body = {"generated": int(now), "devices": devices}
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
                "disk_pct", "uptime_s", "kind", "name") if k in payload})
            if "disks" in payload:
                rec["disks"] = clean_disks(payload["disks"])
            rec["last_seen"] = int(time.time())
            agents[name] = rec
            save_agents(agents)
        return self._json({"ok": True, "stored": name})

if __name__ == "__main__":
    _local_cpu_pct()  # prime the cgroup CPU delta
    DATA.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    start_monitor()
    print(f"dash listening on :{PORT}  token={'set' if TOKEN else 'open'}", flush=True)
    server.serve_forever()
