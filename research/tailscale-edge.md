# Is `tailscale serve` on pve the right edge for the dashboard?

**Verdict:** Yes, for a single-user, tailnet-only dashboard. It terminates TLS with an automatically renewed Let's Encrypt certificate, negotiates HTTP/2, reuses backend connections, and adds roughly 0.1–0.4 ms per warm request over calling Caddy directly. It costs capability (no HTTP/3, no edge cache/compression) and one security gap unrelated to the edge: Caddy is published in plaintext on the LAN.

Tags: **[M]** measured read-only on pve/CT 200 on 2026-11-10; **[D]** documented in a primary source or the running source version; **[I]** inference.

## 1. What is actually terminating TLS

`tailscale serve status` [M] shows three listeners marked `(tailnet only)`, including `https://pve.tail73491d.ts.net:8448 |-- / proxy http://10.10.10.20:8448`. `tailscale funnel status` shows the same, so Funnel is off [M]; public exposure needs a separate Funnel configuration ([KB 1242](https://tailscale.com/kb/1242/tailscale-serve)).

`ss -ltnp` [M] shows the listeners bound to `100.68.196.99%tailscale0` and `[fd7a:…]%tailscale0`, not `0.0.0.0`. A TLS probe [M] returns TLS 1.3 / AES-128-GCM with `ALPN protocol: h2`; the certificate is `CN=pve.tail73491d.ts.net`, issuer Let's Encrypt, valid 2026-10-23 to 2027-01-21 (90 days).

In the running version (1.104.5), Serve is a `net/http.Server` whose TLS config uses `serveTLSNextProtos()` and calls `ServeTLS` per accepted connection ([serve.go:680-691](https://github.com/tailscale/tailscale/blob/v1.104.5/ipn/ipnlocal/serve.go#L680-L691)). `serveTLSNextProtos` returns exactly `{"h2", "http/1.1"}` ([cert.go:144-150](https://github.com/tailscale/tailscale/blob/v1.104.5/ipn/ipnlocal/cert.go#L144-L150)). There is no `h3` in that list and no QUIC listener (`ss -lunp` shows only UDP 41641 for tailscaled). So: HTTP/2 yes, HTTP/3 no.

Certificates are automatic: Tailscale requests a Let's Encrypt certificate for the `*.ts.net` name via DNS-01, the private key stays on the device, and certificates last 90 days ([KB 1153](https://tailscale.com/kb/1153/enabling-https)). That is a real operational advantage over alternatives that manage certificates themselves.

## 2. The proxy path and its limits

For plaintext backends, Serve uses `httputil.ReverseProxy` with an `http.DefaultTransport`-derived transport: 100 idle connections total, `MaxIdleConnsPerHost` at Go's default of 2, a 90 s idle timeout, and `ForceAttemptHTTP2` ([serve.go:1077-1099](https://github.com/tailscale/tailscale/blob/v1.104.5/ipn/ipnlocal/serve.go#L1077-L1099)). Because the backend URL is `http://`, that flag cannot produce h2c: Caddy's plaintext listener answered HTTP/1.1 to a default client [M]. Serve switches to h2c only for gRPC content types ([serve.go:1018-1027](https://github.com/tailscale/tailscale/blob/v1.104.5/ipn/ipnlocal/serve.go#L1018-L1027)), even though Caddy accepts h2c (`--http2-prior-knowledge` returns HTTP/2 200 [M]).

Reuse is real. After ten client requests, one `ESTAB` connection from `10.10.10.1` to `10.10.10.20:8448` remained, and it was still present after an idle interval [M]. Five requests issued by one curl process reported `num_connects=1` then `0` [M], so one h2 connection carried all five.

The inbound server is constructed with only `Handler` and `BaseContext` ([serve.go:680-686](https://github.com/tailscale/tailscale/blob/v1.104.5/ipn/ipnlocal/serve.go#L680-L686)), so Go's zero-value timeouts apply: with `IdleTimeout` and `ReadTimeout` both zero, "there is no timeout" ([server.go:3107-3111](https://github.com/golang/go/blob/go1.27.3/src/net/http/server.go#L3107-L3111)). Harmless on a tailnet; a slowloris concern if Funnel were ever enabled.

## 3. What it costs in latency and resources

Timings are pve→its own tailnet name (tailscaled's loopback path), so they bound the serving stack, not the client.

- Warm request through the edge (`GET /api/stats`, one reused h2 connection): 2.18, 2.29, 2.29, 2.50 ms [M].
- Same request direct to Caddy (`http://10.10.10.20:8448`): 2.01–2.41 ms [M]. Edge overhead is ≈0.1–0.4 ms, including the pve→CT hop.
- New connection through the edge: TLS `time_appconnect` 14.5–21.3 ms, TTFB 17.0–24.0 ms over ten samples [M]. That ~15 ms is loopback/intercept/server-setup cost, not a prediction for a phone [I]; a direct LAN peer answers `tailscale ping` in 7 ms [M].
- `tailscaled` uses 57,340 KiB RSS and 0.2% average CPU over ~24 h; Caddy 60.3 MiB; dash 24.7 MiB [M]. Serve adds no process.

Compression is Caddy's job. Measured identity vs `Accept-Encoding: zstd` [M]: `/` 2,819→1,136 B; `app.js` 12,184→3,986 B; `style.css` 12,614→3,366 B; `/api/stats` 978→383 B; `/api/bookmarks` 3,888→1,273 B. First load falls from 27.6 KB to 8.5 KB (69%). At these sizes the edge's lack of caching/compression is not a real cost.

## 4. MTU and WireGuard overhead

`tailscale0` has MTU 1280 [M]. Tailscale's source defines `safeTUNMTU = 1280`, the "Smallest MTU allowed for IPv6, current default", and `wgHeaderLen = 80` bytes worst case (40 IPv6 + 8 UDP + 4 type + 4 key index + 8 nonce + 16 tag) ([mtu.go:79,95-105](https://github.com/tailscale/tailscale/blob/v1.104.5/net/tstun/mtu.go#L79-L105)). A full 1280-byte inner packet is therefore at most 1,360 bytes on the wire — below 1500 and below 1492-byte PPPoE paths, so no fragmentation and no PMTUD dependency on ordinary paths. PMTUD probing is gated behind `TS_DEBUG_ENABLE_PMTUD`; otherwise the safe MTU is used ([mtu.go:133-142](https://github.com/tailscale/tailscale/blob/v1.104.5/net/tstun/mtu.go#L133-L142)).

The cost is a smaller MSS: 1240 vs 1440 bytes on a 1500-byte path, about 14% fewer payload bytes per segment and a similar single-stream ceiling at equal loss/RTT. For an 8.5 KB page and 383-byte polls that is one extra packet at most. Do not raise the MTU for this workload.

## 5. Direct versus DERP

Both real clients are currently direct on the LAN: `jem-pc` at `192.168.1.157:41641` and `Jem's MacBook Air` at `192.168.1.223:41641`, with `Relay: lhr` as the assigned fallback [M]. `tailscale ping jem-pc` returns 7 ms via the direct address [M]. `netcheck` reports UDP working, IPv4 and IPv6, `MappingVariesByDestIP: false` (easy NAT), and 19.2 ms to the London DERP relay [M]. Tailscale documents DERP as the fallback when direct connection or peer relay is unavailable, carrying WireGuard-encrypted traffic it cannot decrypt, with no published performance figures ([KB 1232](https://tailscale.com/kb/1232/derp-servers)). The phone was offline, so the mobile path is unmeasured.

## 6. Alternatives and what they would change

| Option | Buys | Costs |
|---|---|---|
| `tailscale serve` on pve (current) | Zero new processes, automatic cert, h2, tailnet-only bind | No h3, no edge cache/compression, depends on pve's tailscaled |
| `tailscaled` sidecar in CT 200 | Removes the pve→CT hop; keeps the service self-contained | Second tailnet node/key/DNS name; needs a TUN device with `NET_ADMIN`, or Tailscale's documented userspace fallback, which is a SOCKS5/HTTP proxy rather than a transparent edge ([KB 1112](https://tailscale.com/kb/1112/userspace-networking)); ~56 MB RSS in a 2 GB CT |
| Caddy as TLS edge on pve, bound to `100.68.196.99` | HTTP/3 (Caddy's default protocols are `h1 h2 h3`), one TLS/compression layer | Certificate plumbing: `tailscale cert` files are renewed manually ([KB 1153](https://tailscale.com/kb/1153/enabling-https)); or the `caddy-tailscale`/tsnet plugin, which is "still very experimental" ([README](https://github.com/tailscale/caddy-tailscale)) |
| Caddy with a `tailscale` listener in CT 200 | Embedded node, automatic ts.net cert, h3 | Another experimental dependency and node identity; userspace performance |
| Plain LAN HTTP | Simplest and fastest | No TLS, no auth, no public certificate for a private IP |

Two traps. Mounting pve's `tailscaled.sock` into CT 200 so Caddy can fetch certificates would hand the container a root-authorized LocalAPI: Tailscale explicitly refuses to proxy to its own socket for that reason (`ErrProxyToTailscaledSocket`, [serve.go:83-88](https://github.com/tailscale/tailscale/blob/v1.104.5/ipn/ipnlocal/serve.go#L83-L88)). And Serve's identity headers (`Tailscale-User-*`) only help for auth if the backend is otherwise unreachable — today it is not.

Caddy's `bind` directive targets a specific address ([docs](https://caddyserver.com/docs/caddyfile/directives/bind)), so a host Caddy could bind only `100.68.196.99`. Its upstream keep-alive default is 2 minutes and idle connections per host default to 32 ([docs](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)), both above this workload's needs.

## 7. The security boundary is not what it looks like

`docker ps` [M] shows Caddy publishing `0.0.0.0:8448->8448/tcp` and `[::]:8448`. A plaintext `curl http://10.10.10.20:8448/` succeeds from pve [M]. Any device that can route to 10.10.10.20 can therefore read the dashboard and its API without Tailscale, TLS, or a tailnet ACL [I]. That is the largest gap in the current setup, and it is orthogonal to which TLS edge you choose.

## What this means for the dashboard

- **Keep `tailscale serve`.** At 27.6 KB of assets and a 10-second, 383-byte poll it is not the bottleneck, and it eliminates certificate operations. A sidecar or Caddy TLS edge would add moving parts for no visible gain.
- **Do not chase HTTP/3 or MTU.** The page is a handful of packets; h3's wins matter for a phone on lossy cellular, not two LAN clients. 1280 is the correct, safe MTU.
- **Fix the real gap first:** if the tailnet is meant to be the only path, restrict CT 200's published 8448 to pve (`10.10.10.1`) or unpublish it. This is independent of Serve and is the only finding here with a security consequence.
- **Leave the backend on HTTP/1.1.** Caddy's h2c support is unused because Serve only uses h2c for gRPC; multiplexing one browser connection onto one upstream connection offers little benefit for this request mix.

## Open questions

- Does the phone connect direct or via DERP when off-LAN? It was offline for this investigation; a client-side `tailscale ping`/`status` would answer it.
- Do the browsers actually negotiate h2 and reuse one connection? The server offers h2 and curl proves it; Chrome/Safari behaviour needs DevTools or a client capture.
- Is `10.10.10.20:8448` intentionally LAN-reachable, or should it be firewalled to pve only?
- Would a host Caddy bound to `100.68.196.99` measurably improve first-load time through h3/0-RTT on a phone, or is the added certificate plumbing unjustified?

## Sources

- Tailscale: [`tailscale serve`](https://tailscale.com/kb/1242/tailscale-serve), [Enabling HTTPS](https://tailscale.com/kb/1153/enabling-https), [DERP servers](https://tailscale.com/kb/1232/derp-servers), [Userspace networking](https://tailscale.com/kb/1112/userspace-networking).
- Tailscale source at v1.104.5: [`serve.go`](https://github.com/tailscale/tailscale/blob/v1.104.5/ipn/ipnlocal/serve.go), [`cert.go`](https://github.com/tailscale/tailscale/blob/v1.104.5/ipn/ipnlocal/cert.go), [`mtu.go`](https://github.com/tailscale/tailscale/blob/v1.104.5/net/tstun/mtu.go).
- Go 1.27.3 `net/http`: [`doc.go`](https://github.com/golang/go/blob/go1.27.3/src/net/http/doc.go), [`server.go`](https://github.com/golang/go/blob/go1.27.3/src/net/http/server.go), [`transport.go`](https://github.com/golang/go/blob/go1.27.3/src/net/http/transport.go).
- Caddy: [`reverse_proxy`](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy), [server `protocols`](https://caddyserver.com/docs/caddyfile/options), [`bind`](https://caddyserver.com/docs/caddyfile/directives/bind).
- [`tailscale/caddy-tailscale`](https://github.com/tailscale/caddy-tailscale).

Measurements were read-only (`tailscale`, `ip`, `ss`, `openssl`, `curl`, `docker`, `ssh`); nothing was changed, restarted, or stopped.
