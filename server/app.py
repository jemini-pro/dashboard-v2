#!/usr/bin/env python3
"""Homelab dashboard server.

Serves the static dashboard, the bookmark store, and a device-stats API.
Stdlib only: no pip, no build step.

Endpoints
    GET  /                  static dashboard
    GET  /api/bookmarks     bookmark store (JSON, single source of truth)
    GET  /api/stats         device telemetry
    POST /api/hit           record a bookmark click (beacon)
    POST /api/push/<name>   agent heartbeat + metrics (needs X-Dash-Token)
    GET  /api/health        liveness
"""
import json
import math
import os
import shutil
import ssl
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"
DATA = Path(os.environ.get("DASH_DATA_DIR") or BASE / "data")
AGENTS_FILE = DATA / "agents.json"
HITS_FILE = DATA / "hits.json"

HALF_LIFE_DAYS = 14.0
HIT_RETENTION_DAYS = 90
HIT_RETENTION_SECONDS = HIT_RETENTION_DAYS * 86400
PORT = int(os.environ.get("DASH_PORT", "8000"))
TOKEN = os.environ.get("DASH_TOKEN", "").strip()

PVE_HOST = os.environ.get("PVE_HOST", "").strip()
PVE_NODE = os.environ.get("PVE_NODE", "pve").strip()
PVE_TOKEN = os.environ.get("PVE_TOKEN", "").strip()

_lock = threading.Lock()
_hits_lock = threading.Lock()
_cache = {"at": 0.0, "body": None}
_prev_cpu = {"usec": None, "at": None}


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
    return {
        "id": "docker01",
        "name": "docker01",
        "kind": "lxc",
        "source": "local",
        "cpu_pct": _local_cpu_pct(),
        "mem_pct": round(100.0 * used / total, 1) if total and used is not None else None,
        "mem_used": used,
        "mem_total": total,
        "disk_pct": round(100.0 * du.used / du.total, 1) if du.total else None,
        "uptime_s": _uptime(),
        "online": True,
        "last_seen": int(time.time()),
    }


# ---------- proxmox ----------

def _pve_get(path):
    url = f"https://{PVE_HOST}:8006/api2/json{path}"
    req = urllib.request.Request(url, headers={"Authorization": f"PVEAPIToken={PVE_TOKEN}"})
    ctx = ssl._create_unverified_context()
    with urllib.request.urlopen(req, timeout=5, context=ctx) as r:
        return json.load(r)["data"]


def proxmox_devices():
    if not (PVE_HOST and PVE_TOKEN):
        return []
    try:
        node = _pve_get(f"/nodes/{PVE_NODE}/status")
        dev = {
            "id": "pve",
            "name": "pve",
            "kind": "host",
            "source": "proxmox",
            "cpu_pct": round(100.0 * node.get("cpu", 0), 1),
            "mem_pct": round(100.0 * node["memory"]["used"] / node["memory"]["total"], 1)
            if node.get("memory", {}).get("total") else None,
            "mem_used": node.get("memory", {}).get("used"),
            "mem_total": node.get("memory", {}).get("total"),
            "disk_pct": round(100.0 * node["rootfs"]["used"] / node["rootfs"]["total"], 1)
            if node.get("rootfs", {}).get("total") else None,
            "uptime_s": node.get("uptime"),
            "online": True,
            "last_seen": int(time.time()),
        }
        out = [dev]
        for g in _pve_get(f"/nodes/{PVE_NODE}/lxc"):
            out.append({
                "id": f"ct{g['vmid']}",
                "name": g.get("name") or f"ct{g['vmid']}",
                "kind": "lxc-guest",
                "source": "proxmox",
                "cpu_pct": round(100.0 * g.get("cpu", 0), 1),
                "mem_pct": round(100.0 * g["mem"] / g["maxmem"], 1) if g.get("maxmem") else None,
                "mem_used": g.get("mem"),
                "mem_total": g.get("maxmem"),
                "disk_pct": None,
                "uptime_s": g.get("uptime"),
                "online": g.get("status") == "running",
                "last_seen": int(time.time()),
            })
        return out
    except Exception as exc:  # noqa: BLE001 - surface the reason, never crash the page
        return [{
            "id": "pve", "name": "pve", "kind": "host", "source": "proxmox",
            "cpu_pct": None, "mem_pct": None, "disk_pct": None, "uptime_s": None,
            "online": False, "last_seen": int(time.time()),
            "error": f"{type(exc).__name__}: {exc}",
        }]


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


