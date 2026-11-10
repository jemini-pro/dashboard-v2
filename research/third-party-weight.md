# Third-party weight: the Google Fonts link and the Google Calendar embed

Measured 2026-11-10 from a Tailscale host with `curl` against the URLs in `index.html`. No browser was available, so paint timing, main-thread time, and client cache state are **needs client**.

## What the font link costs

The page requests one stylesheet from `fonts.googleapis.com` and preconnects to both Google font origins.

| Item | Transferred | Notes |
|---|---|---|
| Font CSS (Outfit + Space Grotesk) | 640 B compressed / 8,933 B raw | `private, max-age=86400` |
| Outfit latin WOFF2 | **0 B** | Never downloaded: no rule uses Outfit |
| Space Grotesk latin WOFF2 | 22,288 B | One variable file serves all five weights; latin-ext would add 18,940 B |
| **First load** | **22,928 B, 2 requests, 2 origins** | Both origins preconnected |

Outfit is dead CSS, not dead bytes: `@font-face` rules download only when a style uses the family [S3]. Cost: ~92 compressed bytes and six unused rules. Space Grotesk is referenced only through `--font-family`, and all five weights resolve to one 22,288 B variable file.

The font file caches for a year; the CSS for a day. The real cost is structural: a cross-origin stylesheet is render-blocking, and CSS blocks first paint until the CSSOM is complete [S5]. `display=swap` only governs what happens after the CSS arrives: a 0 ms block period, then fallback text [S3]. If `fonts.googleapis.com` is slow or blocked, nothing paints until that request resolves. A font-file failure after that leaves `sans-serif` on screen with different platform metrics. Preconnect already hides most connection setup (100–500 ms per origin [S4]), so the exposure is the serial CSS request and the third-party dependency.

## What the calendar iframe costs

The iframe points at a group calendar in `AGENDA` mode. Its public ICS address returns **404**, while a known public Google calendar returns 200, so this calendar is not publicly shared [S9]. An unauthenticated embed request returns **HTTP 401, 9,251 B** with the service's own interstitial: "Allow Google Calendar access to your necessary cookies" and "Sign in to your Google Account". The response sets a `SameSite=None` `NID` cookie. It works only with a Google session and that cookie.

Public-calendar first-load floor:

| Resource | Raw | Transferred | Caching |
|---|---|---|---|
| Embed HTML shell | 70,977 B | 13,724 B | `no-store`, every load |
| Main JS (`boq-calendar.CalendarEmbed`) | 246,462 B | 88,024 B | `immutable`, 1 year |
| Main CSS | 313,082 B | 45,793 B | `immutable`, 1 year |
| **First-load floor** | **630 KB** | **147,541 B** | |
| `apis.google.com/js/api.js` | — | 5,867 B | extra request |

The shell references `www.gstatic.com`, `apis.google.com`, `clients6.google.com`, and a favicon: at least four origins and ~8 requests before event data. True request counts need Chrome DevTools. On repeat loads the bundles are cached, but the 71 KB shell is re-fetched and 246 KB of JS still executes.

MDN notes that every iframe is a complete document environment that "requires increased memory and other computing resources", and that its `load` event "is always triggered even if the content fails to load" [S6]. A blank or 401 frame is invisible to the parent. Chrome allows third-party cookies by default, but users, enterprise policy, or browser design can block them [S12].

## Font alternatives

**Self-host a subset.** Both families are SIL OFL 1.1, which permits embedding and redistribution if the notice and licence accompany the files; modified versions (subsetting counts) must not use a Reserved Font Name [S14][S15]. Neither declares one. Self-hosting removes both Google origins, both preconnects, and the render-blocking request. Google's `text=` pipeline yields a 14,664 B variable WOFF2 for the dashboard's 97-glyph alphabet (printable ASCII plus em dash and ellipsis), versus 22,288 B for full latin: 34% smaller, not the "up to 90%" advertised for static fonts [S1]. A single 400-weight latin file is 13,388 B; since the design uses five weights, keep the variable file.

**Keep Google Fonts, shrink the request.** Use `wght@300..700` instead of five discrete weights: same font file, 3 `@font-face` rules instead of 15, 469 B instead of 548 B of CSS. Add `&text=` with the glyph set: 360 B CSS plus 14,664 B. It keeps the dependency and blocking stylesheet.

**System stack.** `system-ui, -apple-system, "Segoe UI", Roboto, sans-serif` costs nothing and cannot fail offline. MDN defines `system-ui` as "the default user interface font on a given platform" and warns that its appearance is platform-dependent [S13]. Here that means Segoe UI on Windows and SF Pro on macOS: different metrics and brand.

| Option | Transfer | Requests / origins | Failure mode | Effort |
|---|---|---|---|---|
| Current Google Fonts link | 22.9 KB | 2 / 2, preconnected | Blocks first paint if Google is unreachable | None |
| Google `text=` subset | 15.0 KB | 2 / 2 | Same dependency | One line |
| Self-hosted subset | 14.7 KB (22.3 KB full latin) | 1 / 0 new | Local file only | Commit WOFF2 + OFL.txt |
| System stack | 0 B | 0 / 0 | None; visual change | CSS edit |

## Calendar alternatives

**Keep the iframe.** No implementation cost, but the 148 KB floor, four-plus origins, 246 KB of JavaScript, cookie/session dependency, and silent failure remain. The private calendar cannot render for a signed-out or cookie-restricted browser.

