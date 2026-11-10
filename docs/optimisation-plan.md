# Dashboard optimisation plan

**Goal.** Make the homelab dashboard feel immediate and cheap to display in Chrome over Tailscale on CT 200 `docker01`, without changing the look or the data contract.

**Non-goals.** Production cutover is the implementer's job via `deploy/deploy.sh`. No visual redesign. No telemetry history or new integrations. The Google Calendar agenda is dropped, not replaced. No CI and no public (Funnel) exposure.

## Current state (10 Nov 2026)

```
Chrome → tailscale serve :8448 (TLS, h2) → 10.10.10.20:8448 → Caddy → dash:8000 (stdlib Python)
```

CT 200: 2 vCPU, 2 GB RAM, 7.8 GB disk. PVE: i5-7500, 4 cores, 23 GB. The page is 27.6 KB raw; `/api/stats` 979 B; `/api/bookmarks` 3.9 KB. Every response is `Cache-Control: no-store`; the Caddy→Python upstream is HTTP/1.0. The live stack was hardened at ~12:35 on 10 Nov with basic auth, HSTS/security headers, pinned digests, read-only root filesystems and 128/256 MB caps. `app.py`, the Caddyfile and the compose file are untracked.

The costs, in order:

1. **Calendar iframe** — 147.5 KB and 246 KB of JS when signed in; ~1.1 s of parent `load` and ~1.0 s of FCP/LCP even when it returns a 401. Silent on failure.
2. **Google Fonts** — 22.9 KB plus a render-blocking third-party stylesheet; Outfit is loaded but never used.
3. **`no-store`** — repeat loads re-download everything; warm bytes 10.7 KB instead of 1.7 KB.
4. **Client rendering** — a full-viewport `blur(140px)` re-runs every frame because the animated blobs change the filtered subtree; σ=140 exceeds Skia's fast paths. `backdrop-filter` adds a per-element pass. The 10 s `innerHTML` rebuild is a periodic spike.
5. **Server shape** — not a lever: Caddy `file_server` and Python static are within noise. TTFB is path latency (~4 ms direct, ~45 ms at 40 ms RTT, ~115 ms at 100 ms); the app answers in under 5 ms.
6. **TLS/Tailscale** — ~8–9 ms of the tailnet TTFB is the handshake. Keep `tailscale serve`; no sidecar, no HTTP/3, no h2c.

## Target metrics

| Metric | Direct LAN | Remote 40 ms | Mobile 100 ms |
|---|---:|---:|---:|
| TTFB | ≤ 30 ms | ≤ 75 ms | ≤ 150 ms |
| FCP | ≤ 400 ms | ≤ 550 ms | ≤ 700 ms |
| LCP | ≤ 500 ms | ≤ 650 ms | ≤ 900 ms |
| load | ≤ 200 ms | ≤ 350 ms | ≤ 500 ms |
| Cold bytes (dashboard origin) | ≤ 26 KB | ≤ 26 KB | ≤ 26 KB |
| Cold requests | ≤ 7 | ≤ 7 | ≤ 7 |
| Warm bytes | ≤ 3 KB | ≤ 3 KB | ≤ 3 KB |
| Warm TTFB | ≤ 30 ms | ≤ 75 ms | ≤ 150 ms |
| Long tasks | none > 50 ms | none > 50 ms | none > 50 ms |

26 KB assumes the self-hosted font subset; if the system stack is chosen instead, tighten to 12 KB. Zero third-party requests is part of the target. Idle CPU ≤ 2% of one core, no continuous compositor work from blur/backdrop, and battery no worse than the recorded baseline are verified post-cutover.

## Architecture

- **Edge:** `tailscale serve` on PVE remains the only user-facing entry; TLS terminates there. Caddy binds `10.10.10.20:8448`; only PVE `10.10.10.1` may reach it. This closes the current `0.0.0.0:8448` LAN bypass.
- **Containers:** Caddy (basic auth, HSTS/security headers, `encode { gzip 9 zstd }`) → `dash` (Python 3.13 stdlib) for every path. Keep the hardened compose: pinned digests, read-only root filesystems, `cap_drop: ALL`, `no-new-privileges`, pids limits, 128/256 MB caps.
- **Server:** Python keeps serving static files and the API. Add `protocol_version = "HTTP/1.1"` for keep-alive; no h2c, no framework, no sidecar. Telemetry stays push-only; the PVE pull is removed.
- **Version control:** `server/app.py`, `deploy/Caddyfile`, `deploy/docker-compose.yml`, `deploy/deploy.sh`, the static files and `fonts/` are tracked.

## Changes

### Delivery and assets

- Static assets: `Cache-Control: public, max-age=0, must-revalidate` + weak ETag `W/"<mtime_ns>-<size>"` + 304. `/api/bookmarks`: `no-cache` + ETag. `/api/stats`: `no-store`.
- Remove `fetch(cache: 'no-store')` from `app.js`.
- Caddy `encode { gzip 9 zstd }`; no precompressed sidecars, no brotli build.
- Self-host `fonts/space-grotesk-subset.woff2` (14,664 B, 97 glyphs, all weights, OFL) with `@font-face` and `font-display: swap`; `Cache-Control: public, max-age=31536000, immutable` on a versioned filename. Drop Outfit and all Google Fonts links/preconnects.
- Remove the calendar iframe and the sidebar; the main content becomes full-width.
- Keep `index.html`, `app.js`, `style.css` separate. No inlining, bundling, minifier, preload or service worker.
- Remove the 4.3 KB embedded bookmarks copy from `app.js`; inject the server copy into the HTML at serve time.

