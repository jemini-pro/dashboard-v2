#!/usr/bin/env bash
# Black-box acceptance check for click history, decayed bookmark scores and
# Workbench deployment documentation. Spec #16 ticket #37; spec #40 ticket #46.
#
# Dependencies: bash, curl, coreutils, and python3 (stdlib only -- the server is
# stdlib Python as well). No docker, node or pip. Starts the real server on a
# free port against a throwaway DASH_DATA_DIR, drives it with curl, then removes
# the temp directory and the process on exit. Safe to run in CI.
#
# Usage: server/check_api.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVER_DIR="$REPO/server"
APP="$SERVER_DIR/app.py"
COMPOSE="$SERVER_DIR/docker-compose.yml"
README="$SERVER_DIR/README.md"
TMP="$(mktemp -d)"
DATA_DIR="$TMP/data"
HITS="$DATA_DIR/hits.json"
BOOKMARKS="$DATA_DIR/bookmarks.json"
PORT=""
BASE=""
PID=""
COUNT=0

# Representative URLs from the tracked bookmark seed.
W_MAIL="https://mail.google.com"
W_CAL="https://calendar.google.com"
W_SHEETS="https://docs.google.com/spreadsheets"
W_DOCS="https://docs.google.com/document"
W_FORMS="https://docs.google.com/forms"
W_DRIVE="https://drive.google.com/drive/u/0/folders/0AAZUVdPYQtvyUk9PVA"

cleanup() {
    if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null; then
        kill "$PID" 2>/dev/null || true
        wait "$PID" 2>/dev/null || true
    fi
    rm -rf "$TMP"
}
trap cleanup EXIT

fail() {
    echo "not ok - $*" >&2
    if [[ -s "$TMP/server.log" ]]; then
        echo "--- server log (tail) ---" >&2
        tail -n 40 "$TMP/server.log" >&2
    fi
    exit 1
}

ok() {
    COUNT=$((COUNT + 1))
    printf 'ok %02d - %s\n' "$COUNT" "$*"
}

# ---------- helpers ----------

free_port() {
    python3 - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
}

start_server() {
    PORT="$(free_port)"
    BASE="http://127.0.0.1:$PORT"
    DASH_DATA_DIR="$DATA_DIR" DASH_PORT="$PORT" DASH_TOKEN= DASH_SAMPLE_SECONDS=1 \
        python3 -u "$APP" >>"$TMP/server.log" 2>&1 &
    PID=$!
    local i
    for i in $(seq 1 100); do
        if curl -fsS "$BASE/api/health" >/dev/null 2>&1; then
            return 0
        fi
        kill -0 "$PID" 2>/dev/null || fail "server exited during startup"
        sleep 0.05
    done
    fail "server did not become healthy on port $PORT"
}

stop_server() {
    if [[ -n "$PID" ]]; then
        kill "$PID" 2>/dev/null || true
        wait "$PID" 2>/dev/null || true
    fi
    PID=""
}

post_hit() { # post_hit <url> -> http status; body lands in $TMP/hit.body
    curl -sS -o "$TMP/hit.body" -w '%{http_code}' \
        -H 'Content-Type: application/json' \
        -d "{\"url\":\"$1\"}" "$BASE/api/hit"
}

get_bookmarks() {
    curl -fsS "$BASE/api/bookmarks" -o "$TMP/bookmarks.json"
}

json_item_field() { # json_item_field <file> <url> <field>
    python3 - "$1" "$2" "$3" <<'PY'
import json, sys
path, url, field = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path) as f:
    store = json.load(f)
categories = store.values() if isinstance(store, dict) else []
for category_list in categories:
    if not isinstance(category_list, list):
        continue
    for category in category_list:
        if not isinstance(category, dict):
            continue
        for item in category.get("items", []):
            if isinstance(item, dict) and item.get("url") == url:
                print(item.get(field, ""))
                raise SystemExit(0)
raise SystemExit(f"{field} absent from /api/bookmarks: {url}")
PY
}

json_item_count() {
    python3 - "$1" <<'PY'
import json, sys
with open(sys.argv[1]) as f:
    store = json.load(f)
print(sum(len(c.get("items", [])) for cs in store.values() for c in cs))
PY
}

assert_close() { # assert_close <got> <want> [tol]
    python3 - "$1" "$2" "${3:-0.001}" <<'PY'
import sys
got, want, tol = (float(x) for x in sys.argv[1:4])
if abs(got - want) > tol:
    raise SystemExit(f"got {got}, want {want} (tol {tol})")
PY
}

assert_gt() { # assert_gt <a> <b>
    python3 - "$1" "$2" <<'PY'
import sys
a, b = (float(x) for x in sys.argv[1:3])
if not a > b:
    raise SystemExit(f"expected {a} > {b}")
PY
}

wait_for_samples() {
    local i
    for i in $(seq 1 100); do
        if curl -fsS "$BASE/api/metrics/summary?window=1h" -o "$TMP/metrics-summary.json" 2>/dev/null; then
            if python3 - "$TMP/metrics-summary.json" <<'PY'
import json, sys
with open(sys.argv[1]) as f:
    data = json.load(f)
raise SystemExit(0 if any(d.get("samples", 0) > 0 for d in data.get("devices", [])) else 1)
PY
            then
                return 0
            fi
        fi
        sleep 0.1
    done
    fail "the metrics sampler wrote no sample within 10 s"
}

