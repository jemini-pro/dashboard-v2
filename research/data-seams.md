# Data seams: polling, push, caching and persistence

Research note, 2026-11-10. Method: read-only inspection (`curl` against the dash container and
Caddy, `/opt/stack/dash/app.py`, `data/*.json`, cgroup counters) plus primary sources. Labels:
**[M]** measured here, **[D]** documented, **[I]** inference. Companion notes:
`research/static-delivery.md`, `research/tailscale-edge.md`.

**Verdict.** Keep push-to-server plus poll-to-browser. Three clients cost ~0.03% of one core and
~16 MB/day; SSE/WebSocket add lifecycle code for latency nobody needs. Fix the poll-failure
path, slow the poll, make bookmarks one source, and delete the dead PVE pull. Add a lock around
`agents.json`; SQLite is not justified.

## 1. What the seam costs today

Host: 2 vCPU / 2 GB; `dash` idles at 1 thread / 20.2 MB RSS [M].
`/api/stats` is generated on demand, cached 5 s, polled every 10 s (`app.js:287`) [M]. Agents
push every ~60 s (observed 60–100 s); offline after 150 s [M/D].

| Item | Value |
|---|---:|
| Stats body | 979 B raw; 881 B compact; 373–384 B gzip/zstd |
| Caddy headers + body per poll | 245 B + ~373 B = ~618 B |
| Request CPU | 0.66 ms cache hit; 1.9 ms cache miss |
| 20 concurrent stats requests | all 200; slowest 18 ms |

Three clients: 25,920 requests/day, ~16 MB/day (~0.5 GB/month), ~15.6 ms CPU/min = 0.026% of one
core (0.013% of 2 vCPU); no cache would still be ~0.06%. Not a resource problem.

## 2. Polling vs SSE vs WebSocket

**Polling.** Cost: 18 requests/min for three clients; HTTP/1.0 means each poll is a fresh
upstream TCP connection [M/D, RFC 9112 §9.3]. Benefit: stateless, independent
retries. Failure: the client catches every rejection and hides the network panel permanently,
never restoring `display` on success (`app.js:280–281`) [M]. One failed poll (wake, Tailscale
path change, restart) blanks the panel until reload — the dominant defect, independent of
transport.

**SSE.** Cost: one socket and one held Python thread per client [D]; a streaming response path,
because the handler always sends `Content-Length` and defaults to HTTP/1.0 [M/D]; a broadcaster
with per-client queues. Benefit: no repeated handshakes, one shared generation, push latency
bounded by the sampler or agent POST. `EventSource` auto-reconnects after an implementation-
defined delay, honours `retry:`, sends `Last-Event-ID`, cannot set custom headers, and uses
`no-store` [D, WHATWG HTML]. Caddy flushes `text/event-stream` immediately [D]; `tailscale serve`
has no inbound read/idle timeout and upgrades/downgrades paths automatically (companion note),
so a stream should survive path changes, with browser reconnect if TCP breaks [I].

**WebSocket.** No WebSocket server exists in the Python standard library, and RFC 6455 defines
no automatic reconnection — only a warning against immediate retry after abnormal closure [D].
Handshake, framing, ping/pong and backoff are app code for unused bidirectionality.

Prior art: Prometheus pulls, using Pushgateway only where push is unavoidable [D]; Home Assistant
streams state over WebSocket because it is bidirectional and many-entity [D]. Here agents must
push (behind NAT); browsers can pull.

## 3. Server cache and conditional GET

The 5 s cache saves ~1.2 ms per request, ~7 ms/min for three clients [M/I]; keep it. Raising it
to 10 s makes every poll a hit and is harmless for 60 s-old data, but CPU does not require it.
Per process; a restart pays 1.9 ms.

Conditional GET is a poor fit. Every response is `no-store` with no `ETag`/`Last-Modified`, so a
conditional request gets a full 200 [M]. A 304 is bodyless [D, RFC 9110 §15.4.5] but still costs
a request and ~245 B of headers; saving the ~373 B body would be ~3.2 MB/day/client, if it
ever matched. The body
includes `generated` and every `last_seen`, so its ETag changes each 5 s regeneration; a 10 s
poll against a 5 s cache rarely matches. `fetch(..., {cache:'no-store'})` also bypasses the HTTP
cache [D, Fetch §2.2.5], so server `max-age` alone does nothing. Leave `/api/stats` `no-store`;
to spend fewer bytes, poll less. Statics/bookmarks: see `static-delivery`.

## 4. File vs SQLite persistence

`agents.json` is 695 B, three records, ~4,320 writes/day; stats reads it on each cache miss
(~12/min) [M/I]. `save_agents` uses temp-file + rename, so readers see old or new, never a torn
file; without `fsync` a power cut can lose the last update but should not corrupt it [I].

The real defect is the unlocked read-modify-write: `do_POST` calls `load_agents()`, mutates, then
`save_agents()` without `_lock` [M]. Simultaneous pushes can lose an update: at 4,320 writes/day
and a ~1 ms window, ~0.1 lost updates/day [I]; aligned timers or a power-restore burst make it
worse. A two-line lock fixes it.

SQLite gives atomicity and crash durability, but three writers/min is not the bottleneck. WAL
allows one writer with concurrent readers [D]; `synchronous=FULL` syncs each commit, `NORMAL`
does not [D]; WAL fails on network filesystems [D]. It adds a schema,
migrations, `-wal`/`-shm`, and ~1–3 MB RSS [I]. Justified only for history, aggregates, >~10
writers, or multiple processes — out of scope. Read-only `bookmarks.json` gains nothing.

