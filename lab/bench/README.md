# Dashboard benchmark lab

Throwaway A/B lab for the dashboard optimisation map. Built on 10 Nov 2026.

Topology:

```
PVE  runner (Playwright + headless Chromium, user space)
  └─ optional TCP delay/bandwidth proxy on 127.0.0.1:1908x
       └─ CT 200  lab-caddy :18080-18083
            ├─ lab-current  Python 3.13 + production app/static
            ├─ lab-cached   Python 3.13 + ETag/304 + HTTP/1.1, same static
            ├─ lab-lean     lab-cached + static without calendar iframe / Google Fonts
            └─ static       Caddy file_server + ETag/304, API proxied to lab-cached
```

The lab is LAN-only on ports 18080–18083. It is **not** exposed through
`tailscale serve`, has no authentication, and must never be published.

## Variants

| Variant | Server | Static page | Caching |
|---|---|---|---|
| `current` | production `app.py` | calendar iframe + Google Fonts | `no-store`, HTTP/1.0 upstream |
| `cached` | `app_cached.py` | same | weak ETag, `no-cache`, 304, HTTP/1.1 keep-alive, gzip 9 |
| `lean` | `app_cached.py` | no calendar, no Google Fonts, system stack | same |
| `static` | Caddy `file_server` + API proxy | lean | Caddy ETag/304, gzip 9 |

## Profiles

`direct`, `lan5` (2.5 ms one-way), `remote40` (20 ms one-way), `mobile100`
(50 ms one-way + 1500 kbps). The proxy is a user-space TCP approximation, not
`tc netem`; treat the profiles as synthetic.

## Run

```bash
# on docker01
cd /opt/lab && docker compose -f compose.yml up -d

# on PVE
cd /home/hermes/lab/dash-bench
~/.hermes/venvs/cron/bin/python runner/bench.py --iterations 6
~/.hermes/venvs/cron/bin/python runner/report.py
```

Control run with third-party requests blocked (cold rows only; interception
interferes with the HTTP cache):

```bash
~/.hermes/venvs/cron/bin/python runner/bench.py \
  --iterations 4 --variants current,cached,lean --profiles direct \
  --block-third-party --out runs-blocked.jsonl
```

## Results

`results/report.md`, `results/summary.csv`, `results/runs.jsonl`,
`results/runs-blocked.jsonl`.

Headline: caching cuts warm bytes ~84% (10.7 KB → 1.7 KB); dropping the
calendar iframe cuts cold load from ~1.2 s to ~44 ms in the 401 case and is
much larger in the signed-in case measured by the third-party research. Server
shape (Caddy file_server vs Python static) is within noise. Headless Chromium
has no GPU/battery signal, so render-cost claims still need a client pass.

## Cleanup

```bash
ssh docker01 'cd /opt/lab && docker compose -f compose.yml down -v'
```

Do **not** remove the `caddy:2-alpine` / `python:3.13-alpine` images: the
production stack uses the same tags. Remove `/opt/lab` only after the results
have been copied.