metrics_summary_field() { # metrics_summary_field <device-id> <field>
    python3 - "$TMP/metrics-summary.json" "$1" "$2" <<'PY'
import json, sys
path, device_id, field = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path) as f:
    data = json.load(f)
for device in data.get("devices", []):
    if device.get("id") == device_id:
        print(device.get(field, ""))
        raise SystemExit(0)
raise SystemExit(f"{device_id} absent from the metrics summary")
PY
}

metrics_min_ts() {
    python3 - "$DATA_DIR/metrics.db" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
try:
    row = con.execute("SELECT MIN(ts) FROM samples").fetchone()
finally:
    con.close()
print(row[0] if row and row[0] is not None else "")
PY
}

disk_min_ts() {
    python3 - "$DATA_DIR/metrics.db" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
try:
    row = con.execute("SELECT MIN(ts) FROM disk_samples").fetchone()
finally:
    con.close()
print(row[0] if row and row[0] is not None else "")
PY
}

push_device() { # push_device <name> <json-file>
    curl -fsS -X POST -H 'Content-Type: application/json' \
        --data-binary @"$2" "$BASE/api/push/$1" >/dev/null
}

wait_for_signals() { # wait_for_signals <device-id>
    local id="$1" i
    for i in $(seq 1 120); do
        if curl -fsS "$BASE/api/metrics?device=$id&window=1h" \
                -o "$TMP/sig-series.json" 2>/dev/null; then
            if python3 - "$TMP/sig-series.json" <<'PY'
import json, sys
with open(sys.argv[1]) as f:
    data = json.load(f)
raise SystemExit(0 if (data.get("signals") or {}).get("load_1") else 1)
PY
            then
                return 0
            fi
        fi
        sleep 0.1
    done
    fail "no signal samples for $id within 12 s"
}

wait_for_device() { # wait_for_device <device-id>
    local id="$1" i
    for i in $(seq 1 120); do
        if curl -fsS "$BASE/api/metrics?device=$id&window=1h" >/dev/null 2>&1; then
            return 0
        fi
        sleep 0.1
    done
    fail "device $id did not appear within 12 s"
}

wait_for_auto_restart() { # wait_for_auto_restart <device-id>
    local id="$1" i
    for i in $(seq 1 140); do
        if curl -fsS "$BASE/api/metrics?device=$id&window=1h" \
                -o "$TMP/restart-series.json" 2>/dev/null; then
            if python3 - "$TMP/restart-series.json" <<'PY'
import json, sys
with open(sys.argv[1]) as f:
    data = json.load(f)
raise SystemExit(0 if any(a.get("auto") and a.get("kind") == "restart"
                          for a in data.get("annotations", [])) else 1)
PY
            then
                return 0
            fi
        fi
        sleep 0.1
    done
    fail "no auto restart annotation for $id within 14 s"
}

annotation_count() {
    python3 - "$DATA_DIR/metrics.db" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
try:
    row = con.execute("SELECT COUNT(*) FROM annotations").fetchone()
finally:
    con.close()
print(row[0])
PY
}

metric_min_ts() {
    python3 - "$DATA_DIR/metrics.db" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
try:
    row = con.execute("SELECT MIN(ts) FROM metric_samples").fetchone()
finally:
    con.close()
print(row[0] if row and row[0] is not None else "")
PY
}

wait_for_volume_samples() { # wait_for_volume_samples <device-id> <min-count>
    local id="$1" want="$2" i
    for i in $(seq 1 120); do
        if curl -fsS "$BASE/api/metrics/summary?window=1h" \
                -o "$TMP/metrics-summary.json" 2>/dev/null; then
            if python3 - "$TMP/metrics-summary.json" "$id" "$want" <<'PY'
import json, sys
with open(sys.argv[1]) as f:
    data = json.load(f)
for device in data.get("devices", []):
    if device.get("id") == sys.argv[2]:
        count = (device.get("volumes") or {}).get("count", 0)
        raise SystemExit(0 if count >= int(sys.argv[3]) else 1)
raise SystemExit(1)
PY
            then
                return 0
            fi
        fi
        sleep 0.1
    done
    fail "no per-volume samples for $id within 12 s"
}

hits_len() { # hits_len <url>
    python3 - "$HITS" "$1" <<'PY'
import json, sys
with open(sys.argv[1]) as f:
    hits = json.load(f)
print(len(hits.get(sys.argv[2], [])))
PY
}

write_bookmarks() {
    # The seed is the tracked bookmark fixture the server injects; the front
    # end no longer carries an embedded copy of these links.
    cp "$SERVER_DIR/bookmarks.seed.json" "$BOOKMARKS"
    python3 - "$BOOKMARKS" <<'PY'
import json, sys
with open(sys.argv[1]) as f:
    store = json.load(f)
items = [item for cats in store.values() for cat in cats for item in cat["items"]]
if len(items) < 31:
    raise SystemExit(f"expected at least 31 seed bookmarks, parsed {len(items)}")
PY
}

