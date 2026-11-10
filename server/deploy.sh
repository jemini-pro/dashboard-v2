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
         index.html app.js style.css server/bookmarks.seed.json; do
    [ -f "$BUILD/$f" ] || die "missing $BUILD/$f"
done
[ -d "$BUILD/fonts" ] || die "missing $BUILD/fonts/"
if grep -RqsE 'calendar\.google\.com|fonts\.googleapis\.com|fonts\.gstatic\.com' \
        "$BUILD/index.html" "$BUILD/app.js" "$BUILD/style.css"; then
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
rm -rf "$STACK/dash/static/fonts"
mkdir -p "$STACK/dash/static/fonts"
cp -a "$BUILD/fonts/." "$STACK/dash/static/fonts/"
install -m 0644 "$BUILD/server/Caddyfile" "$STACK/caddy/Caddyfile"
install -m 0644 "$BUILD/server/docker-compose.yml" "$STACK/docker-compose.yml"

mkdir -p "$STACK/dash/data"
if [ ! -f "$STACK/dash/data/bookmarks.json" ]; then
    cp "$BUILD/server/bookmarks.seed.json" "$STACK/dash/data/bookmarks.json"
    echo "seeded bookmarks.json"
fi
chown -R 1000:1000 "$STACK/dash/data"

say "up"
( cd "$STACK" && DASH_BIND=10.10.10.20 docker compose up -d --remove-orphans )

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
import urllib.request
html = urllib.request.urlopen("http://127.0.0.1:8000/").read().decode()
assert "__BOOKMARKS_JSON__" not in html, "bookmark placeholder was not replaced"
assert 'id="bookmarks-data"' in html, "bookmark data script is missing"
assert "calendar.google.com" not in html, "calendar reference survived"
print("page ok")
PY
    die "page render check failed; rollback: tar -xzf $BACKUP -C $STACK && (cd $STACK && docker compose up -d)"
}
CODE="$(curl -s -o /dev/null -w '%{http_code}' http://10.10.10.20:8448/ || true)"
if [ "$CODE" != "401" ]; then
    die "Caddy answered $CODE without credentials, expected 401; rollback: tar -xzf $BACKUP -C $STACK && (cd $STACK && docker compose up -d)"
fi

say "done"
echo "commit $COMMIT deployed; page answers 401 without credentials as expected"
echo "rollback: tar -xzf $BACKUP -C $STACK && (cd $STACK && docker compose up -d)"
