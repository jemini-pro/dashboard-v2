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
| `stack.js` | shared PVE → LXC → Docker containment model for both rails |
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
| `/api/stats` | GET | devices array, cached 5 s; each device carries 24 h availability and warning counts, plus the pve → LXC → Docker `topology` |
| `/api/metrics/summary` | GET | per-device availability, averages, events, disk trend, warnings, compact volume and latest-signal summaries, a CPU sparkline, plus the same `topology`; long windows read rollups |
| `/api/metrics` | GET | one device's bucketed history, min/max bands, per-volume series, recent signals and annotations: `?device=<id>&window=1h\|6h\|24h\|7d\|30d\|90d\|1y&step=<s>` |
| `/api/hit` | POST | record a bookmark click (`{"url": "..."}` → `204`, no body) |
| `/api/push/<name>` | POST | agent heartbeat, needs `X-Dash-Token`; optionally carries `disks` and extra signals |
| `/api/annotations` | POST | record a deploy/restart/backup/note marker, needs `X-Dash-Token` |

Reads are behind the Caddy basic-auth password on the tailnet. `/api/push/*` is exempt
from that password and uses `X-Dash-Token` instead — a leaked push token can fake a
metric, nothing more. `/agents/*` is exempt too: those are the installer and agent scripts
themselves, which carry no secrets (the token lives in a separate file on the device) and
are fetched by machines that cannot answer a password prompt. That is also how an
installed agent re-reads its own current copy; see [Agent updates](#agent-updates).
`/api/hit` accepts any non-empty URL string, including one no longer in the store;
missing, non-string and empty/whitespace values get a 4xx.

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
- **Per-volume history.** Every heartbeat's `disks` array (capped at 16;
  malformed entries dropped) is written to `disk_samples (device_id, mount,
  ts, label, total, used, pct)` with the same bucket timestamp and in the same
  transaction as its aggregate row. A repeat inside a bucket replaces that
  device's volume rows, so a mount that vanished does not linger and a second
  heartbeat never double-counts. The local `docker01` volume and each agent's
  `disks` array both feed it. LXC guests report no volume data, so they have
  no rows; old agents that send only `disk_pct` simply have none.
- **Extra signals use one generic table.** `metric_samples (device_id, metric,
  ts, value)` stores load (1/5/15), swap used/total, network rx/tx rates and
  CPU iowait/steal. The allowlist is `EXTRA_METRICS` in `app.py`, so a future
  signal needs no schema migration. A heartbeat with no signal fields writes
  nothing, an old agent keeps working, and the API omits what it never
  received — missing signals are never rendered as zero. Swap percentage is
  derived on read as `swap_used / swap_total`; a host with no swap reports
  total 0 and therefore no swap signal.
- **Annotations are SQLite rows, not derived.** `POST /api/annotations` stores
  `(device_id, ts, kind, text)`; `kind` is one of `deploy`, `restart`, `backup`
  or `note`, and an empty `device_id` means fleet-wide. It uses the same
  `X-Dash-Token` as `/api/push` (constant-time compare) and is capped at 4 KB
  and 200 characters. The metrics payload returns annotations in the requested
  window for the selected device plus fleet-wide ones, and adds detected
  restarts as `auto: true`, `kind: "restart"` markers without storing a row.
  Manual annotations also become `type: "annotation"` Events rows; restarts
  already have their own row.
- **Warnings are evaluated server-side.** `evaluate_warnings()` turns the
  summary fields into `{code, level, value, threshold, text}` entries for disk
  percent, days-to-90, disk growth, RAM and offline duration. Warning and
  critical levels are derived from the env thresholds below; `/api/metrics`
  and `/api/metrics/summary` carry the full list. `/api/stats` carries only the
  cheap live subset (`disk`, `mem`, `offline`) as `warning_count` /
  `warning_level`, so the dashboard rail never pays for a history scan.
