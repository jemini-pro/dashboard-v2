#!/usr/bin/env bash
# Black-box acceptance check for click history and decayed bookmark scores.
# Spec #16, ticket #37.
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
APP_JS="$REPO/app.js"
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

# Real embedded bookmarks (app.js) used as representative fixture URLs.
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
    DASH_DATA_DIR="$DATA_DIR" DASH_PORT="$PORT" DASH_TOKEN= \
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

hits_len() { # hits_len <url>
    python3 - "$HITS" "$1" <<'PY'
import json, sys
with open(sys.argv[1]) as f:
    hits = json.load(f)
print(len(hits.get(sys.argv[2], [])))
PY
}

write_bookmarks() {
    # Derive the fixture from the real embedded bookmark copy in app.js (24 work
    # + 7 personal) so payload-size and ordering checks cover realistic titles,
    # descriptions, icons and URLs rather than short "fixture" strings.
    python3 - "$BOOKMARKS" "$APP_JS" <<'PY'
import json, re, sys
out, app_js = sys.argv[1], sys.argv[2]
with open(app_js, encoding="utf-8") as f:
    source = f.read()
start = source.index("const bookmarks = ") + len("const bookmarks = ")
end = source.index("\n};", start) + 2
literal = source[start:end]
# app.js embeds a JS object literal: bare keys and single-quoted strings.
literal = re.sub(r"([{,]\s*)(\w+)(\s*:)", r'\1"\2"\3', literal)
literal = literal.replace("'", '"')
store = json.loads(literal)
items = [item for cats in store.values() for cat in cats for item in cat["items"]]
if len(items) < 31:
    raise SystemExit(f"expected at least 31 embedded bookmarks in app.js, parsed {len(items)}")
with open(out, "w", encoding="utf-8") as f:
    json.dump(store, f)
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

# 1. POST /api/hit -> 204, no body, timestamp stored.
code="$(post_hit "$W_MAIL")"
[[ "$code" == "204" ]] || fail "POST /api/hit returned $code, expected 204"
[[ ! -s "$TMP/hit.body" ]] || fail "POST /api/hit returned a body"
[[ -f "$HITS" ]] || fail "hits.json was not created under DASH_DATA_DIR"
[[ "$(hits_len "$W_MAIL")" == "1" ]] \
    || fail "one hit was not stored for $W_MAIL"
ok "POST /api/hit returns 204 with no body and writes hits.json"

# 2. Score appears in /api/bookmarks and rises with repeated clicks.
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

# 3. Decay ordering and the documented half life.
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

# 4. 90-day trimming happens on write.
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

# 5. Unknown URL is accepted (a link may since have been deleted).
code="$(post_hit https://deleted.example.com)"
[[ "$code" == "204" ]] || fail "unknown URL returned $code, expected 204"
[[ "$(hits_len https://deleted.example.com)" == "1" ]] \
    || fail "unknown URL hit was not stored"
ok "unknown URL is accepted with 204 and stored"

# 6. Renaming a title keeps the score (history is keyed by URL).
score_before="$(json_item_field "$TMP/bookmarks.json" "$W_MAIL" score)"
rename_mail
get_bookmarks
title_after="$(json_item_field "$TMP/bookmarks.json" "$W_MAIL" title)"
score_after="$(json_item_field "$TMP/bookmarks.json" "$W_MAIL" score)"
[[ "$title_after" == "Gmail (renamed)" ]] || fail "rename not visible: $title_after"
assert_close "$score_after" "$score_before" 0.001 \
    || fail "rename changed the score: $score_before -> $score_after"
ok "renaming a title keeps its score ($score_before -> $score_after)"

# 7. Payload size for the representative 31 links from app.js stays under ~10 KB.
items="$(json_item_count "$TMP/bookmarks.json")"
[[ "$items" == "31" ]] \
    || fail "expected 31 representative links derived from app.js, found $items"
title="$(json_item_field "$TMP/bookmarks.json" "$W_DRIVE" title)"
[[ "$title" == "Shared Drive" ]] \
    || fail "fixture is not derived from app.js (expected 'Shared Drive', got '$title')"
size="$(wc -c < "$TMP/bookmarks.json")"
[[ "$size" -lt 10240 ]] || fail "/api/bookmarks is $size bytes, over the ~10 KB budget"
ok "/api/bookmarks is $size bytes for $items links (under 10240)"

# 8. Process restart: the same DASH_DATA_DIR keeps the history. This is the
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

# 9. Static guarantees: no cookie is set and the feature makes no third-party
# request. Deliberately scoped to the click-history/strip code so pre-existing
# Google Fonts / calendar references do not produce false positives.
grep -q "Set-Cookie" "$APP" && fail "server sets a cookie"
grep -q "document.cookie" "$REPO/app.js" && fail "front end touches document.cookie"
grep -Fq "navigator.sendBeacon('/api/hit'" "$REPO/app.js" \
    || fail "click handler does not beacon to the same-origin /api/hit"
grep -Fq "mostUsedEl.addEventListener('click', recordBookmarkHit)" "$REPO/app.js" \
    || fail "Most used strip is missing its click listener"
grep -Fq "mostUsedEl.addEventListener('auxclick', recordBookmarkHit)" "$REPO/app.js" \
    || fail "Most used strip is missing its auxclick listener"
awk '/# ---------- click history ----------/{f=1; next} /# ---------- http ----------/{f=0} f' "$APP" \
    | grep -Eq 'https?://|urlopen|urllib' \
    && fail "click-history code makes an outbound request"
# The shared card/section renderers and the strip selection must stay same-origin
# too: no hard-coded external URL, no fetch/XHR, no cookie access.
awk '
    /^function (recordBookmarkHit|bookmarkCard|categorySection|selectMostUsed|renderMostUsed)\(/ {f=1}
    f {print}
    f && /^}/ {f=0}
' "$REPO/app.js" > "$TMP/strip-functions.js"
for fn in recordBookmarkHit bookmarkCard categorySection selectMostUsed renderMostUsed; do
    grep -q "^function $fn(" "$TMP/strip-functions.js" \
        || fail "static check could not find function $fn in app.js"
done
grep -Eq 'https?://|fetch\(|XMLHttpRequest|document\.cookie' "$TMP/strip-functions.js" \
    && fail "card/strip code makes an external request or touches cookies"
ok "feature sets no cookie and makes no third-party request"

# 10. Repo/deploy coherence: compose defaults describe the live /opt/stack
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
grep -Fq '/opt/stack/dash/data/hits.json' "$README" \
    || fail "README does not document the durable hits.json host path"
grep -Fq 'server/check_api.sh' "$README" \
    || fail "README does not document this check"
ok "compose paths are overridable, defaults match /opt/stack, and README documents both"

echo "all $COUNT checks passed"