seed_hits() {
    python3 - "$HITS" "$W_CAL" "$W_SHEETS" "$W_DOCS" "$W_FORMS" "$W_DRIVE" <<'PY'
import json, os, sys, time
path = sys.argv[1]
cal, sheets, docs, forms, drive = sys.argv[2:7]
hits = {}
if os.path.exists(path):
    with open(path) as f:
        hits = json.load(f)
now = time.time()
# Decay ordering: one week < four weeks < sixty days.
hits[cal] = [now - 7 * 86400]
hits[sheets] = [now - 28 * 86400]
hits[docs] = [now - 60 * 86400]
# A worked example for the 14-day half life: exp(-14/14) = 0.3679 -> 0.37.
hits[drive] = [now - 14 * 86400]
# Older than the 90-day retention window: dropped when a new hit is written.
hits[forms] = [now - 91 * 86400, now - 100 * 86400]
with open(path, "w") as f:
    json.dump(hits, f)
PY
}

pin_item() { # pin_item <url> - add "pinned": true to a fixture item
    python3 - "$BOOKMARKS" "$1" <<'PY'
import json, sys
path, url = sys.argv[1], sys.argv[2]
with open(path) as f:
    store = json.load(f)
for category_list in store.values():
    if not isinstance(category_list, list):
        continue
    for category in category_list:
        for item in category.get("items", []):
            if item.get("url") == url:
                item["pinned"] = True
with open(path, "w") as f:
    json.dump(store, f)
PY
}

rename_mail() {
    python3 - "$BOOKMARKS" "$W_MAIL" <<'PY'
import json, sys
with open(sys.argv[1]) as f:
    store = json.load(f)
for category in store["work"]:
    for item in category["items"]:
        if item["url"] == sys.argv[2]:
            item["title"] = "Gmail (renamed)"
with open(sys.argv[1], "w") as f:
    json.dump(store, f)
PY
}

# ---------- run ----------

mkdir -p "$DATA_DIR"
write_bookmarks
start_server
ok "server started on an ephemeral port with a throwaway DASH_DATA_DIR"

# 1. The Launchpad is data-driven: a pinned flag passes through unchanged.
pin_item "$W_MAIL"
get_bookmarks
[[ "$(json_item_field "$TMP/bookmarks.json" "$W_MAIL" pinned)" == "True" ]] \
    || fail "a pinned flag did not survive /api/bookmarks"
ok "/api/bookmarks passes a pinned flag through unchanged"

# 2. POST /api/hit -> 204, no body, timestamp stored.
code="$(post_hit "$W_MAIL")"
[[ "$code" == "204" ]] || fail "POST /api/hit returned $code, expected 204"
[[ ! -s "$TMP/hit.body" ]] || fail "POST /api/hit returned a body"
[[ -f "$HITS" ]] || fail "hits.json was not created under DASH_DATA_DIR"
[[ "$(hits_len "$W_MAIL")" == "1" ]] \
    || fail "one hit was not stored for $W_MAIL"
ok "POST /api/hit returns 204 with no body and writes hits.json"

# 3. Score appears in /api/bookmarks and rises with repeated clicks.
get_bookmarks
score_one="$(json_item_field "$TMP/bookmarks.json" "$W_MAIL" score)" \
    || fail "score missing for $W_MAIL"
assert_close "$score_one" 1.0 0.01 || fail "one click scored $score_one, expected 1.0"
post_hit "$W_MAIL" >/dev/null
post_hit "$W_MAIL" >/dev/null
get_bookmarks
score_three="$(json_item_field "$TMP/bookmarks.json" "$W_MAIL" score)"
assert_close "$score_three" 3.0 0.01 || fail "three clicks scored $score_three, expected 3.0"
assert_gt "$score_three" "$score_one" || fail "score did not rise: $score_one -> $score_three"
ok "score appears in /api/bookmarks and rises on repeated clicks (1.0 -> 3.0)"

# 4. Decay ordering and the documented half life.
seed_hits
get_bookmarks
s7="$(json_item_field "$TMP/bookmarks.json" "$W_CAL" score)"
s28="$(json_item_field "$TMP/bookmarks.json" "$W_SHEETS" score)"
s60="$(json_item_field "$TMP/bookmarks.json" "$W_DOCS" score)"
s14="$(json_item_field "$TMP/bookmarks.json" "$W_DRIVE" score)"
assert_gt "$s7" "$s28" || fail "7-day hit ($s7) did not outrank 28-day hit ($s28)"
assert_gt "$s28" "$s60" || fail "28-day hit ($s28) did not outrank 60-day hit ($s60)"
assert_gt "$s60" 0 || fail "60-day hit scored zero, expected non-zero"
assert_close "$s14" 0.37 0.005 || fail "14-day half life scored $s14, expected 0.37"
ok "older hits decay: 7d=$s7 > 28d=$s28 > 60d=$s60; 14d=$s14"

# 5. 90-day trimming happens on write.
[[ "$(hits_len "$W_FORMS")" == "2" ]] \
    || fail "seed hits for $W_FORMS were not written"
post_hit "$W_FORMS" >/dev/null
[[ "$(hits_len "$W_FORMS")" == "1" ]] \
    || fail "90-day-old hits were not trimmed on write"