### Client rendering

- Bake the three blobs into static radial gradients, or move the filter onto each blob; stop the full-viewport σ=140 Gaussian. If the drift stays, animate only `transform`/`opacity` at 10–15 Hz.
- Replace card/panel `backdrop-filter: blur()` with a translucent fill that matches the look.
- Scope the universal `* { transition: ... }` to `a`, `button`, `.bookmark-card`; remove it from layout/theme flips.
- Add `@media (prefers-reduced-motion: reduce)` to stop the drift.
- Diff device cards and update only changed text and bar widths; do not replace `innerHTML` every 10 s.
- Pause polling on `visibilitychange`; resume on focus.
- Do not add `will-change`.

### Data seams

- Keep push-to-server + 10 s browser polling and the 5 s server cache; no SSE/WebSocket.
- Add a lock around the `load_agents()` → mutate → `save_agents()` sequence in `POST /api/push/*`.
- Delete `PVE_HOST`, `PVE_TOKEN`, `proxmox_devices()` and the unverified-TLS helper (~60 lines).
- Make `data/bookmarks.json` the single source of truth; inject it into `index.html` at serve time. Keep `/api/bookmarks` for compatibility.
- On a failed `/api/stats` fetch, keep the last data and show a stale/offline indicator; retry with backoff. Never hide the network panel permanently. On a bookmark failure, show an error state.
- Keep the basic auth and push token; no new auth surface.

## Data contract

- `GET /api/stats` → `{generated, devices[]}`; each device: `id, name, kind, source, cpu_pct, mem_pct, mem_used, mem_total, disk_pct, uptime_s, online, last_seen`, optional `error`. `no-store`.
- `POST /api/push/<name>` with `X-Dash-Token` → stores the heartbeat in `data/agents.json`; agents every 60 s, offline after 150 s.
- `GET /api/bookmarks` → `{work:[...], personal:[...]}`; served `no-cache` + ETag for compatibility, but the initial page is rendered from the same file.
- `data/agents.json` and `data/bookmarks.json`; no history.

## Deploy

Tracked: `server/app.py`, `deploy/Caddyfile`, `deploy/docker-compose.yml`, `deploy/deploy.sh`, `deploy/.env.example`, static files, `fonts/`, and a seed `data/bookmarks.json`.

Host: `/opt/stack/shared/.env` (`DASH_TOKEN`, `DASH_AUTH_HASH`, `DASH_USER`, `DASH_MEM_TOTAL`; no `PVE_*`), `/opt/stack/shared/data/` (live bookmarks + agents; never overwritten by a deploy), `/opt/stack/releases/<timestamp>/`, `/opt/stack/current` symlink.

`deploy/deploy.sh` fetches a pinned commit, builds a self-contained release, seeds bookmarks only if absent, preflights for forbidden third-party references, runs `docker compose up -d`, smoke-tests `/api/health` with credentials from the shared env, points `current` at the release, keeps three releases, and prints the rollback command. Rollback is one symlink plus one compose command. The Caddy hash comes from `{$DASH_AUTH_HASH}`. Remove the stale `/opt/stack/dashboard` and the dead `PVE_*` keys on the first deploy. No CI.

## Verification

- Lab: branch `lab/bench`, `lab/bench/README.md`, `lab/bench/results/report.md`. Re-run after changes; compare against the target table.
- Post-cutover client pass on JEM-PC and the MacBook: cold/warm Network summary, Navigation/Resource Timing, LCP, long tasks; DevTools Performance Frames/GPU; Task Manager idle CPU; `powercfg`/`powermetrics` battery. Acceptance is the target table plus the GPU/CPU/battery criteria.
- On-demand regression: keep the lab runner and rerun it after delivery changes; no CI.

## Ordered implementation checklist

1. Add `server/app.py`, `deploy/*`, static and font assets to the repo; remove the dead PVE pull.
2. Apply caching, weak ETags, 304s and `protocol_version = "HTTP/1.1"` in `app.py`.
3. Drop the calendar iframe and Google Fonts; add the self-hosted subset and the full-width layout.
4. Apply the rendering changes (gradients, translucent fill, scoped transitions, reduced motion, diffed cards, hidden-tab pause).
5. Apply the data changes (lock, server-injected bookmarks, stale/offline handling).
6. Bind Caddy to `10.10.10.20:8448` and restrict it to PVE `10.10.10.1`; move the auth hash to the shared env.
7. Add `deploy/deploy.sh`; cut the first release; remove `/opt/stack/dashboard` and `PVE_*`.
8. Verify against the target table, then run the client pass and record the GPU/battery result.

## Risks and open items

- The font subset must look identical; if it does not, fall back to the system stack and tighten cold bytes to 12 KB.
- The basic-auth 401 adds one cold round trip; measure it in the client pass.
- The lab's calendar 401 understates the signed-in iframe cost; the drop decision is still correct.
- Headless Chromium cannot judge GPU or battery; the render changes are verified after cutover.
- The LAN-port restriction needs an LXC/PVE firewall rule and a check from another LAN device.
- The live hardening may keep changing; re-pin the config hashes before cutover.
- Kiosk/autostart behaviour on JEM-PC and the MacBook is unchanged by this plan and remains OS-specific.
- The phone remains a best-effort target; the mobile profile is a guardrail, not a commitment.

## Evidence

Tickets: the map index links every decision. Research branches: `research/static-delivery`, `research/tailscale-edge`, `research/benchmark-method`, `research/third-party-weight`, `research/render-cost`, `research/data-seams`. Lab: `lab/bench`.
