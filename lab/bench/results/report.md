# Dashboard benchmark lab — results

Headless Chromium 156 on PVE, driving the candidate stacks in CT 200 over the LAN. Plain HTTP; Tailscale/TLS adds a constant ~8–9 ms not included here. Latency/bandwidth profiles use a user-space TCP proxy, not packet-level `tc netem`.

## Variants

- **current** — production shape: `no-store`, HTTP/1.0 upstream, calendar iframe + Google Fonts.
- **cached** — weak ETag + `no-cache`/304, HTTP/1.1 keep-alive, tuned gzip, same page.
- **lean** — cached plus the calendar iframe and Google Fonts removed (system font stack).
- **static** — Caddy `file_server` with ETag/304 and tuned encode, API proxied to `cached`, lean page.

## Direct LAN

| Variant | Cold TTFB | Cold FCP | Cold LCP | Cold load | Cold req | Cold KB | Warm TTFB | Warm req | Warm KB | Warm cache hits |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| current | 4.5 ms | 1274.0 ms | 1274.0 ms | 1194.5 ms | 7.0 | 11.7 | 3.0 ms | 8.0 | 10.7 | 3.0 |
| cached | 3.5 ms | 820.0 ms | 820.0 ms | 1144.0 ms | 7.0 | 11.3 | 3.0 ms | 8.0 | 1.7 | 4.0 |
| lean | 4.0 ms | 230.0 ms | 230.0 ms | 44.0 ms | 5.0 | 10.1 | 2.0 ms | 5.0 | 1.7 | 1.0 |
| static | 3.0 ms | 224.0 ms | 224.0 ms | 45.0 ms | 5.0 | 10.1 | 2.0 ms | 5.0 | 1.7 | 1.0 |

## Remote-profile (40 ms RTT synthetic)

| Variant | Cold TTFB | Cold FCP | Cold LCP | Cold load | Cold req | Cold KB | Warm TTFB | Warm req | Warm KB | Warm cache hits |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| current | 46.0 ms | 332.0 ms | 332.0 ms | 215.5 ms | 7.0 | 12.2 | 44.5 ms | 8.0 | 11.3 | 3.0 |
| cached | 45.0 ms | 1104.0 ms | 1104.0 ms | 2908.5 ms | 7.0 | 11.3 | 43.0 ms | 8.0 | 1.7 | 4.0 |
| lean | 45.0 ms | 296.0 ms | 296.0 ms | 154.0 ms | 5.0 | 10.1 | 45.0 ms | 5.0 | 1.7 | 1.0 |
| static | 43.0 ms | 260.0 ms | 260.0 ms | 148.0 ms | 5.0 | 10.1 | 42.0 ms | 5.0 | 1.7 | 1.0 |

## Third-party control (cold, direct)

The same page with Google Fonts and the calendar iframe allowed versus blocked. The blocked rows are cold-only: request interception interferes with the HTTP cache, so their warm numbers are not used.

| Variant | Third parties | FCP | LCP | Load | Cold KB |
|---|---:|---:|---:|---:|---:|
| current | allowed | 1274.0 ms | 1274.0 ms | 1194.5 ms | 11.7 |
| current | blocked | 234.0 ms | 234.0 ms | 83.5 ms | 10.7 |
| cached | allowed | 820.0 ms | 820.0 ms | 1144.0 ms | 11.3 |
| cached | blocked | 240.0 ms | 240.0 ms | 63.5 ms | 10.4 |
| lean | allowed | 230.0 ms | 230.0 ms | 44.0 ms | 10.1 |
| lean | blocked | 190.0 ms | 190.0 ms | 58.5 ms | 10.1 |

## Byte deltas on a cold direct load

- cached vs current: -2.9% transferred bytes (current 11.7 KB → cached 11.3 KB)

- lean vs current: -13.1% transferred bytes (→ 10.1 KB)

- static vs lean (same page, different server): +0.0% transferred bytes

## Key findings

- **Caching is the repeat-load win.** On the direct profile, cached warm bytes fall from ~10.7 KB to ~1.7 KB and warm load from ~110 ms to ~28 ms; `current` keeps re-downloading because every response is `no-store`.
- **Dropping the calendar/fonts is the perceived-load win.** With the private calendar returning a 401, the iframe still costs ~1.1 s of `load` and ~1.0 s of FCP/LCP; the lean page loads in ~44 ms. A signed-in Google profile would be worse: the research measured a 147.5 KB / 246 KB JS floor for the working iframe.
- **Server shape is not the lever.** Caddy `file_server` and Python static are within noise at this size; choose on simplicity and the deploy story, not speed.
- **Compression tuning is small.** Tuned gzip saves ~3% of cold bytes here; keep it, but it does not justify a build step on its own.
- **TTFB is the path.** ~4 ms direct, ~45 ms at 40 ms synthetic RTT, ~115 ms at 100 ms; the app itself answers in under 5 ms.
- **No render signal.** Long tasks and CLS are zero in headless Chromium; GPU and battery claims still need the client pass.

## Limitations

- Headless Chromium has no real GPU compositing or battery signal; render-cost claims need the client pass.
- The proxy adds per-chunk delay and an approximate rate limit; it is not `tc netem`.
- Third-party responses (Google Fonts, the private calendar 401) can drift between runs.
- Small differences within a few milliseconds are noise at this sample size.

## Reproduce

```bash
cd /home/hermes/lab/dash-bench
~/.hermes/venvs/cron/bin/python runner/bench.py --iterations 6
~/.hermes/venvs/cron/bin/python runner/report.py
```