python3 - "$HITS" "$W_FORMS" <<'PY' || fail "a retained hit is older than the 90-day window"
import json, sys, time
with open(sys.argv[1]) as f:
    stamps = json.load(f)[sys.argv[2]]
now = time.time()
assert len(stamps) == 1, stamps
assert max(now - ts for ts in stamps) <= 90 * 86400 + 5, stamps
PY
ok "hits older than 90 days are trimmed when a new hit is written"

# 6. Unknown URL is accepted (a link may since have been deleted).
code="$(post_hit https://deleted.example.com)"
[[ "$code" == "204" ]] || fail "unknown URL returned $code, expected 204"
[[ "$(hits_len https://deleted.example.com)" == "1" ]] \
    || fail "unknown URL hit was not stored"
ok "unknown URL is accepted with 204 and stored"

# 7. Renaming a title keeps the score (history is keyed by URL).
score_before="$(json_item_field "$TMP/bookmarks.json" "$W_MAIL" score)"
rename_mail
get_bookmarks
title_after="$(json_item_field "$TMP/bookmarks.json" "$W_MAIL" title)"
score_after="$(json_item_field "$TMP/bookmarks.json" "$W_MAIL" score)"
[[ "$title_after" == "Gmail (renamed)" ]] || fail "rename not visible: $title_after"
assert_close "$score_after" "$score_before" 0.001 \
    || fail "rename changed the score: $score_before -> $score_after"
ok "renaming a title keeps its score ($score_before -> $score_after)"

# 8. Payload size for the 31 representative links in the seed stays under ~10 KB.
items="$(json_item_count "$TMP/bookmarks.json")"
[[ "$items" == "31" ]] \
    || fail "expected 31 representative links in the seed, found $items"
title="$(json_item_field "$TMP/bookmarks.json" "$W_DRIVE" title)"
[[ "$title" == "Shared Drive" ]] \
    || fail "fixture is not the tracked seed (expected 'Shared Drive', got '$title')"
size="$(wc -c < "$TMP/bookmarks.json")"
[[ "$size" -lt 10240 ]] || fail "/api/bookmarks is $size bytes, over the ~10 KB budget"
ok "/api/bookmarks is $size bytes for $items links (under 10240)"

# 9. Server-side history: the sampler writes even though nobody is polling the
# browser, and the derived endpoints expose availability and points.
wait_for_samples
samples="$(metrics_summary_field docker01 samples)"
availability="$(metrics_summary_field docker01 availability_pct)"
[[ "$samples" -ge 1 ]] || fail "docker01 has no stored samples ($samples)"
[[ "$availability" == "100.0" ]] || fail "docker01 availability is $availability, expected 100.0"
python3 - "$TMP/metrics-summary.json" <<'PY' || fail "summary sparkline is not 12 points"
import json, sys
with open(sys.argv[1]) as f:
    data = json.load(f)
assert all(len(device.get("spark", [])) == 12 for device in data["devices"]), data
PY
ok "the server-side sampler stores history without a browser open ($samples samples)"

curl -fsS "$BASE/api/metrics?device=docker01&window=1h" -o "$TMP/metrics-series.json" \
    || fail "GET /api/metrics failed"
python3 - "$TMP/metrics-series.json" <<'PY' || fail "metrics series payload is wrong"
import json, sys
with open(sys.argv[1]) as f:
    data = json.load(f)
assert data["device"]["id"] == "docker01", data.get("device")
assert data["window"] == "1h", data.get("window")
assert data["step"] >= 60, data.get("step")
assert data["points"], "no points"
assert len(data["points"]) <= 400, len(data["points"])
assert len(data["points"][0]) == 5, data["points"][0]
assert "availability_pct" in data["summary"], data.get("summary")
assert data["source"] == "raw", data.get("source")
assert set(data["bands"]) == {"cpu", "mem", "disk"}, data.get("bands")
for key, band in data["bands"].items():
    assert len(band) == len(data["points"]), (key, len(band), len(data["points"]))
    assert all(len(row) == 3 for row in band), (key, band[:2])
PY
ok "/api/metrics returns bucketed points, a step and a derived summary"

# 11b. Rollup routing and peaks: 7d/30d read the 5-minute tier and 90d/1y the
# hourly tier, and every payload carries min/max bands aligned with points.
curl -fsS "$BASE/api/metrics?device=docker01&window=7d" \
    -o "$TMP/series-7d.json" || fail "GET 7d series failed"
python3 - "$TMP/series-7d.json" <<'PY' || fail "7d series is not rollup-backed"
import json, sys
with open(sys.argv[1]) as f:
    data = json.load(f)
assert data["source"] == "5m", data.get("source")
assert data["points"], "no 7d points"
assert len(data["bands"]["cpu"]) == len(data["points"])
PY
curl -fsS "$BASE/api/metrics?device=docker01&window=90d" \
    -o "$TMP/series-90d.json" || fail "GET 90d series failed"
python3 - "$TMP/series-90d.json" <<'PY' || fail "90d series is not hourly-rollup-backed"
import json, sys
with open(sys.argv[1]) as f:
    data = json.load(f)