def agent_devices():
    now = time.time()
    out = []
    for name, rec in load_agents().items():
        age = now - rec.get("last_seen", 0)
        out.append({
            "id": f"agent:{name}",
            "name": rec.get("name", name),
            "kind": rec.get("kind", "pc"),
            "source": "agent",
            "cpu_pct": rec.get("cpu_pct"),
            "mem_pct": rec.get("mem_pct"),
            "mem_used": rec.get("mem_used"),
            "mem_total": rec.get("mem_total"),
            "disk_pct": rec.get("disk_pct"),
            "uptime_s": rec.get("uptime_s"),
            "online": age < 150,  # agents push every 60 s; allow two misses
            "last_seen": int(rec.get("last_seen", 0)),
        })
    return out


# ---------- aggregate ----------

def stats():
    now = time.time()
    with _lock:
        if _cache["body"] and now - _cache["at"] < 5:
            return _cache["body"]
    devices = [local_device()] + proxmox_devices() + agent_devices()
    body = {"generated": int(now), "devices": devices}
    with _lock:
        _cache["at"] = now
        _cache["body"] = body
    return body


def bookmarks():
    try:
        with open(DATA / "bookmarks.json") as f:
            store = json.load(f)
    except (OSError, ValueError):  # a fresh install has no store yet
        return {"work": []}
    scores = hit_scores()
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


# ---------- click history ----------

def load_hits():
    try:
        with open(HITS_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 - a missing store means no clicks yet
        return {}


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
        live = [
            ts for ts in stamps
            if not isinstance(ts, bool) and isinstance(ts, (int, float)) and ts >= cutoff
        ]
        if live:
            kept[url] = live
    return kept


def record_hit(url):
    now = time.time()
    with _hits_lock:
        hits = load_hits()
        hits.setdefault(url, []).append(now)
        save_hits(trim_hits(hits, now))


def hit_scores(now=None):
    """Decayed click weight per URL: sum(exp(-age_days / HALF_LIFE_DAYS))."""
    now = time.time() if now is None else now
    scores = {}
    for url, stamps in load_hits().items():
        if not isinstance(stamps, list):
            continue
        total = 0.0
        for ts in stamps:
            if isinstance(ts, bool) or not isinstance(ts, (int, float)):
                continue
            age_days = (now - ts) / 86400.0
            if 0 <= age_days <= HIT_RETENTION_DAYS:
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
}


class Handler(BaseHTTPRequestHandler):
    server_version = "dash/1.0"

    def log_message(self, fmt, *args):  # quieter logs
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj))

    def do_GET(self):
        path = self.path.split("?")[0]
        try:
            if path == "/api/stats":
                return self._json(stats())
            if path == "/api/bookmarks":
                return self._json(bookmarks())
            if path == "/api/health":
                return self._json({"ok": True, "pve": bool(PVE_HOST and PVE_TOKEN)})
        except Exception as exc:  # noqa: BLE001
            return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        target = (STATIC / rel).resolve()
        if not str(target).startswith(str(STATIC)) or not target.is_file():
            return self._send(404, "not found", "text/plain; charset=utf-8")
        ctype = CONTENT_TYPES.get(target.suffix, "application/octet-stream")
        return self._send(200, target.read_bytes(), ctype)

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/api/hit":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length))
            except Exception:  # noqa: BLE001 - malformed input is the client's fault
                return self._json({"error": "bad payload"}, 400)
            url = payload.get("url") if isinstance(payload, dict) else None
            if not isinstance(url, str):
                return self._json({"error": "missing url"}, 400)
            record_hit(url)
            return self._send(204, b"")
        if not path.startswith("/api/push/"):
            return self._send(404, "not found", "text/plain; charset=utf-8")
        if TOKEN and self.headers.get("X-Dash-Token") != TOKEN:
            return self._json({"error": "bad token"}, 401)
        name = path.rsplit("/", 1)[-1][:40] or "unknown"
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception as exc:  # noqa: BLE001
            return self._json({"error": f"bad payload: {exc}"}, 400)
        agents = load_agents()
        rec = dict(agents.get(name, {}))
        rec.update({k: payload[k] for k in (
            "cpu_pct", "mem_pct", "mem_used", "mem_total",
            "disk_pct", "uptime_s", "kind", "name") if k in payload})
        rec["last_seen"] = int(time.time())
        agents[name] = rec
        save_agents(agents)
        return self._json({"ok": True, "stored": name})


if __name__ == "__main__":
    _local_cpu_pct()  # prime the cgroup CPU delta
    DATA.mkdir(parents=True, exist_ok=True)
    print(f"dash listening on :{PORT}  pve={'on' if PVE_HOST and PVE_TOKEN else 'off'}  token={'set' if TOKEN else 'open'}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