- **Two rollup tiers.** `rollup_5m` and `rollup_1h` (plus the matching
  `disk_rollup_5m` / `disk_rollup_1h` for volumes) store `samples`,
  `online_samples`, per-series average/min/max and the uptime first/last/min
  for one bucket. The sampler recomputes only the buckets the current
  heartbeat touches, inside the same transaction: the 5-minute row from raw
  `samples`, the hourly row from the 5-minute rows, and the volume rows from
  online `disk_samples`. A rollup row is a pure function of its raw bucket, so
  re-running it is always safe.
- **Window routing.** `≤ 24 h` reads raw rows; `7d` and `30d` read
  `rollup_5m`; `90d` and `1y` read `rollup_1h`. `/api/metrics/summary` takes
  the same route, so it never scans a raw 30-day window per device per
  request. Live summary requests are additionally cached for
  `SUMMARY_CACHE_SECONDS` (30 s) and shared across open tabs; every sample
  write and every agent push invalidates it, so a new value is never hidden
  for the full TTL. An explicit `now` (tests/backfills) bypasses the cache.
- **Retention.** Raw samples, raw volume rows and signal rows are kept for
  `DASH_HISTORY_DAYS` (default **30**); 5-minute rollups for
  `DASH_ROLLUP_5M_DAYS` (default **30**); hourly rollups for
  `DASH_ROLLUP_1H_DAYS` (default **366**). All six tables are pruned at most
  once an hour, on the same pass.
- **Row-size math.** One device costs one `samples` row per bucket plus one
  `disk_samples` row per mount. Rollups are 288 rows/device/day at 5 minutes
  and 24 rows/device/day at 1 hour, regardless of mount count for the
  aggregate tiers. Seven single-volume devices at the defaults therefore hold
  roughly 10k raw rows/day (20–30 MB for 30 days), 60k 5-minute rows for
  30 days (≈7–9 MB), and 61k hourly rows for a year (≈9–12 MB), with volume
  rollups multiplying the rollup row count by the number of mounts.
- **Peaks survive bucketing.** `/api/metrics` keeps its five-tuple `points`
  (timestamp, cpu avg, mem avg, disk avg, online) and adds a parallel
  `bands = {cpu|mem|disk: [[ts, min, max], …]}` array, aligned one-to-one with
  the points. Rollup bands fold stored minima/maxima with `MIN`/`MAX`; a
  coarse `?step=` re-weights averages by sample count so it stays exact.
  `MAX_POINTS` still caps both arrays at 400.
- **Derived, not stored.** Availability, downtime, offline events, restarts
  (an `uptime_s` drop), CPU/RAM averages and peaks, disk growth (least-squares
  slope) and days-to-90% are computed when a request asks for them. For long
  windows the same signals come from rollup buckets; downtime is the offline
  share of each bucket and events are bucket-level transitions. The same
  least-squares rule and days-to-90 projection run per volume, online-only, so
  an offline agent's stale volume values never look like a trend. Offline runs
  are detected from transitions, so no separate event table is needed.
- **Agent gaps are explicit.** An agent that stops pushing still has a row in
  `agents.json`; the sampler records it with `online = 0`. Charts break the line
  for those points instead of drawing a flat line. A gap in the *server* itself
  is simply absent data and does not count as device downtime.

The Metrics page (`/metrics.html`) uses the same data:

- range tabs for **1h / 6h / 24h / 7d / 30d / 90d / 1y**;
- a landing device when the URL has no `?device=`: the worst active warning
  first, then a physical machine, and a container last. Without that the page
  opened on whichever container the summary listed first, whose single volume
  also hides the volume table;
- a three-line chart (CPU, RAM, disk, fixed 0–100% axis) with a legend, a
  crosshair tooltip, surface-ringed end dots, a faint min/max peak band behind
  each line, and a table view for exact values (each cell reads
  `min–avg–max%`);
- a volume table under the chart when the selected device reports more than
  one volume (label, mount, used %, size, growth/day, days-to-full), with the
  per-volume samples added to the opt-in table view — the 0–100 chart keeps a
  single aggregate disk line rather than becoming multi-volume;
- a compact signal strip (load 1/5/15, swap, net RX/TX, I/O wait, steal) with
  sparklines, fed by the extra-signal API. It never joins the 0–100 chart and
  a signal the device did not send is absent rather than a zero tile;
