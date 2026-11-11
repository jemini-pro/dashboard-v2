# Dashboard server and device agents

The front end in the repo root is static. This directory is the runtime that serves it:
a stdlib Python API, a Caddy entry point, and one-shot agents that push device metrics.

The UI is the **Workbench** layout: a dense one-screen command centre made of a toolbar,
a main column (Jump back in, Launchpad, All tools) and a Network rail. There is no
`prototype/` directory and no `?variant=` switcher — the Workbench is the only layout.

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
| `agents/dash-agent.py` | Linux/macOS agent, posts every 60 s with all local volumes |
| `agents/dash-agent.ps1` | Windows agent, PowerShell 5+, all fixed/removable drives |
| `agents/com.jem.dashagent.plist` | macOS LaunchAgent, token from `~/dash/.token` |
| `agents/install-windows-agent.ps1` | Windows installer, hidden via `wscript` |
| `agents/pve-host-agent.py` | Proxmox host agent (host metrics + guest list) |
| `agents/install-pve-host-agent.sh` | installs the above as a systemd timer |

## Endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/health` | GET | `{ok}` |
| `/api/bookmarks` | GET | the bookmark store, with a decayed `score` per item |
| `/api/stats` | GET | devices array, cached 5 s; each device carries 24 h availability |
| `/api/metrics/summary` | GET | per-device availability, averages, events, disk trend and a CPU sparkline |
| `/api/metrics` | GET | one device's bucketed history: `?device=<id>&window=1h\|6h\|24h\|7d\|30d&step=<s>` |
| `/api/hit` | POST | record a bookmark click (`{"url": "..."}` → `204`, no body) |
| `/api/push/<name>` | POST | agent heartbeat, needs `X-Dash-Token`; optionally carries `disks` |

Reads are behind the Caddy basic-auth password on the tailnet. `/api/push/*` is the only
path exempt from that password and uses `X-Dash-Token` instead — a leaked push token can
fake a metric, nothing more. `/api/hit` accepts any non-empty URL string, including one
no longer in the store; missing, non-string and empty/whitespace values get a 4xx.

The front end is served with `Cache-Control: no-cache` and a weak ETag; static assets
revalidate with 304s, `/api/stats` is `no-store`, and the versioned font is immutable.

## Metrics history

The dashboard stores a little history so the Network rail and the Metrics page
can answer "is this normal, and since when?" rather than only "what is it now?".

- **Server-side sampler.** `app.py` starts a daemon thread that calls
  `sample_all()` immediately and then every `DASH_SAMPLE_SECONDS` (default
  **60 s**). It samples the local container and every agent in `agents.json`,
  whether or not a browser is open. Browser polling has no bearing on the
  store, so a closed dashboard leaves no gap.
- **SQLite, not another rewritten JSON file.** History lives in
  `data/metrics.db` (stdlib `sqlite3`, WAL). `bookmarks.json` and `hits.json`
  are rewritten whole on each write, which is fine for rare clicks but not for
  a sample every minute. One row per device per bucket; a repeat inside the
  same bucket updates the row instead of adding one.
- **What is stored.** `ts, cpu_pct, mem_pct, mem_used, mem_total, disk_pct,
  uptime_s, online`. Device metadata (`id`, display name, kind, first/last
  seen) is kept in a second table so renaming an agent does not split its
  timeline. Values that are not finite numbers are stored as `NULL`.
- **Retention.** Raw samples are kept for `DASH_HISTORY_DAYS` (default **30**)
  and pruned at most once an hour. At seven devices that is roughly 10k rows a
  day, or 20–30 MB for the full window.
- **Derived, not stored.** Availability, downtime, offline events, restarts
  (an `uptime_s` drop), CPU/RAM averages and peaks, disk growth (least-squares
  slope) and days-to-90% are computed when a request asks for them. Offline
  runs are detected from transitions, so no separate event table is needed.
- **Agent gaps are explicit.** An agent that stops pushing still has a row in
  `agents.json`; the sampler records it with `online = 0`. Charts break the line
  for those points instead of drawing a flat line. A gap in the *server* itself
  is simply absent data and does not count as device downtime.

The Metrics page (`/metrics.html`) uses the same data:

- range tabs for **1h / 6h / 24h / 7d / 30d**;
- a three-line chart (CPU, RAM, disk, fixed 0–100% axis) with a legend, a
  crosshair tooltip, surface-ringed end dots and a table view for exact values;
