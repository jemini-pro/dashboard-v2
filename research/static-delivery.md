# Static delivery for the dashboard: caching, compression, preload, protocol

Research note, 2026-11-10. Method: read-only inspection of the live stack (`curl` against
`https://pve.tail73491d.ts.net:8448`, `/opt/stack/{dash/app.py,caddy/Caddyfile}`) plus primary
sources (RFCs, Caddy docs and v2.11.9 source, CPython 3.13.18 source). Labels: **(measured)** =
observed here; **(source/docs)** = documented or visible in source; **(inference)** = reasoned.

## What the stack does today

Compressed sizes served by Caddy, in bytes **(measured)**:

| Asset | Raw | gzip | zstd |
|---|---:|---:|---:|
| `/` (index.html) | 2,819 | 1,078 | 1,136 |
| `/app.js` | 12,184 | 3,830 | 3,986 |
| `/style.css` | 12,614 | 3,137 | 3,366 |
| `/api/stats` | 978 | 373 | 385 |
| `/api/bookmarks` | 3,888 | 1,186 | 1,273 |
| **Total** | **32,483** | **9,604** | **10,146** |

Every response carries `Cache-Control: no-store`, no `ETag`, no `Last-Modified`, and
`Vary: Accept-Encoding` (added by Caddy). The edge negotiates **HTTP/2**, while `Via: 1.0 Caddy`
shows the Python server answering **HTTP/1.0**; a conditional request returns `200` and the full
body **(measured)**. zstd-capable clients get the *larger* variant because Caddy prefers zstd
by default **(measured + [docs](https://caddyserver.com/docs/caddyfile/directives/encode))**.

The repo tracks only the three static files; `app.py` and the Caddyfile live only under
`/opt/stack` and are untracked **(measured)**.

## Caching is the dominant lever

`no-store` is not "revalidate"; it forbids storing at all. RFC 9111 §5.2.2.5: a cache "MUST NOT
intentionally store" the response, so every navigation re-downloads the full ~10 KB **(docs)**.
`no-cache` is the correct directive for revalidatable content: it may be stored but "MUST NOT be
used ... without forwarding it for validation" (§5.2.2.4) **(docs)**; MDN draws the same
distinction ([MDN Cache-Control](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Cache-Control)).

Adding validators makes that revalidation cheap. RFC 9110 §8.8.3.1 says an origin server
"SHOULD send an ETag" when change detection is practical, and allows "a modification timestamp
that has sub-second resolution" as the basis **(docs)**. A `304` must carry `ETag`, `Vary`, and
`Cache-Control` as a `200` would, and "cannot contain content or trailers" (§15.4.5);
`If-None-Match` uses **weak comparison** (§13.1.2) **(docs)**. For files this is ~20 lines in
`Handler`: stat the file, emit `ETag: W/"<mtime_ns>-<size>"`, compare `If-None-Match` (or
`If-Modified-Since`), send a bodyless 304. `st_mtime_ns` is enough; hashing is unnecessary.

One Caddy interaction matters. `encode` rewrites **strong** ETags per variant
(`"abc"` → `"abc-zstd"`) per RFC 9110 §8.8.3.3, and strips that suffix from `If-None-Match`
before forwarding upstream so the origin can still match
([encode.go](https://github.com/caddyserver/caddy/blob/v2.11.9/modules/caddyhttp/encode/encode.go)) **(source)**.
**Weak** ETags (`W/"..."`) are left untouched. Use a weak ETag: it is legal here, weak
comparison is used anyway, and it avoids the suffix round-trip (including when a cached gzip tag
meets a zstd negotiation) **(source + inference)**.

Freshness is the second half. `no-cache` still costs a round trip per asset per navigation.
`max-age=N` lets the browser reuse without any request; `immutable` also suppresses reload
revalidation, but RFC 8246 §1–2 scopes it to content republished under new URLs **(docs)**. So
`max-age=31536000, immutable` needs fingerprinted/versioned URLs (a build step or templating).
Otherwise `max-age=300` + ETag is a reasonable compromise for statics, and `no-cache` + ETag
for `index.html` and `/api/bookmarks`; `/api/stats` should stay `no-store`.

Two client-side details undercut any server policy. `app.js` fetches `/api/bookmarks` and
`/api/stats` with `{cache: 'no-store'}` (`app.js:208,267`, **measured**), so bookmarks keeps
bypassing the cache until that option is removed. And a 304 is not free: one round trip plus a
few hundred header bytes. With `max-age` on statics, a repeat navigation drops from ~10 KB and
five requests to roughly one HTML revalidation plus the API calls **(inference)**.

## Compression: already on, but mistuned

Caddy's `encode` defaults are zstd (preferred) and gzip, `minimum_length 512`, gzip level **5**,
zstd ≈ level 3 **(docs + [gzip.go](https://github.com/caddyserver/caddy/blob/v2.11.9/modules/caddyhttp/encode/gzip/gzip.go))**. At
those defaults **gzip is ~5% smaller than zstd** on this corpus **(measured)** — zstd's default
level is weak on 12 KB inputs. Raising levels is config-only: `encode { zstd better; gzip 9 }`
should land near 7,641 bytes for the three statics versus 8,488 today (~10% smaller); `best` ≈ 7,593
**(measured with the `zstd` CLI; `better` is documented as zstd level 7–8 at 2–3× default CPU by
[klauspost/compress](https://github.com/klauspost/compress/blob/master/zstd/encoder_options.go))**. Cost is milliseconds —
20 runs of `zstd -11` over `app.js` took 0.05 s **(measured)**.

Precompressed sidecars are *not* available through `reverse_proxy`. Only `file_server` has
`precompressed`, and it requires `.br`/`.zst`/`.gz` files to exist — a **build step**
([file_server docs](https://caddyserver.com/docs/caddyfile/directives/file_server)) **(docs)**. The stock 2.11.9 image does
register `http.precompressed.br` alongside gzip/zstd, though dynamic brotli is not in the
standard build (only `http.encoders.gzip`/`.zstd`) **(measured via `caddy list-modules`)**.
Payoff is small: static `zstd -19` totals 7,616 bytes versus 7,788 for `gzip -9` **(measured)**
— ~2% for a build pipeline. Brotli sidecars might add a single-digit percentage more
**(inference)**. Not worth it at 28 KB.

`Vary: Accept-Encoding` needs no work: Caddy emits it even for identity responses and on 304s
([encode.go](https://github.com/caddyserver/caddy/blob/v2.11.9/modules/caddyhttp/encode/encode.go)) **(source)**. Compressed
bodies larger than roughly 2 KB lose `Content-Length` **(measured)**; irrelevant under HTTP/2.

## Protocol and keep-alive

The edge is HTTP/2 **(measured)**, so the browser already multiplexes all five requests over one
connection. The weak link is Caddy→Python. `BaseHTTPRequestHandler.protocol_version` defaults to
`"HTTP/1.0"`, and keep-alive is enabled only at `HTTP/1.1` or above
([server.py](https://github.com/python/cpython/blob/v3.13.18/Lib/http/server.py), lines 321–322, 649)
**(source)**. That is why `Via: 1.0 Caddy` appears and why Caddy opens a fresh TCP connection per
proxied request. Setting `protocol_version = "HTTP/1.1"` is one line: the handler already sends
`Content-Length` on every path, and `send_error` sends `Connection: close` **(source)**, so
persistence is safe. Caddy's transport defaults to 1.1/2 with a 2-minute keepalive
([reverse_proxy docs](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)) and will pool connections **(docs)**.
The saving is sub-millisecond connects per load, not a headline.

**h2c upstream is not available without a rewrite.** Caddy supports `versions h2c`
**(docs)**, but CPython's server explicitly rejects HTTP/2 requests with `505`
(`version_number >= (2, 0)`, server.py:320–327) **(source)**. The gain over HTTP/1.1 keep-alive
for five small sequential responses is negligible; a rewrite (or putting a static file server in
front) is not justified by protocol alone.

## Preload

Don't. MDN notes that preloading CSS/JS that is already present in the same HTML chunk is
"trivial" because the browser's preload scanner discovers it anyway
([MDN rel=preload](https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Attributes/rel/preload)) **(docs)**.
`index.html` is 2.8 KB, CSS is in `<head>`, and `app.js` is at the end of `<body>` — all discovered
during the same parse **(measured)**. The real cold path is elsewhere: the render-blocking Google
Fonts stylesheet (8,933 bytes raw / 635 gzip, two families) and the `calendar.google.com` iframe
are third-party requests that dwarf 28 KB **(measured for the CSS; inference for the iframe)**.

## Ranked changes

| # | Change | Expected impact | Build step? |
|---|---|---|---|
| 1 | Replace `no-store` with `no-cache` + weak ETag + 304 on `index.html`, statics, `/api/bookmarks` | Repeat nav ~10 KB → ~1 KB of 304s; makes the browser cache usable at all | No |
| 2 | Add freshness: `max-age=300` + ETag on statics (or hashed URLs + `max-age=31536000, immutable`) | No static requests on repeat loads; immutable needs versioned URLs | No for max-age; **yes** for hashed/immutable |
| 3 | Remove `cache: 'no-store'` from the two `fetch` calls in `app.js` | Otherwise #1 does nothing for bookmarks | No |
| 4 | Tune compression: `encode { zstd better; gzip 9 }` | ~8–10% off cold bytes (8,488 → 7,641 static bytes) | No |
| 5 | `protocol_version = "HTTP/1.1"` in `Handler` | Removes one TCP connect per proxied request; Caddy pools upstream connections | No |
| 6 | Serve statics from Caddy `file_server` with `precompressed br zstd gzip` | Free ETag/304 for statics, plus brotli sidecars; ~3–10% over tuned dynamic | **Yes** (sidecars) + bind mount |
| 7 | `<link rel="preload">` | No measurable gain for assets already in the HTML | No |

A config-only stopgap: Caddy's `header` directive with `>` can override upstream `Cache-Control`
after proxying ([header docs](https://caddyserver.com/docs/caddyfile/directives/header)), e.g.
`header /app.js >Cache-Control "public, max-age=300"`. It buys repeat-load freshness without
touching `app.py`, but cannot produce validators or 304s.

## What this means for the dashboard

The 28 KB is already compressed and delivered over HTTP/2; further protocol or preload work is
rounding error. The outlier is caching: `no-store` guarantees a full re-download on every visit,
and the fix is small, stdlib-only, and needs no build. Do #1, #3, #4, and #5 together — all are
single-line-to-30-line changes — and defer #2's immutable variant until there is a deploy pipeline
that can fingerprint filenames. Do not build a brotli/precompressed pipeline for ~1 KB. If cold
*first paint* is the real complaint, profile the Google Fonts stylesheet and calendar iframe
before touching the 28 KB of first-party assets.

## Open questions

- Is the dashboard opened repeatedly or left open for hours? That decides between `no-cache`
  (always correct) and a longer `max-age` (faster, briefly stale after deploys).
- Where would `app.py` changes live? It is untracked, so server-side work needs a versioned home.
- Is Chrome the only client? `zstd better` wins once levels are raised, but other clients may not
  advertise zstd.
- How much of cold load is the calendar iframe and font files? A DevTools trace would say whether
  the remaining wins are even in scope.