assert data["source"] == "1h", data.get("source")
assert data["points"], "no 90d points"
assert len(data["bands"]["disk"]) == len(data["points"])
PY
python3 - "$DATA_DIR/metrics.db" <<'PY' || fail "a rollup tier has no rows"
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
try:
    for table in ("rollup_5m", "rollup_1h", "disk_rollup_5m", "disk_rollup_1h"):
        count = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        assert count > 0, f"{table} is empty"
finally:
    con.close()
PY
ok "7d/90d series route through 5-minute and hourly rollups with min/max bands"

curl -fsS "$BASE/api/metrics/summary?window=90d" \
    -o "$TMP/summary-90d.json" || fail "GET 90d summary failed"
python3 - "$TMP/summary-90d.json" <<'PY' || fail "90d summary is not rollup-backed"
import json, sys
with open(sys.argv[1]) as f:
    data = json.load(f)
assert data["source"] == "1h", data.get("source")
assert data["devices"], "no devices"
assert all(device["source"] == "1h" for device in data["devices"])
PY
ok "a long-window summary is answered from hourly rollups, not raw rows"

# 9b. Per-volume history. A push with a disks array must become its own rows,
# exposed as a bucketed series plus growth/day and days-to-90, without changing
# the shape of the existing points or summary.
cat > "$TMP/voltest-push.json" <<'JSON'
{
  "name": "voltest",
  "kind": "pc",
  "cpu_pct": 5.0,
  "mem_pct": 20.0,
  "mem_used": 1,
  "mem_total": 2,
  "disk_pct": 91.0,
  "uptime_s": 10,
  "disks": [
    {"mount": "/", "label": "System", "total": 1000, "used": 100, "pct": 10.0},
    {"mount": "/mnt/data", "label": "Data", "total": 1000, "used": 910, "pct": 91.0}
  ]
}
JSON
push_device voltest "$TMP/voltest-push.json" || fail "push with a disks array failed"
wait_for_volume_samples agent:voltest 2

python3 - "$TMP/metrics-summary.json" <<'PY' || fail "volume summary payload is wrong"
import json, sys
with open(sys.argv[1]) as f:
    data = json.load(f)
device = next(d for d in data["devices"] if d["id"] == "agent:voltest")
volumes = device["volumes"]
assert volumes["count"] == 2, volumes
assert volumes["fullest"]["mount"] == "/mnt/data", volumes
assert volumes["fullest"]["pct"] == 91.0, volumes
for key in ("growth_per_day", "days_to_90"):
    assert key in volumes["fullest"], volumes
assert len(device["spark"]) == 12
PY
ok "/api/metrics/summary exposes a compact per-device volume summary"

curl -fsS "$BASE/api/metrics?device=agent:voltest&window=1h" \
    -o "$TMP/voltest-series.json" || fail "GET per-volume series failed"
python3 - "$TMP/voltest-series.json" <<'PY' || fail "per-volume series payload is wrong"
import json, sys
with open(sys.argv[1]) as f:
    data = json.load(f)
volumes = {v["mount"]: v for v in data.get("volumes", [])}
assert set(volumes) == {"/", "/mnt/data"}, volumes
assert len(data["points"][0]) == 5, data["points"][0]
assert data["volumes"][0]["mount"] == "/mnt/data", data["volumes"]
for key in ("mount", "label", "total", "used", "pct",
            "growth_per_day", "days_to_90", "points"):
    assert key in volumes["/"], key
assert volumes["/mnt/data"]["pct"] == 91.0
assert volumes["/"]["points"], volumes["/"]
PY
ok "/api/metrics returns per-volume bucket points, growth and days-to-90"

# 9c. An oversized list is capped server-side: 26 volumes in, 16 stored.
python3 - "$TMP/volcap-push.json" <<'PY'
import json, sys
disks = [{"mount": "/vol%02d" % i, "label": "V%d" % i,
          "total": 1000, "used": 100, "pct": 10.0} for i in range(26)]
disks[20] = {"mount": "", "total": 0, "used": 0, "pct": 0.0}
with open(sys.argv[1], "w") as f:
    json.dump({"name": "volcap", "kind": "pc", "cpu_pct": 1.0,
               "mem_pct": 2.0, "disk_pct": 10.0, "uptime_s": 1,
               "disks": disks}, f)
PY
push_device volcap "$TMP/volcap-push.json" || fail "oversized disk push failed"
wait_for_volume_samples agent:volcap 16
python3 - "$TMP/metrics-summary.json" <<'PY' || fail "oversized volume list was not capped"
import json, sys
with open(sys.argv[1]) as f:
    data = json.load(f)
device = next(d for d in data["devices"] if d["id"] == "agent:volcap")
assert device["volumes"]["count"] == 16, device["volumes"]
PY
ok "an oversized volume list is capped at 16 and malformed entries are dropped"