- a fleet summary (online count, average availability, peak CPU, disk
  pressure), a device list with 5-minute CPU sparklines, and an events rail
  (offline / back online / restarted, newest first).

The dashboard's Network rail links to it and shows a compact `24h <n>%` chip
on each device row once the first samples exist. The page and its CSS/JS are served from
`static/` like the other front-end files; `server/deploy.sh` installs all three.

Tuning (optional; add to `dash/.env` and recreate the container):

```bash
DASH_SAMPLE_SECONDS=60   # sampler cadence, minimum 5
DASH_HISTORY_DAYS=30     # raw-sample retention
```

## Workbench UI

The page never scrolls; the body grid, All tools list and Network rail own their own
overflow. At the target desktop sizes every core section fits in one screen.

| Region | Contents |
|---|---|
| Toolbar | date, live clock, `Search all tools…` field, health dot + `n/m online` |
| Jump back in | most-used rows from the scored API payload, with a normalised bar |
| Launchpad | pinned tool tiles (monogram + two-line label) |
| All tools | every category in store order, numbered rows, live search count |
| Network | `n/m online · x% avg CPU`, kind-average chips, compact device rows with 24 h availability, and a **Metrics →** link |

A device that reports more than one volume gets a compact breakdown under its row
(label, size, bar, percentage). The `DSK` cell stays a single gauge and shows the
**fullest** volume, so a nearly full data or external drive cannot hide behind a roomy
system disk. Single-volume devices render no extra line, keeping the rail at its old
height.

- Search filters title, description and category case-insensitively, shows `x of N tools`
  and names the query when nothing matches. `/` focuses the field; `Esc` clears and blurs.
- Health is amber when any device is offline and green only when all are. Device status is
  carried by a dot **and** text, never colour alone.
- Icons are monograms only: no emoji and no runtime third-party icon requests.
- A failed stats poll keeps the last good reading and shows the `stale` label.
- The work/personal theme (09:00–17:00) and the debug time control still apply.

Responsive rules:

- `≥1181px`: two-column body; the rail is `clamp(320px, 25vw, 390px)` wide.
- `901–1180px`: one-column body; Network sits below the main column in one rail column.
- `≤900px`: Launchpad stacks under Jump back in; All tools uses two columns.
- `≤600px`: All tools uses one column; the Launchpad auto-fits four tiles per row.
- `≤850px` height: tighter shell/card padding, a shorter usage list and tighter device rows
  so **1280×800** still fits without page scroll.

### Disk reporting

`/api/push/<name>` accepts an optional `disks` array alongside `disk_pct`:

```json
"disks": [
  {"mount": "C:\\", "label": "Windows", "total": 1000204886016, "used": 987000000000, "pct": 98.7}
]
```

`dash-agent.ps1` reports every fixed and removable Windows volume, not just `C:`.
`dash-agent.py` reports every real local Linux/macOS filesystem, root first, skipping
pseudo filesystems, snap loops and duplicate mounts. The server drops malformed entries,
caps the list at 16 and recomputes `disk_pct` as the fullest volume. Agents that have not
been updated keep working: they send only `disk_pct`, so they get no breakdown and their
single gauge stays as it was.

## Jump back in (click scores)

Click history is keyed by URL in `data/hits.json`. The server scores **every URL in the
store**: `score = sum(exp(-age_days / 14))`, rounded to 2 dp, with hits older than 90 days
trimmed. The server injects the scored store into `index.html`, so the strip renders on
the first paint with no second request; `/api/bookmarks` remains for compatibility and
returns the same scored payload. This supersedes the old embedded first-paint copy, which
has been removed: a static-only host with no injection falls back to fetching
`/api/bookmarks`.

The browser card is narrower than the scoring: `selectMostUsed()` only looks at the
rendered `work` list, keeps items scoring above `0.5`, and shows at most six in descending
order. A click on a personal link is still recorded and scored, but can never appear in the
card. The card stays visible with a quiet `No usage yet` row when nothing qualifies.

One fresh click scores `1.0` (a single `exp(0)` term), so a single click already clears the
`0.5` threshold; the spec's "~3 clicks" wording describes typical use, not a gating rule.
The threshold and the 14-day half-life are the actual rule.

## Launchpad pinned tools

The Launchpad is data-driven: an item in `bookmarks.json` opts in with a `"pinned": true`
field. `/api/bookmarks` already passes item fields through unchanged alongside the stamped
`score`, so no server change is needed.

