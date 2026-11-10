# Benchmark method: trustworthy cold/warm and render numbers

Research note, 2026-11-10. **Measured** facts were collected read-only from `pve`, CT 200 `docker01`, and the tailnet. **Documented** facts cite the owning spec or first-party source. **Inference** is labelled where it matters.

## Bottom line

The dashboard is latency-bound, not bandwidth-bound: 27,617 B uncompressed, 8,488 B with zstd, 381 B per stats poll. Measured TTFB through `tailscale serve` is 17.7 ms median, about 14.9 ms of it TLS; the app answers in ~3 ms on loopback. At Slow 4G the bytes cost ~42 ms, so connection setup, the ingress, Google Fonts, and the Calendar iframe dominate. One "load time" number mostly measures Google.

## 1. Cold and warm are two axes

Separate process state from cache state, and name the cell:

- **C0, cold process**: new Chrome, new temporary profile, empty cache, no service worker. Lighthouse's default: `disableStorageReset: false`, mobile, `simulate` [2]; `chrome-launcher` uses a temporary `--user-data-dir` [3].
- **C1, cold cache / warm process**: same process, `Network.clearBrowserCache`, then navigate. DNS, TLS, and sockets may be warm; `Page.reload({ignoreCache: true})` is Shift-refresh [5].
- **W1, warm cache**: same context and URL, cache on. Verify `transferSize` 0 and `deliveryType: "cache"`; revalidation gives 300 [4]. Otherwise it is not W1.

A browser cache is private, and `no-store` means a cache "MUST NOT store any part" of the response [1]. **Measured**: every response from `https://pve.tail73491d.ts.net:8448` sends `cache-control: no-store`, including the three static assets. W1 is impossible today; the browser refetches 27.6 KB every navigation. "Warm" today is C1. The APIs also use `cache: 'no-store'`. After a cache-header fix, clear real clients with a fresh profile or `Clear-Site-Data: "cache"` [20].

## 2. Measure the path you actually have

**Measured**: `tailscale status --json` shows `jem-pc` at `CurAddr: 192.168.1.157:41641` and the MacBook at `192.168.1.223:41641`; `tailscale ping jem-pc` returns `via 192.168.1.157:41641 in 4ms`. Both clients share the LAN with `pve`, so "over Tailscale" today means direct LAN, not WAN or DERP. DERP latency to London measured 15.5 ms; JSON `RegionLatency` for region 18 was 21.4 ms. A first `netcheck` printed 889.2 ms for the same relay, an outlier to discard [6]. Two peers homed to `lhr` should see about twice that, 30–43 ms; that is inference.

Real clients enter through `tailscale serve`: TLS terminates at the tailnet name and the proxy forwards to `http://10.10.10.20:8448` [8]. Do not benchmark a loopback or direct container path and compare it with a real client; measured TTFB is 3 ms on loopback versus 17.7 ms through `tailscale serve`.

A genuine DERP number needs an off-LAN client, confirmed with `tailscale ping` (`via DERP(lhr)`) and `tailscale status` (`relay`, not `direct`) [7]. Record `CurAddr`, `Relay`, and the ping line with every run; discard the series if the path changes.

## 3. Simulate without lying

- `tc netem` is one-way and egress-only; for TCP realism it "must be placed on the ingress of the receiver host" [9]. Model RTT as one-way delay = RTT/2 each way, with `delay`, `loss`, `rate`, and `seed`.
- CDP emulation is per-request, not per-packet. `Network.emulateNetworkConditions` is deprecated for `emulateNetworkConditionsByRule` plus `overrideNetworkState`; latency is "minimum latency from request sent to response headers received", throughput is aggregated bytes/sec [5]. It cannot model handshakes, ACKs, or queueing; never stack it on netem.
- Lighthouse says simulated throttling has "inherent inaccuracy" and deep investigation "should use packet-level throttling tools" [10]. Use packet-level shaping for path claims.
- CPU: `Emulation.setCPUThrottlingRate` is a slowdown factor [5]; Lighthouse mobile uses 4x [10]. Do not add a cgroup cap and still call it the Lighthouse profile.
- Mobile: `Emulation.setDeviceMetricsOverride`, `setTouchEmulationEnabled`, `setUserAgentOverride` [5]; Lighthouse's moto g power is 412x823 at DPR 1.75 [2].
- Third parties: "full" and "hermetic" runs. Hermetic blocks `fonts.googleapis.com`, `fonts.gstatic.com`, and `calendar.google.com` with `Network.setBlockedURLs` [5]; it is an app-only lower bound.

