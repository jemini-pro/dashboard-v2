#!/usr/bin/env bash
# Deploy the dashboard from the repo to /opt/stack on docker01.
# Run as root on docker01. No CI; every step is idempotent and backed up.
#
#   server/deploy.sh [ref]        # default: main
#
# Secrets live in /opt/stack/dash/.env and are never read from the repo.
set -euo pipefail

REF="${1:-main}"
REPO_URL="${REPO_URL:-https://github.com/jemini-pro/dashboard-v2.git}"
STACK="${STACK:-/opt/stack}"
BUILD="${BUILD:-/opt/stack/build}"
BACKUP_DIR="${BACKUP_DIR:-/opt/stack/backups}"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="$BACKUP_DIR/stack-$TS.tar.gz"

say() { printf '\n== %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root on docker01"
[ -d "$STACK/dash" ] || die "missing $STACK/dash"
[ -f "$STACK/dash/.env" ] || die "missing $STACK/dash/.env"

say "fetch $REF"
if [ ! -d "$BUILD/.git" ]; then
    git clone --quiet "$REPO_URL" "$BUILD"
fi
git -C "$BUILD" fetch --quiet --prune origin
git -C "$BUILD" checkout --quiet -f "$REF"
if git -C "$BUILD" rev-parse --verify --quiet "origin/$REF" >/dev/null; then
    git -C "$BUILD" reset --quiet --hard "origin/$REF"
fi
COMMIT="$(git -C "$BUILD" rev-parse --short HEAD)"

say "preflight"
for f in server/app.py server/Caddyfile server/docker-compose.yml \
         index.html app.js style.css metrics.html metrics.js metrics.css \
         server/bookmarks.seed.json; do
    [ -f "$BUILD/$f" ] || die "missing $BUILD/$f"
done
[ -d "$BUILD/fonts" ] || die "missing $BUILD/fonts/"
if grep -RqsE 'calendar\.google\.com|fonts\.googleapis\.com|fonts\.gstatic\.com' \
        "$BUILD/index.html" "$BUILD/app.js" "$BUILD/style.css" \
        "$BUILD/metrics.html" "$BUILD/metrics.js" "$BUILD/metrics.css"; then
    die "third-party references remain in the front end"
fi

say "backup"
mkdir -p "$BACKUP_DIR"
tar -C "$STACK" -czf "$BACKUP" dash caddy docker-compose.yml
echo "backup: $BACKUP"

say "secrets"
ENVF="$STACK/dash/.env"
if ! grep -q '^DASH_AUTH_HASH=' "$ENVF"; then
    HASH="$(grep -oE '\$2[aby]\$[0-9]+\$[./A-Za-z0-9]+' "$STACK/caddy/Caddyfile" | head -1 || true)"
    [ -n "$HASH" ] || die "set DASH_AUTH_HASH in $ENVF with: docker run --rm caddy:2-alpine caddy hash-password --plaintext '...'"
    printf 'DASH_AUTH_HASH=%s\n' "$HASH" >> "$ENVF"
    echo "preserved the existing basic-auth hash in $ENVF"
fi
sed -i '/^PVE_/d' "$ENVF" || true

say "install"
install -m 0644 "$BUILD/server/app.py" "$STACK/dash/app.py"
install -m 0644 "$BUILD/index.html" "$STACK/dash/static/index.html"
install -m 0644 "$BUILD/app.js" "$STACK/dash/static/app.js"
install -m 0644 "$BUILD/style.css" "$STACK/dash/static/style.css"
install -m 0644 "$BUILD/metrics.html" "$STACK/dash/static/metrics.html"
install -m 0644 "$BUILD/metrics.js" "$STACK/dash/static/metrics.js"
install -m 0644 "$BUILD/metrics.css" "$STACK/dash/static/metrics.css"
rm -rf "$STACK/dash/static/fonts"
mkdir -p "$STACK/dash/static/fonts"
cp -a "$BUILD/fonts/." "$STACK/dash/static/fonts/"
install -m 0644 "$BUILD/server/Caddyfile" "$STACK/caddy/Caddyfile"
install -m 0644 "$BUILD/server/docker-compose.yml" "$STACK/docker-compose.yml"

# The metrics page is three extra files next to index.html. Fail here rather
# than at the smoke test if one of them never reached the static directory.
for f in metrics.html metrics.js metrics.css; do
    [ -s "$STACK/dash/static/$f" ] || die "install left $f missing from $STACK/dash/static"
done

mkdir -p "$STACK/dash/data"
if [ ! -f "$STACK/dash/data/bookmarks.json" ]; then
    cp "$BUILD/server/bookmarks.seed.json" "$STACK/dash/data/bookmarks.json"
    echo "seeded bookmarks.json"
fi
chown -R 1000:1000 "$STACK/dash/data"

# Third-party references belong to the front-end source, not the rendered page:
# the rendered page embeds the live bookmark store, and a bookmark may point
# anywhere, including calendar.google.com.
if grep -RqsE 'calendar\.google\.com|fonts\.googleapis\.com|fonts\.gstatic\.com' \
        "$STACK/dash/static/index.html" "$STACK/dash/static/app.js" "$STACK/dash/static/style.css" \
        "$STACK/dash/static/metrics.html" "$STACK/dash/static/metrics.js" \
        "$STACK/dash/static/metrics.css"; then
    die "third-party references remain in the installed front end"
fi

say "up"
# Every service change here is a bind-mounted file, and compose only recreates
# a container when its own config changes. Without --force-recreate the deploy
# happily installs a new app.py while the old process keeps serving the code it
# loaded at start-up, so a new API route 404s after a "successful" deploy.
( cd "$STACK" && DASH_BIND=10.10.10.20 docker compose up -d --remove-orphans --force-recreate )

say "smoke"
HEALTHY=0
for _ in $(seq 1 30); do
    if docker compose -f "$STACK/docker-compose.yml" exec -T dash python -c \
        "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/api/health').read().decode())" \
        >/dev/null 2>&1; then
        HEALTHY=1
        break
    fi
    sleep 1
done
if [ "$HEALTHY" -ne 1 ]; then
    die "dash did not become healthy; rollback: tar -xzf $BACKUP -C $STACK && (cd $STACK && docker compose up -d)"
fi
docker compose -f "$STACK/docker-compose.yml" exec -T dash python - <<'PY' || {
import json
import time
import urllib.request


def fetch(path):
    return urllib.request.urlopen("http://127.0.0.1:8000" + path).read().decode()


html = fetch("/")
assert "__BOOKMARKS_JSON__" not in html, "bookmark placeholder was not replaced"
assert 'id="bookmarks-data"' in html, "bookmark data script is missing"
metrics = fetch("/metrics.html")
assert "Metrics" in metrics and "metrics.js" in metrics, "metrics page is missing"

# The static page says nothing about the running process. Hit the sampler's
# API too, or a stale container that never loaded the new app.py still passes.
summary = None
for _ in range(30):
    try:
        summary = json.loads(fetch("/api/metrics/summary?window=1h"))
        if summary.get("devices"):
            break
    except Exception:
        pass
    time.sleep(0.5)
assert summary is not None and summary.get("devices"), \
    "metrics API is not serving samples (old app.py still running?)"
print("page and metrics api ok")
PY
    die "page/API render check failed; rollback: tar -xzf $BACKUP -C $STACK && (cd $STACK && docker compose up -d)"
}
CODE="$(curl -s -o /dev/null -w '%{http_code}' http://10.10.10.20:8448/ || true)"
if [ "$CODE" != "401" ]; then
    die "Caddy answered $CODE without credentials, expected 401; rollback: tar -xzf $BACKUP -C $STACK && (cd $STACK && docker compose up -d)"
fi

# A mangled hash also answers 401 to everything, so compare what .env says with
# what the container actually received. Compose interpolation silently truncates
# a bcrypt hash unless env_file uses format: raw.
say "auth"
WANT_HASH="$(sed -n 's/^DASH_AUTH_HASH=//p' "$STACK/dash/.env" | head -1)"
GOT_HASH="$(docker compose -f "$STACK/docker-compose.yml" exec -T caddy printenv DASH_AUTH_HASH | tr -d '\r\n' || true)"
if [ "$WANT_HASH" != "$GOT_HASH" ]; then
    die "caddy holds a ${#GOT_HASH}-byte hash, .env holds ${#WANT_HASH} bytes; env_file interpolation truncated it, so no password can match. Keep format: raw. rollback: tar -xzf $BACKUP -C $STACK && (cd $STACK && docker compose up -d)"
fi

say "done"
echo "commit $COMMIT deployed; page answers 401 without credentials as expected"
echo "rollback: tar -xzf $BACKUP -C $STACK && (cd $STACK && docker compose up -d)"