On the live host, edit `/opt/stack/dash/data/bookmarks.json` (read per request, so reload
the page; no restart) and add the flag to an item:

```json
{
  "title": "Gmail",
  "desc": "Email",
  "url": "https://mail.google.com",
  "pinned": true
}
```

Tiles keep store order and are capped at eight. When no item is pinned, the browser falls
back to these titles when they are present in the store; missing ones are skipped:

> Gmail, Google Calendar, Google Sheets, GitHub, Slack, T3 Code, Proxmox, AWS

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
installs the server, static files, fonts and the served copies of the agents, runs
`docker compose up -d`, and smoke-tests both the rendered page and the unauthenticated
401. It prints the exact rollback command. No CI.

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
cp ../index.html ../app.js ../style.css ../metrics.html ../metrics.js ../metrics.css static/
cp -r ../fonts static/
DASH_DIR=. DASH_ENV_FILE=./.env CADDYFILE=./Caddyfile docker compose up -d
```

Docker is not needed for API work. Run the server directly instead:

```bash
cd /path/to/dashboard-v2
env -u DASH_TOKEN python3 server/app.py
```

## Workbench checks

Run the stdlib suite (API plus source-level front-end assertions) and the black-box API
check:

```bash
python3 -m unittest discover -s server -p 'test_*.py' -v
server/check_api.sh
```

For the layout, copy the six front-end files into `server/static/` and open the app:

```bash
mkdir -p server/static
cp index.html app.js style.css server/static/
env -u DASH_TOKEN DASH_PORT=8080 DASH_DATA_DIR=/tmp/dash-data python3 server/app.py
```

Then verify in a browser:

- `1440×900` and `1280×800`: `document.documentElement.scrollHeight <= window.innerHeight`
  — every core section and device row visible without page scroll.
- `390×844`: usable phone layout; the body grid scrolls internally, Launchpad shows four
  tiles per row and All tools is one column.
- Breakpoints: two body columns at `≥1181px`; one column with Network below main at
  `901–1180px`; stacked Launchpad and two list columns at `≤900px`; one list column at
  `≤600px`; tighter shell/card padding and device rows at `≤850px` height.
- Click a bookmark, a Launchpad tile and a Jump back in row; each sends `POST /api/hit`
  via `sendBeacon`, and the score rises after a reload.

Automated screenshots are captured when a headless browser is available; otherwise the
viewport items above are the remaining manual checks.

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
keeps the score; `/api/bookmarks` stays under ~10 KB for the current 31 links; the
sampler writes history with no browser open; `/api/metrics` returns bucketed points,
a step and a derived summary; unknown windows/devices are rejected; hits **and**
metrics history survive a full process restart; and the click feature sets no cookie
and makes no third-party request. It also guards the compose/README path documentation below and the
Workbench UI, breakpoint and check documentation above.

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

host:      /opt/stack/dash/data/metrics.db
container: /app/data/metrics.db
```

Because `data/` is on the host bind mount, `docker compose up -d dash` (recreate),
`docker compose restart dash`, and even `docker compose down` all preserve it — only
deleting `/opt/stack/dash/data/hits.json` or `/opt/stack/dash/data/metrics.db` (or the
directory) loses history. SQLite keeps its `-wal` and `-shm` files beside `metrics.db`;
they must stay on the same writable mount. The same mount persists `bookmarks.json`,
`agents.json` and `metrics.db`.

Docker is not available in this implementation environment, so durability was verified
with the equivalent process-restart phase in `server/check_api.sh`: it writes hits and
metric samples to a temp `DASH_DATA_DIR`, kills and restarts `python3 app.py` against
that same directory, and asserts the score, hit count and earliest sample are unchanged.
Current evidence:

```
ok 03 - POST /api/hit returns 204 with no body and writes hits.json
ok 13 - hits survived a full server restart (score 3.0 -> 3.0)
ok 14 - metrics history survived a full server restart (first sample 1794320920)
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
- **Metrics history is sampled by the server, not by the browser.** If you add a new
  data source, feed it through `sample_all()`/`record_samples()`; do not record from
  `/api/stats`, or history only exists while a tab is open.
- **`metrics.db` is a single writer.** `record_samples` holds `_db_lock`; reads use
  their own connections under WAL. Keep new writes on the same path rather than opening
  a connection per request and writing concurrently.