| Profile | One-way delay | Rate | Loss | CPU | Basis |
|---|---|---|---|---|---|
| `local-direct` | 0–2 ms | unshaped | 0 | 1x | measured 4 ms tailnet ping |
| `derp-uk` | 15–21 ms (RTT 31–43 ms) | unshaped | 0 | 1x | measured DERP latency; derived |
| `mobile-slow4g` | 75 ms (RTT 150 ms) | 1.6/0.75 Mbps | 0 | 4x | Lighthouse preset [10] |
| `home-remote` | 20 ms (RTT 40 ms) | 20/5 Mbps | 0 | 1x | assumption; record it |

The `derp-uk` row models relay latency only, not DERP queueing or QoS.

## 4. What runs where

**Measured**: CT 200 has 2 vCPU, 2 GB RAM, 6 GB free; `dash` uses 24 MB, Caddy 61 MB, and neither has limits set. `pve` has 4 cores, 23 GB, no Docker, and no Chrome; CT 200 has no Chrome either. Docker's `--cpus` is a hard quota, `--cpu-shares` is a soft weight, `--memory` is hard [12].

- **Server**: CT 200, Docker 29.9.3, dash at `--cpus=0.5 --memory=256m --pids-limit=128`, Caddy at `--cpus=0.25 --memory=128m`. Measured usage makes these generous.
- **Synthetic Chrome**: not on CT 200. Lighthouse requires 2 dedicated cores, recommends 4 cores and 4–8 GB, and forbids concurrent runs [11]. CT 200 is exactly the minimum and would contend with the server. Run Chrome on `pve` (`--cpus=2 --memory=2g --shm-size=512m`) once Docker exists, or in a dedicated CT/VM [13].
- **Real clients**: headed Chrome on JEM-PC and the MacBook. Record browser version, OS, window size, power state, extensions, and tailnet path; label results "LAN direct".
- **Ingress**: every path should end at `https://pve.tail73491d.ts.net:8448` so `tailscale serve` is included.

## 5. Harness shape

1. **`bench-runner`**: Node + Playwright; owns CDP, resets state, injects observers, navigates, writes JSONL plus a trace. Playwright contexts are isolated [14]; CDP comes from `context.newCDPSession(page)` [15].
2. **`bench-server`**: dashboard + Caddy in CT 200, digest-pinned, only via `tailscale serve`.
3. **`bench-collector`**: same-origin `sendBeacon` sink for real clients.
4. **`bench-compare`**: validates manifests, pairs rounds, reports median difference and bootstrap 95% CI.

- C0 (new process/profile) and C1 (same process, `Network.clearBrowserCache`), then W1 only when cache headers permit it. For W1, verify `deliveryType: "cache"` or `transferSize: 0` [4], or fail the run as mislabelled.
- Inject before navigation with `Page.addScriptToEvaluateOnNewDocument` [5]: `PerformanceObserver` with `buffered: true` for `largest-contentful-paint`, `layout-shift`, `paint`, `longtask`, and `resource` [16]; `MutationObserver` marks for the first bookmark and device cards.
- Record FCP, LCP, CLS [18][19]; `bookmarks-ready` and `stats-ready`; TTFB and navigation timing; a resource table (URL, `transferSize`, `encodedBodySize`, `deliveryType`, `nextHopProtocol`); and `Performance.getMetrics` deltas for `TaskDuration`, `ScriptDuration`, `LayoutDuration`, `JSHeapUsedSize` [5].
- Do not use `loadEventEnd`: `load` waits for iframes [17], and Calendar is cross-origin.
- Sample size: 5 pilots, then 20 per cell, randomized ABBA order, one browser at a time. Lighthouse documents 5 runs plus median, which is "twice as stable as 1 run" [11]; 20 is inference for small LCP deltas. Report median, p10/p90, and a paired bootstrap CI.
- Artifacts: `manifest.json` (schema version, git SHA, image digests, Chrome version, OS/device, profile, block list, run order, host load, tailnet path); `runs.jsonl`; `traces/<run>.json.gz` from `Tracing.start` (`ReturnAsStream`, gzip) [5], hashed in the JSONL; optional NetLog [21].
- Compare only matching manifests; pair by round; refuse if the path changed, the browser differs, or the CI includes 0.