# 9d. Extra signals: a synthetic payload proves the server side for agents we
# cannot run here (Windows), and an old-style payload proves backwards compat.
cat > "$TMP/sigtest-push.json" <<'JSON'
{
  "name": "sigtest",
  "kind": "pc",
  "cpu_pct": 5.0,
  "mem_pct": 20.0,
  "mem_used": 1,
  "mem_total": 2,
  "disk_pct": 10.0,
  "uptime_s": 10,
  "load_1": 0.8,
  "load_5": 0.6,
  "load_15": 0.5,
  "swap_used": 250,
  "swap_total": 1000,
  "net_rx_bps": 1200,
  "net_tx_bps": 300,
  "cpu_iowait": 1.5,
  "cpu_steal": 0.2
}
JSON
push_device sigtest "$TMP/sigtest-push.json" || fail "push with extra signals failed"
wait_for_signals agent:sigtest
python3 - "$TMP/sig-series.json" <<'PY' || fail "signal series payload is wrong"
import json, sys
with open(sys.argv[1]) as f:
    data = json.load(f)
signals = data["signals"]
assert signals["load_1"]["value"] == 0.8, signals
assert signals["load_1"]["points"], signals
assert signals["swap"]["pct"] == 25.0, signals
assert signals["swap"]["used"] == 250.0, signals
assert signals["net_rx_bps"]["value"] == 1200.0, signals
assert signals["cpu_iowait"]["value"] == 1.5, signals
assert "cpu_steal" in signals, signals
PY
ok "a push with extra signals exposes load, swap, network and CPU pressure"

cat > "$TMP/oldsig-push.json" <<'JSON'
{"name": "oldsig", "kind": "pc", "cpu_pct": 1.0, "mem_pct": 2.0,
 "mem_used": 1, "mem_total": 2, "disk_pct": 3.0, "uptime_s": 4}
JSON
push_device oldsig "$TMP/oldsig-push.json" || fail "old-style push failed"
wait_for_device agent:oldsig
curl -fsS "$BASE/api/metrics?device=agent:oldsig&window=1h" \
    -o "$TMP/oldsig-series.json" || fail "old-style series failed"
python3 - "$TMP/oldsig-series.json" <<'PY' || fail "old agent gained fabricated signals"
import json, sys
with open(sys.argv[1]) as f:
    data = json.load(f)
assert data["signals"] == {}, data["signals"]
PY
ok "an old agent without signals still works and gains no fabricated zeros"

curl -fsS "$BASE/api/metrics/summary?window=1h" \
    -o "$TMP/metrics-summary.json" || fail "summary after signals failed"
python3 - "$TMP/metrics-summary.json" <<'PY' || fail "summary signal values are wrong"
import json, sys
with open(sys.argv[1]) as f:
    data = json.load(f)
device = next(d for d in data["devices"] if d["id"] == "agent:sigtest")
assert device["signals"]["load_1"] == 0.8, device["signals"]
assert device["signals"]["swap_pct"] == 25.0, device["signals"]
assert device["signals"]["net_rx_bps"] == 1200.0, device["signals"]
old = next(d for d in data["devices"] if d["id"] == "agent:oldsig")
assert old["signals"] == {}, old["signals"]
PY
ok "/api/metrics/summary carries latest signals and none for old agents"

# 9e. Annotations are token-protected writes, rendered as markers and Events;
# warnings are evaluated server-side from the summary fields.
ann_ts="$(python3 -c 'import time; print(int(time.time()) - 60)')"
cat > "$TMP/annotation-push.json" <<JSON
{"device": "docker01", "kind": "deploy", "text": "check_api deploy", "ts": $ann_ts}
JSON
curl -fsS -X POST -H 'Content-Type: application/json' \
    --data-binary @"$TMP/annotation-push.json" \
    "$BASE/api/annotations" -o "$TMP/annotation.json" \
    || fail "POST /api/annotations failed"
python3 - "$TMP/annotation.json" <<'PY' || fail "annotation response is wrong"
import json, sys
with open(sys.argv[1]) as f:
    data = json.load(f)
assert data["ok"] is True and data["id"], data
PY
curl -fsS "$BASE/api/metrics?device=docker01&window=1h" \
    -o "$TMP/ann-series.json" || fail "GET series with annotations failed"
python3 - "$TMP/ann-series.json" <<'PY' || fail "annotation is missing from the chart/events"
import json, sys
with open(sys.argv[1]) as f:
    data = json.load(f)
manual = [a for a in data["annotations"] if a["text"] == "check_api deploy"]
assert manual and manual[0]["kind"] == "deploy" and not manual[0]["auto"], data["annotations"]
events = [e for e in data["events"]
          if e["type"] == "annotation" and e.get("text") == "check_api deploy"]
assert events, data["events"]
PY
ok "a deploy annotation appears as a chart marker and an Events row"

cat > "$TMP/warn-push.json" <<'JSON'
{"name": "warnagent", "kind": "pc", "cpu_pct": 1.0, "mem_pct": 95.0,
 "disk_pct": 96.0, "uptime_s": 10}
JSON
push_device warnagent "$TMP/warn-push.json" || fail "warning push failed"
wait_for_device agent:warnagent
curl -fsS "$BASE/api/metrics/summary?window=1h" \
    -o "$TMP/warn-summary.json" || fail "summary with warnings failed"
python3 - "$TMP/warn-summary.json" <<'PY' || fail "warnings were not evaluated"
import json, sys
with open(sys.argv[1]) as f:
    data = json.load(f)