**Server-side ICS plus local render.** Google exposes a read-only "Secret address in iCal format" under Calendar Settings → *Settings for my calendars* → calendar → *Integrate calendar* [S8]. Only the owner should know it and it can be reset, so treat it as a bearer credential in the server's `.env`, never the frontend [S8]. The public equivalent "only works if the calendar is public" [S9], which this one is not. The ICS endpoint sends **no `Access-Control-Allow-Origin`** and **`X-Frame-Options: SAMEORIGIN`**, so the browser can neither fetch nor frame it; retrieval must be server-side. A full public year of 244 events measured 95,699 B raw / **9,869 B gzipped**, `Cache-Control: no-store`; a week of meetings is far smaller. Poll every 5–15 minutes, cache the parsed agenda, and serve `/api/agenda` same-origin. RFC 5545 defines `VEVENT`, `DTSTART`/`DTEND`, `RRULE`, and `VTIMEZONE`, so recurrence and DST need a real parser [S10]. ical.js parses RFC 5545 and iterates recurrences, is MPL-2.0, and its minified build is 77,698 B raw / about 20 KB transferred [S16]. Python's `icalendar` is BSD-2-Clause but pulls `python-dateutil` and `tzdata` [S17]; the dashboard server is deliberately stdlib-only ("no pip, no build step") [S18], so client-side ical.js or a vendored parser fits better. The Calendar API needs OAuth for private data [S11][S19] — far more machinery than a secret ICS URL.

**Drop it.** The bookmarks grid already has a Google Calendar link (`app.js`), so removing the iframe costs one glance.

| Option | Transfer | Requests / origins | Main-thread | Failure mode | Effort |
|---|---|---|---|---|---|
| Iframe | 148 KB cold, 14 KB shell every load | 8+ / 4+ | 246 KB JS | 401/cookie/sign-in, blank frame | None |
| Server ICS + local render | ~10 KB gzip per full-year feed | 1 / local server | ~20 KB parser | Stale cache with timestamp | Medium |
| Drop | 0 | 0 | 0 | None; bookmark remains | Trivial |

## What this means for the dashboard

The premise that fonts dominate load time is half right. Measured font weight is **22.9 KB, two requests, both origins preconnected** — trivial beside the calendar's **148 KB first-load floor**, 4+ origins, and 246 KB of JavaScript. Outfit is loaded but never used, so its 32 KB file is never fetched; deleting it is housekeeping, not a win.

The font link's real cost is a third-party stylesheet in the render-blocking path; its failure mode is a stalled first paint, not a missing font. Self-hosting one 14.7 KB subset removes that dependency and both preconnects, and is licence-clean; the system stack removes it entirely at the cost of a different look on Windows and macOS.

The calendar is the larger, more fragile dependency. Its embed needs a Google session and a third-party cookie for this non-public calendar, and the parent cannot detect a failed frame. Server-side ICS with a cached agenda is the only alternative that keeps the feature without a browser session; missing CORS and `SAMEORIGIN` framing make server-side retrieval mandatory.

## Open questions

- Is the group calendar intentionally non-public, and does the always-on Chrome profile keep a Google session and third-party cookies across reboots and updates?
- What are FCP/LCP, long tasks and request counts on JEM-PC and the MacBook, cold?
- How large is this calendar's secret ICS, and does it emit `RRULE` or expanded instances?
- Can the dashboard container make outbound HTTPS to `calendar.google.com`? Proxmox access is firewalled, so verify egress.
- Is vendoring ical.js (MPL-2.0, ~20 KB) acceptable, or is a smaller RRULE subset worth the correctness risk?
- Are accented names expected (needs Latin-1/latin-ext)?

## Sources

- [S1] Google Fonts CSS2 API — https://developers.google.com/fonts/docs/css2
- [S2] Google Fonts technical considerations — https://developers.google.com/fonts/docs/technical_considerations
- [S3] web.dev, Best practices for fonts — https://web.dev/articles/font-best-practices
- [S4] web.dev, Preconnect and dns-prefetch — https://web.dev/articles/preconnect-and-dns-prefetch
- [S5] MDN, Critical rendering path — https://developer.mozilla.org/en-US/docs/Web/Performance/Critical_rendering_path
- [S6] MDN, `<iframe>` — https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/iframe
- [S7] Google Calendar Help, Embed a calendar — https://support.google.com/calendar/answer/41207
- [S8] Google Calendar Help, Secret address in iCal format — https://support.google.com/calendar/answer/37648
- [S9] Google Calendar Help, Public address in iCal format — https://support.google.com/calendar/answer/37083
- [S10] RFC 5545, iCalendar — https://datatracker.ietf.org/doc/html/rfc5545
- [S11] Google Cloud, API keys — https://docs.cloud.google.com/docs/authentication/api-keys
- [S12] Privacy Sandbox, Third-party cookie restrictions (updated 2026-10-26) — https://privacysandbox.google.com/3pcd
- [S13] MDN, `font-family` / `system-ui` — https://developer.mozilla.org/en-US/docs/Web/CSS/font-family
- [S14] Space Grotesk OFL 1.1 — https://github.com/google/fonts/blob/main/ofl/spacegrotesk/OFL.txt
- [S15] Outfit OFL 1.1 — https://github.com/google/fonts/blob/main/ofl/outfit/OFL.txt
- [S16] ical.js (MPL-2.0) — https://github.com/kewisch/ical.js
- [S17] icalendar for Python (BSD-2-Clause) — https://github.com/collective/icalendar
- [S18] Dashboard server, stdlib-only — `origin/vendor-runtime:server/app.py`
- [S19] Google Calendar API `events.list` — https://developers.google.com/calendar/api/v3/reference/events/list
