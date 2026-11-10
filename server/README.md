# Dashboard server and device agents

The front end in the repo root is static. This directory is the runtime that serves it:
a stdlib Python API, a Caddy entry point, and one-shot agents that push device metrics.

## Layout

| Path | What |
|---|---|
| `server/app.py` | the whole API, stdlib only, no pip, no build step |
| `server/test_app.py` | stdlib `unittest` coverage for the API and front-end wiring |
| `server/docker-compose.yml` | `caddy` (only published port) + `dash`, hardened and capped |
| `server/Caddyfile` | `:8448` → `dash:8000`, basic auth, gzip-9/zstd, plain HTTP behind Tailscale |
| `server/deploy.sh` | backup, install, smoke-test and roll back `/opt/stack` |
| `server/bookmarks.seed.json` | fresh-install bookmark seed (the live store is never overwritten) |
| `server/check_api.sh` | dependency-free black-box acceptance check |
| `server/.env.example` | template for `dash/.env` (never commit `.env`) |
| `fonts/` | self-hosted Space Grotesk subset plus its OFL licence |
| `agents/dash-agent.py` | Linux/macOS agent, posts every 60 s |
| `agents/dash-agent.ps1` | Windows agent, PowerShell 5+ |
| `agents/com.jem.dashagent.plist` | macOS LaunchAgent, token from `~/dash/.token` |
| `agents/install-windows-agent.ps1` | Windows installer, hidden via `wscript` |
| `agents/pve-host-agent.py` | Proxmox host agent (host metrics + guest list) |
| `agents/install-pve-host-agent.sh` | installs the above as a systemd timer |

## Endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/health` | GET | `{ok}` |
| `/api/bookmarks` | GET | the bookmark store, with a decayed `score` per item |
| `/api/stats` | GET | devices array, cached 5 s |
| `/api/hit` | POST | record a bookmark click (`{"url": "..."}` → `204`, no body) |
| `/api/push/<name>` | POST | agent heartbeat, needs `X-Dash-Token` |

Reads are behind the Caddy basic-auth password on the tailnet. `/api/push/*` is the only
path exempt from that password and uses `X-Dash-Token` instead — a leaked push token can
fake a metric, nothing more. `/api/hit` accepts any non-empty URL string, including one
no longer in the store; missing, non-string and empty/whitespace values get a 4xx.

The front end is served with `Cache-Control: no-cache` and a weak ETag; static assets
revalidate with 304s, `/api/stats` is `no-store`, and the versioned font is immutable.

## Most used strip

Click history is keyed by URL in `data/hits.json`. The server scores **every URL in the
store**: `score = sum(exp(-age_days / 14))`, rounded to 2 dp, with hits older than 90 days
trimmed. The server injects the scored store into `index.html`, so the strip renders on
the first paint with no second request; `/api/bookmarks` remains for compatibility and
returns the same scored payload.

The browser strip is narrower than the scoring: `selectMostUsed()` only looks at the
rendered `work` list, keeps items scoring above `0.5`, and shows at most six. A click on
a personal link is still recorded and scored, but can never appear in the strip.

One fresh click scores `1.0` (a single `exp(0)` term), so a single click already clears the
`0.5` threshold; the spec's "~3 clicks" wording describes typical use, not a gating rule.
The threshold and the 14-day half-life are the actual rule.

## Running it

### Live layout (`/opt/stack`)

The vendored compose file and Caddyfile mirror the live host layout. On the host the
compose file sits at `/opt/stack/docker-compose.yml`, next to `dash/` and `caddy/`:

| Compose path | Live host path |
|---|---|
| `./caddy/Caddyfile` | `/opt/stack/caddy/Caddyfile` |
| `./dash/.env` | `/opt/stack/dash/.env` |
| `./dash` (mounted at `/app`) | `/opt/stack/dash` |
| `./dash/data` (mounted at `/app/data`) | `/opt/stack/dash/data` |

`/opt/stack/dash/.env` needs `DASH_TOKEN`, `DASH_MEM_TOTAL` and `DASH_AUTH_HASH`. Generate
the hash with `docker run --rm caddy:2-alpine caddy hash-password --plaintext '...'`.
The deploy sets `DASH_BIND=10.10.10.20`, so the LXC's LAN address is the only listener and
`tailscale serve` remains the only user entry.

```bash
cd /opt/stack
cp dash/.env.example dash/.env   # if absent; fill DASH_TOKEN and DASH_AUTH_HASH
docker compose up -d
```

### Deploying

`server/deploy.sh [ref]` (run as root on `docker01`) fetches the ref, preflights for
third-party references, backs up `/opt/stack`, preserves or creates `DASH_AUTH_HASH`,
installs the server, static files and fonts, runs `docker compose up -d`, and smoke-tests
both the rendered page and the unauthenticated 401. It prints the exact rollback command.
No CI.

`/opt/stack/dash/data/bookmarks.json` is the single source of truth for bookmarks, and is
read per request: edit it, reload the page, no restart. `/api/bookmarks` returns that
store with the decayed `score` stamped on every item.

### From this repo

The compose defaults above describe the live layout, so running it from `server/` needs
three overrides. `server/` plays the role of `dash/`, and the server serves `BASE/static`,
so copy the static front end there first:

```bash
cd server
cp .env.example .env            # fill DASH_TOKEN and DASH_AUTH_HASH, or skip Docker and run app.py directly
mkdir -p static
cp ../index.html ../app.js ../style.css static/
cp -r ../fonts static/
DASH_DIR=. DASH_ENV_FILE=./.env CADDYFILE=./Caddyfile docker compose up -d
```

Docker is not needed for API work. Run the server directly instead:

```bash
cd /path/to/dashboard-v2
env -u DASH_TOKEN python3 server/app.py
```

## Manual check

With `DASH_TOKEN` unset, the API listens on `:8000` and `/api/hit` is open. Point
`DASH_PORT` elsewhere if 8000 is taken. A fresh checkout has no `server/static`, so `/`
is 404; the API endpoints work regardless.

```bash
env -u DASH_TOKEN python3 server/app.py &

curl -i -X POST -H 'Content-Type: application/json' \
    -d '{"url":"https://mail.google.com"}' http://localhost:8000/api/hit
# HTTP/1.1 204 No Content

curl -s http://localhost:8000/api/bookmarks \
    | python3 -m json.tool | grep -A2 'mail.google.com'
# "url": "https://mail.google.com",
# "score": 1.0
```

Click the link again and reload: the score rises. By default history lives in
`server/data/hits.json`; set `DASH_DATA_DIR=/some/dir` to keep it elsewhere.

## Black-box check

`server/check_api.sh` needs only bash, curl, coreutils and python3 (stdlib). It picks a
free port, starts the real server against a temp `DASH_DATA_DIR`, exercises the API with
curl, then removes the temp directory and process. Docker and node are not required.

```bash
server/check_api.sh
```

It asserts: `POST /api/hit` → `204` with no body; the score appears in
`/api/bookmarks` and rises; older hits decay (`7d > 28d > 60d`, 14-day hit ≈ `0.37`);
hits older than 90 days are trimmed on write; an unknown URL is accepted; a title rename
keeps the score; `/api/bookmarks` stays under ~10 KB for the current 31 links; hits
survive a full process restart; and the click feature sets no cookie and makes no
third-party request. It also guards the compose/README path documentation below.

## Click-history durability

`app.py` resolves its storage directory once:

```python
DATA = Path(os.environ.get("DASH_DATA_DIR") or BASE / "data")
HITS_FILE = DATA / "hits.json"
```

Inside the deployed `dash` container `BASE` is `/app` (the service sets
`working_dir: /app` and runs `python -u app.py`), so history is written to
`/app/data/hits.json`. The service bind-mounts the code directory and a separate writable
data directory:

```yaml
volumes:
  - ${DASH_DIR:-./dash}:/app
  - ${DASH_DATA_DIR:-./dash/data}:/app/data
```

In the live stack that resolves to `/opt/stack/dash:/app`. The exact host path is
therefore:

```
host:      /opt/stack/dash/data/hits.json
container: /app/data/hits.json
```

Because `data/` is on the host bind mount, `docker compose up -d dash` (recreate),
`docker compose restart dash`, and even `docker compose down` all preserve it — only
deleting `/opt/stack/dash/data/hits.json` (or the directory) loses history. The same
mount already persists `bookmarks.json` and `agents.json`.

Docker is not available in this implementation environment, so durability was verified
with the equivalent process-restart phase in `server/check_api.sh`: it writes hits to a
temp `DASH_DATA_DIR`, kills and restarts `python3 app.py` against that same directory,
and asserts the score and hit count are unchanged. Current evidence:

```
ok 02 - POST /api/hit returns 204 with no body and writes hits.json
ok 09 - hits survived a full server restart (score 3.0 -> 3.0)
```

## Gotchas worth knowing before changing anything

- **`/proc` inside this container describes the host, not the container.** Keep `cgroup: host`
  on the `dash` service and read `/sys/fs/cgroup`. Subtract `inactive_file` from
  `memory.current`, or page cache makes an idle 2 GB box read 94% full.
- **`docker compose restart` does not re-read `env_file`.** Use `up -d` to recreate.
  Restart *is* right for a bind-mounted file like the Caddyfile.
- **Compose paths are relative to the compose file's directory.** The defaults are the
  live `/opt/stack` layout; the repo run above overrides `DASH_DIR`, `DASH_ENV_FILE` and
  `CADDYFILE`. Any other layout must set the same three variables.
- **The Caddyfile hard-codes `dash:8000`.** If you change `DASH_PORT`, update
  `reverse_proxy` in the Caddyfile too.
- **The server no longer pulls the Proxmox API.** `PVE_*` and the unverified-TLS pull are
  gone; `agents/pve-host-agent.py` pushes host and guest metrics, which needs no firewall
  hole.
- **Caching lives in `app.py`, not Caddy.** Static assets and the injected index use weak
  ETags plus 304s; `/api/stats` stays `no-store`. Caddy only compresses (`gzip 9`/zstd).
- **`server/deploy.sh` never overwrites `dash/data/`.** It seeds `bookmarks.json` only
  when absent and chowns the data directory to uid 1000.
- **Agents go offline after 150 s** without a push, so they run every 60 s.
