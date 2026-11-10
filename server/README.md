# Dashboard server and device agents

The front end in the repo root is static. This directory is the runtime that serves it:
a stdlib Python API, a Caddy entry point, and one-shot agents that push device metrics.

## Layout

| Path | What |
|---|---|
| `server/app.py` | the whole API, stdlib only, no pip, no build step |
| `server/docker-compose.yml` | `caddy` (only published port) + `dash` |
| `server/Caddyfile` | `:8448` → `dash:8000`, plain HTTP behind Tailscale |
| `server/check_api.sh` | dependency-free black-box acceptance check |
| `server/.env.example` | template for `dash/.env` (never commit `.env`) |
| `agents/dash-agent.py` | Linux/macOS agent, posts every 60 s |
| `agents/dash-agent.ps1` | Windows agent, PowerShell 5+ |
| `agents/com.jem.dashagent.plist` | macOS LaunchAgent, token from `~/dash/.token` |
| `agents/install-windows-agent.ps1` | Windows installer, hidden via `wscript` |
| `agents/pve-host-agent.py` | Proxmox host agent (host metrics + guest list) |
| `agents/install-pve-host-agent.sh` | installs the above as a systemd timer |

## Endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/health` | GET | `{ok, pve}` |
| `/api/bookmarks` | GET | the bookmark store, with a decayed `score` per item |
| `/api/stats` | GET | devices array, cached 5 s |
| `/api/hit` | POST | record a bookmark click (`{"url": "..."}` → `204`, no body) |
| `/api/push/<name>` | POST | agent heartbeat, needs `X-Dash-Token` |

Reads are open on the tailnet. Only pushes are authenticated — a leaked token can fake
a metric, nothing more. `/api/hit` accepts any URL, including one no longer in the store.

## Running it

### Live layout (`/opt/stack`)

The vendored compose file and Caddyfile mirror the live host layout. On the host the
compose file sits at `/opt/stack/docker-compose.yml`, next to `dash/` and `caddy/`:

| Compose path | Live host path |
|---|---|
| `./caddy/Caddyfile` | `/opt/stack/caddy/Caddyfile` |
| `./dash/.env` | `/opt/stack/dash/.env` |
| `./dash` (mounted at `/app`) | `/opt/stack/dash` |

```bash
cd /opt/stack
cp dash/.env.example dash/.env   # if absent; fill DASH_TOKEN
docker compose up -d
```

### From this repo

The compose defaults above describe the live layout, so running it from `server/` needs
three overrides. `server/` plays the role of `dash/`, and the server serves `BASE/static`,
so copy the static front end there first:

```bash
cd server
cp .env.example .env            # fill DASH_TOKEN, or leave it empty for an open local push endpoint
mkdir -p static
cp ../index.html ../app.js ../style.css static/
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
`/app/data/hits.json`. The service bind-mounts the whole dashboard directory:

```yaml
volumes:
  - ${DASH_DIR:-./dash}:/app
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
- **The container cannot reach the Proxmox API** when the host firewall runs
  `policy_in: DROP`. That is why `agents/pve-host-agent.py` exists: the host pushes,
  which needs no firewall hole.
- **Agents go offline after 150 s** without a push, so they run every 60 s.