## 6. What makes a result untrustworthy

- Mixing ingress: loopback or direct container path versus `https://pve.tail...:8448` (3 ms versus 17.7 ms measured).
- Calling a LAN-direct run "remote" or "DERP" without recording `CurAddr` and `Relay`.
- Calling a `no-store` reload "warm", or leaving `Network.setCacheDisabled(true)` on.
- Stacking CDP emulation and netem, or treating per-request throttling as packet-level.
- Running Chrome and the server on the same 2 vCPU CT, or running tests concurrently.
- Letting third parties drift between arms without recording whether they were blocked.
- Best-of-N, median of 3, or comparing means of long-tailed timings.
- Using `load` as "dashboard ready", or changing Chrome/container versions mid-series.

## What this means for the dashboard

1. Optimise latency and connection setup, not bytes. At 8.5 KB compressed the page is already small; Slow 4G transfer is about 42 ms, and local TTFB is 17.7 ms.
2. `Cache-Control: no-store` on static assets is the biggest warm-load defect. ETag plus immutable caching would make W1 possible, but it is a server change and must be benchmarked as one.
3. Google Fonts is render-blocking [22] and the Calendar iframe delays `load`. Measure LCP plus explicit marks, and keep a hermetic profile to attribute third-party cost.
4. Real-client A/B on the current tailnet is LAN-direct. A remote or DERP claim needs an off-LAN client or a labelled netem profile.
5. CT 200 is at Lighthouse's hardware minimum. Run one browser at a time, preferably on PVE.

## Open questions

- Is `no-store` intentional for static assets, or a dash default?
- Is the Calendar iframe worth its cost, given that its internals are unmeasurable?
- Can Docker be installed on `pve`, or should a dedicated CT host the runner?
- Are either clients ever off-LAN? Today both are on 192.168.1.x.
- What effect size matters (50 or 100 ms LCP), and which client Chrome versions can be frozen for a series?
- Is HTTP/3 in scope? The current path is h2 through `tailscale serve`.

## Sources

1. RFC 9111, HTTP Caching. https://www.rfc-editor.org/rfc/rfc9111.html
2. Lighthouse default settings and screen metrics, `core/config/constants.js`. https://github.com/GoogleChrome/lighthouse/blob/main/core/config/constants.js
3. chrome-launcher temporary `--user-data-dir`. https://github.com/GoogleChrome/chrome-launcher/blob/main/src/chrome-launcher.ts
4. W3C Resource Timing, `transferSize`, `encodedBodySize`, `deliveryType`. https://www.w3.org/TR/resource-timing/
5. Chrome DevTools Protocol, `browser_protocol.json` (Network, Performance, Emulation, Page, Tracing). https://github.com/ChromeDevTools/devtools-protocol/blob/master/json/browser_protocol.json
6. Tailscale DERP servers. https://tailscale.com/kb/1232/derp-servers
7. Tailscale connection types. https://tailscale.com/kb/1257/connection-types
8. Tailscale Serve. https://tailscale.com/kb/1312/serve
9. `tc-netem(8)`. https://man7.org/linux/man-pages/man8/tc-netem.8.html
10. Lighthouse throttling. https://github.com/GoogleChrome/lighthouse/blob/main/docs/throttling.md
11. Lighthouse score variability. https://github.com/GoogleChrome/lighthouse/blob/main/docs/variability.md
12. Docker resource constraints. https://docs.docker.com/engine/containers/resource_constraints/
13. Docker run reference. https://docs.docker.com/reference/cli/docker/container/run/
14. Playwright browser contexts. https://playwright.dev/docs/browser-contexts
15. Playwright CDPSession. https://playwright.dev/docs/api/class-cdpsession
16. MDN, `PerformanceObserver.observe()` `buffered`. https://developer.mozilla.org/en-US/docs/Web/API/PerformanceObserver/observe
17. MDN, Window `load` event. https://developer.mozilla.org/en-US/docs/Web/API/Window/load_event
18. web.dev, Largest Contentful Paint. https://web.dev/articles/lcp
19. web.dev, Cumulative Layout Shift. https://web.dev/articles/cls
20. MDN, `Clear-Site-Data`. https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Clear-Site-Data
21. Chromium NetLog. https://chromium.googlesource.com/chromium/src/+/main/net/docs/net-log.md
22. Lighthouse audit, render-blocking resources. https://developer.chrome.com/docs/lighthouse/performance/render-blocking-resources