- a warning banner (device, text, warning/critical dot) above the chart, plus
  a compact `nw` chip on each device row in the page rail. The dashboard's
  Network rail shows the same live-only chip from `/api/stats`;
- dashed chart markers for every annotation in the window, with the note on
  the SVG `<title>`, and an Events row for each manual annotation;
- a fleet summary (device online count, average availability, peak CPU, disk
  pressure), a device list with 5-minute CPU sparklines, and an events rail
  (offline / back online / restarted, newest first). Docker containers are
  charted under their host but are not devices: the `n/m online` tallies on
  both pages skip `kind="container"`, so one stopped container cannot turn the
  fleet amber.

The dashboard's Network rail links to it and shows a compact `24h <n>%` chip
on each device row once the first samples exist. The page and its CSS/JS are served from
`static/` like the other front-end files; `server/deploy.sh` installs all three.

Tuning (optional; add to `dash/.env` and recreate the container):

```bash
DASH_SAMPLE_SECONDS=60    # sampler cadence, minimum 5
DASH_HISTORY_DAYS=30      # raw-sample and raw volume retention
DASH_ROLLUP_5M_DAYS=30    # 5-minute rollup retention (7d/30d windows)
DASH_ROLLUP_1H_DAYS=366   # hourly rollup retention (90d/1y windows)
DASH_WARN_DISK_PCT=85     # disk warning line; critical is +10, capped at 99
DASH_WARN_DAYS_TO_90=14   # days-to-90 warning line; critical is warn/4
DASH_WARN_GROWTH=1.0      # disk growth %/day warning; critical is 5x
DASH_WARN_MEM_PCT=90      # RAM warning line; critical is +7, capped at 99
DASH_WARN_OFFLINE_S=600   # offline warning; critical is 6x
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
pseudo filesystems, snap loops and duplicate mounts. `pve-host-agent.py` reports the
Proxmox host's root filesystem; LXC guests have no volume data and send none. The server
drops malformed entries, caps the list at 16 and recomputes `disk_pct` as the fullest
volume. Agents that have not been updated keep working: they send only `disk_pct`, so they
get no breakdown and their single gauge stays as it was.

Every accepted `disks` entry is also written to `disk_samples`, so per-volume growth and
days-to-90 survive the live payload and are available after a restart. The local
container's volume is included on every sample pass.

### Extra signals

Optional top-level fields on `/api/push/<name>` feed `metric_samples`:

| Field | Meaning |
|---|---|
| `load_1`, `load_5`, `load_15` | run-queue load averages |
| `swap_used`, `swap_total` | bytes; the API derives `swap.pct` |
| `net_rx_bps`, `net_tx_bps` | bytes per second across real interfaces |
| `cpu_iowait`, `cpu_steal` | percent, clamped to 0–100 |

`dash-agent.py` samples `/proc/loadavg`, swap, `/proc/net/dev` (twice, one second
apart) and `/proc/stat` on Linux; on macOS it uses `os.getloadavg()`,
`sysctl vm.swapusage` and `netstat -ib`. `pve-host-agent.py` reports the same host
signals and none for LXC guests. `dash-agent.ps1` reports the Windows page file as
swap and `Get-Counter` network rates as best effort; Windows has no load average
or iowait/steal, so those fields are never sent. The PowerShell path cannot be
executed in this repo's test environment — the server side is covered with
synthetic payloads, and the source-level assertions guard the optional fields.

Signals are raw-only and the API caps their series at the last 24 hours
(`SIGNAL_WINDOW_S`). A 90d/1y range does not scan signal history just to draw a
sparkline; the strip says which window it is showing. Missing signals are omitted
from `signals`, never filled with zero.

## Agent updates

An installed agent is never revisited by hand — the Windows task, cron entry or LaunchAgent
keeps running whatever the installer downloaded. So the served copy is the source of truth:

- At the start of every run, `dash-agent.ps1` and `dash-agent.py` fetch
  `GET /agents/dash-agent.<ext>`, compare it with the file on disk, and replace the file
  when the text differs. The next run (or, in `--loop` mode, an immediate `execv`) runs the
  new code. A failed fetch, an unreadable file or a read-only install is ignored: the agent
  keeps reporting with the copy it already has.
- `DASH_AGENT_UPDATE=0` pins a development copy so editing it is not immediately undone
  by the server's version.
- Deploy publishes the repository's agent files to `static/agents/` (see `deploy.sh`), so a
  merged agent fix reaches every device within a minute of the deploy, with no reinstall.
- This is deliberately a remote-code path for the dashboard host: a token holder already
  controls what the device reports. Keep the server, `/opt/stack/dash/.env` and the tailnet
  itself as the trust boundary. A device installed **before** this mechanism existed still
  needs the installer run once; after that it updates itself.

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
cp ../index.html ../app.js ../stack.js ../style.css ../metrics.html ../metrics.js ../metrics.css static/
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

For the layout, copy the seven front-end files into `server/static/` and open the app:

```bash
mkdir -p server/static
cp index.html app.js stack.js style.css server/static/
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
a step, min/max bands and a derived summary; 7d/30d route through `rollup_5m` and
90d/1y through `rollup_1h`; all four rollup tables contain rows; a push with a `disks`
array becomes per-volume series with growth/day and days-to-90 and a compact summary,
an oversized list is capped at 16; a push with extra signals exposes load/swap/network/
CPU pressure, while an old payload still works and gains no synthetic zeros; a deploy
annotation becomes a marker and Events row; server-side warnings flag disk and RAM;
a detected restart becomes an auto marker; unknown windows/devices are rejected; hits,
metrics history, per-volume history, extra-signal history **and** annotations survive
a full process restart; and the click feature sets no cookie and makes no third-party
request. It also guards the compose/README path documentation below and the
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
  a connection per request and writing concurrently. `disk_samples` is written inside
  the same lock and transaction as `samples`, and both are pruned on the same pass.
