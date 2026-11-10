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
import hashlib
import hmac
import json
import math
import os
import shutil
import threading
import time
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

_lock = threading.Lock()
_agents_lock = threading.Lock()
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
    devices = [local_device()] + agent_devices()
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
        path = self.path.split("?")[0]
        try:
            if path == "/api/stats":
                return self._json(stats())
            if path == "/api/bookmarks":
                return self._json_cached(bookmarks())
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
            rec["last_seen"] = int(time.time())
            agents[name] = rec
            save_agents(agents)
        return self._json({"ok": True, "stored": name})

if __name__ == "__main__":
    _local_cpu_pct()  # prime the cgroup CPU delta
    DATA.mkdir(parents=True, exist_ok=True)
    print(f"dash listening on :{PORT}  token={'set' if TOKEN else 'open'}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