device = next(d for d in data["devices"] if d["id"] == "agent:warnagent")
codes = {w["code"]: w["level"] for w in device["warnings"]}
assert codes.get("disk") == "critical", codes
assert codes.get("mem") == "warning", codes
PY
ok "server-side warnings flag disk and RAM thresholds with severity"

cat > "$TMP/restart1-push.json" <<'JSON'
{"name": "restartagent", "kind": "pc", "cpu_pct": 1.0, "uptime_s": 1000}
JSON
push_device restartagent "$TMP/restart1-push.json" || fail "restart push 1 failed"
wait_for_device agent:restartagent
cat > "$TMP/restart2-push.json" <<'JSON'
{"name": "restartagent", "kind": "pc", "cpu_pct": 1.0, "uptime_s": 10}
JSON
push_device restartagent "$TMP/restart2-push.json" || fail "restart push 2 failed"
wait_for_auto_restart agent:restartagent
ok "a detected restart is auto-annotated on the chart"

code="$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/metrics/summary?window=2h")"
[[ "$code" == "400" ]] || fail "unknown window returned $code, expected 400"
code="$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/metrics?window=1h")"
[[ "$code" == "400" ]] || fail "missing device returned $code, expected 400"
code="$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/metrics?device=nope&window=1h")"
[[ "$code" == "404" ]] || fail "unknown device returned $code, expected 404"
ok "metrics endpoints reject unknown windows, missing and unknown devices"

min_ts_before="$(metrics_min_ts)"
[[ -n "$min_ts_before" ]] || fail "metrics.db has no samples before the restart"
disk_min_before="$(disk_min_ts)"
[[ -n "$disk_min_before" ]] || fail "metrics.db has no per-volume samples before the restart"
metric_min_before="$(metric_min_ts)"
[[ -n "$metric_min_before" ]] || fail "metrics.db has no extra-signal samples before the restart"
annotations_before="$(annotation_count)"
[[ "$annotations_before" -ge 1 ]] || fail "no annotations before the restart"

# 10. Process restart: the same DASH_DATA_DIR keeps the history. This is the
# no-docker equivalent of recreating the `dash` container with its bind mount.
score_before="$(json_item_field "$TMP/bookmarks.json" "$W_MAIL" score)"
stamps_before="$(hits_len "$W_MAIL")"
stop_server
[[ -f "$HITS" ]] || fail "hits.json vanished when the server stopped"
start_server
get_bookmarks
score_restart="$(json_item_field "$TMP/bookmarks.json" "$W_MAIL" score)"
stamps_restart="$(hits_len "$W_MAIL")"
assert_close "$score_restart" "$score_before" 0.001 \
    || fail "score changed across restart: $score_before -> $score_restart"
[[ "$stamps_restart" == "$stamps_before" ]] \
    || fail "hit count changed across restart: $stamps_before -> $stamps_restart"
ok "hits survived a full server restart (score $score_before -> $score_restart)"

wait_for_samples
min_ts_after="$(metrics_min_ts)"
[[ "$min_ts_after" == "$min_ts_before" ]] \
    || fail "metrics history changed across restart: $min_ts_before -> $min_ts_after"
ok "metrics history survived a full server restart (first sample $min_ts_after)"

disk_min_after="$(disk_min_ts)"
[[ "$disk_min_after" == "$disk_min_before" ]] \
    || fail "per-volume history changed across restart: $disk_min_before -> $disk_min_after"
ok "per-volume history survived a full server restart (first sample $disk_min_after)"

metric_min_after="$(metric_min_ts)"
[[ "$metric_min_after" == "$metric_min_before" ]] \
    || fail "signal history changed across restart: $metric_min_before -> $metric_min_after"
ok "extra-signal history survived a full server restart (first sample $metric_min_after)"

annotations_after="$(annotation_count)"
[[ "$annotations_after" == "$annotations_before" ]] \
    || fail "annotations changed across restart: $annotations_before -> $annotations_after"
ok "annotations survived a full server restart ($annotations_after stored)"

# 11. Static guarantees: no cookie is set and the feature makes no third-party
# request. Deliberately scoped to the click-history/strip code so unrelated
# front-end code does not produce false positives.
grep -q "Set-Cookie" "$APP" && fail "server sets a cookie"
grep -q "document.cookie" "$REPO/app.js" && fail "front end touches document.cookie"
grep -qE 'calendar\.google\.com|fonts\.googleapis\.com|fonts\.gstatic\.com' \
    "$REPO/index.html" "$REPO/app.js" "$REPO/stack.js" "$REPO/style.css" \
    "$REPO/metrics.html" "$REPO/metrics.js" "$REPO/metrics.css" \
    && fail "front end still references the calendar iframe or Google Fonts"
grep -qE 'https?://|document\.cookie' "$REPO/metrics.js" \
    && fail "metrics page makes an external request or touches cookies"
grep -Fq "navigator.sendBeacon('/api/hit'" "$REPO/app.js" \
    || fail "click handler does not beacon to the same-origin /api/hit"
grep -Fq "document.addEventListener('click', recordHit)" "$REPO/app.js" \
    || fail "link clicks are not delegated to one beacon handler"
grep -Fq "document.addEventListener('auxclick', recordHit)" "$REPO/app.js" \
    || fail "middle-clicks are not delegated to one beacon handler"