## 5. Consolidating the two bookmark sources

The embedded `const bookmarks` in `app.js` is 4,357 B — 36% of the 12,184 B file — while the
server body is 3,888 B [M]. Both hold the same 31 items [M]; no drift today, but two
hand-maintained copies will drift.

The fallback also masks failure: `loadBookmarks()` paints the embedded copy, then fetches; on
error it logs and keeps the copy with no retry (`app.js:206–214`) [M]. A server fault is
invisible and stale bookmarks persist. Best: make `bookmarks.json` the only hand-edited source
and inline it into `index.html` as a JSON script block — instant paint, one source, one less
request; the index becomes dynamic, fine for one user. Deleting it and fetching with a
loading/error state is simpler. Add a retry/stale badge. Compact JSON saves 276 B on bookmarks,
98 B on stats [M], mostly already compressed.

## 6. Removing the dead PVE pull

`PVE_HOST`/`PVE_TOKEN` are empty; `PVE_NODE` is `pve`; health returns
`{"ok":true,"pve":false}` [M]. `proxmox_devices()` returns `[]` before any network call, and the
push agent already covers pve [M]. Delete `_pve_get`, `proxmox_devices`, the `ssl`/`urllib.request`
imports, the `PVE_*` reads and health field: ~60 lines, plus a latent footgun
(`_create_unverified_context()` if revived) [M]. `.env`/`.env.bak` still carry `PVE_*` keys; the
backup also holds an old `DASH_TOKEN` — remove them (values not read) [M].

Bigger issue: `app.py` is untracked — `origin/main` has only the three static files and no
`app.py` history [M]. Version it before editing.

## Decision table

| Option | Cost here | Benefit | Failure mode | Move when |
|---|---|---|---|---|
| Keep 10 s polling | ~0.03% core, 16 MB/day | Stateless, independent retries | Panel hidden after one error | ≤10 clients, ≥10 s freshness |
| Poll 30–60 s | 3–6× less traffic/CPU | Matches 60 s agents | docker01 stats 60 s stale | If 10 s local CPU unused |
| SSE | Thread+socket/client; stream path | Push latency; fewer requests | Restart drops; backpressure; auto-reconnect | >10 clients, <5 s, agents <10 s |
| WebSocket | Handshake/framing/reconnect; no stdlib | Bidirectional | No auto-reconnect | Only if commands needed |
| ETag/304 on stats | +245 B headers, validation | Saves ~373 B | Body changes every 5 s | Cache TTL ≥ poll; metered |
| JSON + `_lock` | Two lines | No lost updates; readable | No fsync | Now |
| SQLite | Schema, WAL, 1–3 MB RSS | Atomicity, queries | WAL invalid on network FS | History, >10 writers |
| Inline/remove bookmark fallback | Dynamic index or fetch | One source | Loading state on failure | Now |
| Remove PVE pull | ~60 lines deleted | Less code, no unverified TLS | None (dead) | Now |

## What this means for the dashboard

At three browsers, polling 979 B every 10 s is rounding error on 2 vCPU / 2 GB, and the 5 s cache
coalesces them. SSE adds a held thread per client, a streaming path and reconnection/backpressure
code to save a few megabytes and milliseconds per day. Do the cheap work: make one failed poll
recover (show staleness), poll every 30–60 s unless 10 s docker01 CPU matters, lock the agents
read-modify-write, make bookmarks one source, delete the PVE pull. Skip conditional GET on
`/api/stats`; the body changes every generation and the client bypasses the cache. Revisit SSE
only if clients, update rate or latency change by ~10×.

## Open questions

- How many browsers stay open, and are any off-LAN/mobile? That is the real SSE threshold.
- Does one failed fetch blank the deployed panel? Clear in code, unconfirmed live.
- Are bookmarks edited on the server, in the repo, or both? That picks inline vs fetch.
- Is history ever in scope? If yes, SQLite; if no, JSON plus a lock. Do agent timers fire
  together? That sets the race frequency.

## Sources

- Python [`http.server`](https://docs.python.org/3/library/http.server.html); [RFC 9111](https://www.rfc-editor.org/rfc/rfc9111.html), [RFC 9110](https://www.rfc-editor.org/rfc/rfc9110.html), [RFC 9112 §9.3](https://www.rfc-editor.org/rfc/rfc9112.html).
- [WHATWG SSE](https://html.spec.whatwg.org/multipage/server-sent-events.html); [Fetch §2.2.5](https://fetch.spec.whatwg.org/#request-cache-directive); [RFC 6455](https://www.rfc-editor.org/rfc/rfc6455.html).
- [Caddy reverse_proxy](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy); [SQLite WAL](https://www.sqlite.org/wal.html), [Appropriate Uses](https://www.sqlite.org/whentouse.html), [How to Corrupt](https://www.sqlite.org/howtocorrupt.html).
- [Tailscale NAT traversal](https://tailscale.com/blog/how-nat-traversal-works), [DERP](https://tailscale.com/kb/1232/derp-servers), [Serve](https://tailscale.com/kb/1242/tailscale-serve); [Home Assistant WebSocket](https://contributors.home-assistant.io/core/internals/websocket-api/); [Prometheus FAQ](https://prometheus.io/docs/introduction/faq/).

All inspection was read-only.