- **Per-volume series are joined to `samples` by `(device_id, ts)` and filtered to
  `online = 1`.** This is deliberate: `agents.json` keeps an offline agent's last
  `disks` values, and without the join those stale values would draw a flat,
  falsely-recent volume trend. If you add another writer to `disk_samples`, it must
  also create the matching `samples` row or its history will never be served.
- **Rollups are deleted and rewritten one bucket at a time.** Do not "fix" a rollup
  row by editing it in place outside `_refresh_rollups`: the delete+rewrite is what
  makes a late heartbeat inside the same bucket replace rather than double-count.
  Raw rows are still the source of truth for ≤24 h windows and for rebuilding a
  rollup; rollups only need to cover the retention windows that route to them.
- **Summary caching is live-only and write-invalidated.** `metrics_summary(now=None)`
  caches for 30 s; passing an explicit `now` (tests, a backfill) deliberately bypasses
  it. `record_samples()` and the push handler call `_invalidate_summary()`, so a new
  device or value appears on the next poll rather than after the TTL. Any new write
  path must do the same, or it will look invisible for up to 30 s.
- **Signal history is intentionally raw-only and capped at 24 h.** Do not widen
  `SIGNAL_WINDOW_S` without either a rollup tier or a cache: `metric_samples` has
  one row per metric per bucket, so a 30-day query would scan several hundred
  thousand rows just to draw a sparkline. The 24 h cap is what makes the strip
  cheap on every range.
- **Windows network counters are localized.** `Get-Counter` paths such as
  `\Network Interface(*)\Bytes Received/sec` differ by OS language, which is why
  the PowerShell agent treats them as optional. Never make a missing counter a
  hard failure; omit the keys instead.
- **Auto restart annotations are derived, not stored.** They come from the same
  restart transition detection as the Events rail, so they appear for long
  windows via rollups without a write per restart. Manual annotations are the
  only rows in the `annotations` table; a deployment note with `device: ""`
  intentionally shows on every device's chart.
- **The rail warning chip is live-only.** `/api/stats` evaluates only the fields
  already in the live payload, so days-to-90 and growth warnings exist on the
  metrics page but not on the dashboard rail. Do not add a history scan to the
  5 s stats poll just to complete the chip; use the metrics summary instead.