awk '/# ---------- click history ----------/{f=1; next} /# ---------- http ----------/{f=0} f' "$APP" \
    | grep -Eq 'https?://|urlopen|urllib' \
    && fail "click-history code makes an outbound request"
# The shared hit handler and the strip selection must stay same-origin too: no
# hard-coded external URL, no fetch/XHR, no cookie access.
awk '
    /^function (recordHit|selectMostUsed|renderMostUsed)\(/ {f=1}
    f {print}
    f && /^}/ {f=0}
' "$REPO/app.js" > "$TMP/strip-functions.js"
for fn in recordHit selectMostUsed renderMostUsed; do
    grep -q "^function $fn(" "$TMP/strip-functions.js" \
        || fail "static check could not find function $fn in app.js"
done
grep -Eq 'https?://|fetch\(|XMLHttpRequest|document\.cookie' "$TMP/strip-functions.js" \
    && fail "card/strip code makes an external request or touches cookies"
ok "feature sets no cookie and makes no third-party request"

# 12. Repo/deploy coherence: compose defaults describe the live /opt/stack
# layout but can be pointed at the repo, and the durable host path is documented.
grep -Fq '${DASH_DIR:-./dash}:/app' "$COMPOSE" \
    || fail "compose does not make the dash directory overridable"
grep -Fq '${CADDYFILE:-./caddy/Caddyfile}' "$COMPOSE" \
    || fail "compose does not make the Caddyfile overridable"
grep -Fq '${DASH_ENV_FILE:-./dash/.env}' "$COMPOSE" \
    || fail "compose does not make the dash env file overridable"
grep -Fq 'DATA = Path(os.environ.get("DASH_DATA_DIR") or BASE / "data")' "$APP" \
    || fail "server data directory is not DASH_DATA_DIR-or-BASE/data"
grep -Fq 'HITS_FILE = DATA / "hits.json"' "$APP" \
    || fail "hit history is not stored as DATA/hits.json"
grep -Fq 'METRICS_FILE = DATA / "metrics.db"' "$APP" \
    || fail "metrics history is not stored as DATA/metrics.db"
grep -Fq '/opt/stack/dash/data/hits.json' "$README" \
    || fail "README does not document the durable hits.json host path"
grep -Fq 'server/check_api.sh' "$README" \
    || fail "README does not document this check"
grep -Fq '## Metrics history' "$README" \
    || fail "README does not document metrics history"
grep -Fq '/api/metrics' "$README" \
    || fail "README does not document the metrics endpoints"
grep -Fq '/opt/stack/dash/data/metrics.db' "$README" \
    || fail "README does not document the durable metrics.db path"
ok "compose paths are overridable, defaults match /opt/stack, and README documents both"

# 13. README documents how an owner pins a Launchpad tool.
grep -Fq '"pinned": true' "$README" \
    || fail "README does not document the pinned bookmark flag"
ok "README documents the pinned: true bookmark flag"

# 14. README documents the Workbench UI, its breakpoints and the checks.
grep -Fq '## Workbench UI' "$README" \
    || fail "README does not document the Workbench UI"
grep -Fq '1280×800' "$README" \
    || fail "README does not document the one-screen viewport checks"
grep -Fq '1180px' "$README" \
    || fail "README does not document the responsive breakpoints"
grep -Fq 'python3 -m unittest discover -s server' "$README" \
    || fail "README does not document the stdlib test command"
ok "README documents the Workbench UI, breakpoints and checks"

# 15. Rollup/peak documentation and the two long range tabs are wired.
for needle in 'rollup_5m' 'DASH_ROLLUP_5M_DAYS' 'DASH_ROLLUP_1H_DAYS' \
        'SUMMARY_CACHE_SECONDS' 'min/max' 'metric_samples' 'SIGNAL_WINDOW_S' \
        'load_1' 'swap_used' 'net_rx_bps' '/api/annotations' \
        'DASH_WARN_DISK_PCT' 'DASH_WARN_DAYS_TO_90'; do
    grep -Fq "$needle" "$README" \
        || fail "README does not document $needle"
done
grep -Fq 'data-range="90d"' "$REPO/metrics.html" \
    || fail "metrics page has no 90d range tab"
grep -Fq 'data-range="1y"' "$REPO/metrics.html" \
    || fail "metrics page has no 1y range tab"
grep -Fq 'min–avg–max' "$REPO/metrics.html" \
    || fail "table view does not advertise min-avg-max"
grep -Fq 'chart-band' "$REPO/metrics.js" \
    || fail "chart has no min/max peak band"
grep -Fq 'id="signal-strip"' "$REPO/metrics.html" \
    || fail "metrics page has no signal strip"
grep -Fq 'fmtRate' "$REPO/metrics.js" \
    || fail "signal strip has no rate formatting"
grep -Fq 'id="warning-banner"' "$REPO/metrics.html" \
    || fail "metrics page has no warning banner"
grep -Fq 'chart-marker' "$REPO/metrics.js" \
    || fail "chart has no annotation markers"
grep -Fq 'device-warn' "$REPO/app.js" \
    || fail "dashboard rail has no warning indicator"
ok "README documents rollups/signals/annotations and the page exposes their UI"

echo "all $COUNT checks passed"
