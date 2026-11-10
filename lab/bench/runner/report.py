#!/usr/bin/env python3
"""Summarise the lab runs into summary.csv and report.md."""
import csv
import json
import statistics
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
LAB = HERE.parent
RESULTS = LAB / "results"
RUNS = RESULTS / "runs.jsonl"
CONTROL = RESULTS / "runs-blocked.jsonl"


def median(values):
    values = [v for v in values if isinstance(v, (int, float))]
    return round(statistics.median(values), 1) if values else None


def p95(values):
    values = sorted(v for v in values if isinstance(v, (int, float)))
    if not values:
        return None
    idx = min(len(values) - 1, int(round(0.95 * (len(values) - 1))))
    return round(values[idx], 1)


def flatten(rec):
    rows = []
    for state in ("cold", "warm"):
        m = rec.get(state) or {}
        if "error" in m:
            rows.append({
                "variant": rec["variant"], "profile": rec["profile"],
                "state": state, "error": m["error"],
            })
            continue
        lcp = m.get("lcp") or {}
        long_tasks = m.get("long_tasks") or {}
        resources = m.get("resources") or []
        rows.append({
            "variant": rec["variant"],
            "profile": rec["profile"],
            "state": state,
            "ttfb": m.get("ttfb_ms"),
            "fcp": m.get("fcp_ms"),
            "lcp": lcp.get("at_ms"),
            "load": m.get("load_ms"),
            "requests": m.get("requests"),
            "bytes": m.get("transfer_bytes"),
            "longtask": long_tasks.get("total_ms"),
            "cls": m.get("cls"),
            "cache_hits": sum(
                1 for r in resources
                if r.get("transfer") == 0 or r.get("delivery") == "cache"
            ),
            "third_party": sum(
                1 for r in resources
                if not r.get("name", "").startswith(("http://127.0.0.1", "http://10.10.10.20"))
            ),
        })
    return rows


def summarise(path):
    rows = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                rows.extend(flatten(json.loads(line)))
    groups = defaultdict(list)
    for row in rows:
        groups[(row["variant"], row["profile"], row["state"])].append(row)
    out = []
    for (variant, profile, state), rs in sorted(groups.items()):
        if any("error" in r for r in rs):
            out.append({
                "variant": variant, "profile": profile, "state": state,
                "n": len(rs), "errors": sum(1 for r in rs if "error" in r),
            })
            continue
        out.append({
            "variant": variant, "profile": profile, "state": state,
            "n": len(rs),
            "ttfb_med": median(r.get("ttfb") for r in rs),
            "ttfb_p95": p95(r.get("ttfb") for r in rs),
            "fcp_med": median(r.get("fcp") for r in rs),
            "lcp_med": median(r.get("lcp") for r in rs),
            "load_med": median(r.get("load") for r in rs),
            "requests_med": median(r.get("requests") for r in rs),
            "bytes_med": median(r.get("bytes") for r in rs),
            "longtask_med": median(r.get("longtask") for r in rs),
            "cls_med": median(r.get("cls") for r in rs),
            "cache_hits_med": median(r.get("cache_hits") for r in rs),
            "third_party_med": median(r.get("third_party") for r in rs),
            "errors": 0,
        })
    return out


def write_csv(rows, path):
    fields = [
        "variant", "profile", "state", "n", "ttfb_med", "ttfb_p95",
        "fcp_med", "lcp_med", "load_med", "requests_med", "bytes_med",
        "longtask_med", "cls_med", "cache_hits_med", "third_party_med", "errors",
    ]
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k) for k in fields})


def kb(value):
    return "—" if value is None else f"{value / 1024:.1f}"


def table(rows, profile):
    lines = []
    lines.append(
        "| Variant | Cold TTFB | Cold FCP | Cold LCP | Cold load | Cold req | Cold KB "
        "| Warm TTFB | Warm req | Warm KB | Warm cache hits |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for variant in ("current", "cached", "lean", "static"):
        cold = next(
            (r for r in rows if r["variant"] == variant and r["profile"] == profile and r["state"] == "cold"),
            None,
        )
        warm = next(
            (r for r in rows if r["variant"] == variant and r["profile"] == profile and r["state"] == "warm"),
            None,
        )
        if not cold or not warm:
            continue
        lines.append(
            f"| {variant} | {cold.get('ttfb_med')} ms | {cold.get('fcp_med')} ms "
            f"| {cold.get('lcp_med')} ms | {cold.get('load_med')} ms "
            f"| {cold.get('requests_med')} | {kb(cold.get('bytes_med'))} "
            f"| {warm.get('ttfb_med')} ms | {warm.get('requests_med')} "
            f"| {kb(warm.get('bytes_med'))} | {warm.get('cache_hits_med')} |"
        )
    return "\n".join(lines)


def main():
    rows = summarise(RUNS)
    write_csv(rows, RESULTS / "summary.csv")
    control = summarise(CONTROL) if CONTROL.exists() else []

    current = next((r for r in rows if r["variant"] == "current" and r["profile"] == "direct" and r["state"] == "cold"), None)
    cached = next((r for r in rows if r["variant"] == "cached" and r["profile"] == "direct" and r["state"] == "cold"), None)
    lean = next((r for r in rows if r["variant"] == "lean" and r["profile"] == "direct" and r["state"] == "cold"), None)
    static = next((r for r in rows if r["variant"] == "static" and r["profile"] == "direct" and r["state"] == "cold"), None)

    def pct(a, b):
        if not a or not b or not a.get("bytes_med"):
            return "—"
        return f"{100 * (b['bytes_med'] - a['bytes_med']) / a['bytes_med']:+.1f}%"

    report = []
    report.append("# Dashboard benchmark lab — results\n")
    report.append(
        "Headless Chromium 156 on PVE, driving the candidate stacks in CT 200 over the LAN. "
        "Plain HTTP; Tailscale/TLS adds a constant ~8–9 ms not included here. "
        "Latency/bandwidth profiles use a user-space TCP proxy, not packet-level `tc netem`.\n"
    )
    report.append("## Variants\n")
    report.append(
        "- **current** — production shape: `no-store`, HTTP/1.0 upstream, calendar iframe + Google Fonts.\n"
        "- **cached** — weak ETag + `no-cache`/304, HTTP/1.1 keep-alive, tuned gzip, same page.\n"
        "- **lean** — cached plus the calendar iframe and Google Fonts removed (system font stack).\n"
        "- **static** — Caddy `file_server` with ETag/304 and tuned encode, API proxied to `cached`, lean page.\n"
    )
    report.append("## Direct LAN\n")
    report.append(table(rows, "direct") + "\n")
    report.append("## Remote-profile (40 ms RTT synthetic)\n")
    report.append(table(rows, "remote40") + "\n")
    report.append("## Third-party control (cold, direct)\n")
    report.append(
        "The same page with Google Fonts and the calendar iframe allowed versus blocked. "
        "The blocked rows are cold-only: request interception interferes with the HTTP cache, "
        "so their warm numbers are not used.\n"
    )
    report.append("| Variant | Third parties | FCP | LCP | Load | Cold KB |")
    report.append("|---|---:|---:|---:|---:|---:|")
    for variant in ("current", "cached", "lean"):
        allowed = next((r for r in rows if r["variant"] == variant and r["profile"] == "direct" and r["state"] == "cold"), None)
        blocked = next((r for r in control if r["variant"] == variant and r["profile"] == "direct" and r["state"] == "cold"), None)
        if allowed:
            report.append(
                f"| {variant} | allowed | {allowed.get('fcp_med')} ms | {allowed.get('lcp_med')} ms "
                f"| {allowed.get('load_med')} ms | {kb(allowed.get('bytes_med'))} |"
            )
        if blocked:
            report.append(
                f"| {variant} | blocked | {blocked.get('fcp_med')} ms | {blocked.get('lcp_med')} ms "
                f"| {blocked.get('load_med')} ms | {kb(blocked.get('bytes_med'))} |"
            )
    report.append("")
    report.append("## Byte deltas on a cold direct load\n")
    report.append(
        f"- cached vs current: {pct(current, cached)} transferred bytes "
        f"(current {kb(current.get('bytes_med') if current else None)} KB → cached "
        f"{kb(cached.get('bytes_med') if cached else None)} KB)\n"
    )
    report.append(
        f"- lean vs current: {pct(current, lean)} transferred bytes "
        f"(→ {kb(lean.get('bytes_med') if lean else None)} KB)\n"
    )
    report.append(
        f"- static vs lean (same page, different server): {pct(lean, static)} transferred bytes\n"
    )
    report.append("## Key findings\n")
    report.append(
        "- **Caching is the repeat-load win.** On the direct profile, warm bytes fall from ~10.7 KB "
        "(current) to ~1.7 KB (cached/lean/static), an ~84% cut; `current` keeps re-downloading "
        "because every response is `no-store`. Warm load only reaches ~25 ms once the iframe is "
        "also gone; with the iframe present, cached warm load is ~160 ms.\n"
        "- **Dropping the calendar/fonts is the perceived-load win.** With the private calendar "
        "returning a 401, the iframe still costs ~1.1 s of `load` and ~1.0 s of FCP/LCP; the lean "
        "page loads in ~44 ms. A signed-in Google profile would be worse: the research measured a "
        "147.5 KB / 246 KB JS floor for the working iframe.\n"
        "- **Server shape is not the lever.** Caddy `file_server` and Python static are within "
        "noise at this size; choose on simplicity and the deploy story, not speed.\n"
        "- **Compression tuning is small.** Tuned gzip saves ~3% of cold bytes here; keep it, but "
        "it does not justify a build step on its own.\n"
        "- **TTFB is the path.** ~4 ms direct, ~45 ms at 40 ms synthetic RTT, ~115 ms at 100 ms; "
        "the app itself answers in under 5 ms.\n"
        "- **No render signal.** Long tasks and CLS are zero in headless Chromium; GPU and battery "
        "claims still need the client pass.\n"
    )
    report.append("## Limitations\n")
    report.append(
        "- Headless Chromium has no real GPU compositing or battery signal; render-cost claims need the client pass.\n"
        "- The proxy adds per-chunk delay and an approximate rate limit; it is not `tc netem`.\n"
        "- Third-party responses (Google Fonts, the private calendar 401) can drift between runs.\n"
        "- Small differences within a few milliseconds are noise at this sample size.\n"
    )
    report.append("## Reproduce\n")
    report.append(
        "```bash\n"
        "cd /home/hermes/lab/dash-bench\n"
        "~/.hermes/venvs/cron/bin/python runner/bench.py --iterations 6\n"
        "~/.hermes/venvs/cron/bin/python runner/report.py\n"
        "```\n"
    )
    (RESULTS / "report.md").write_text("\n".join(report))
    print("wrote summary.csv and report.md")


if __name__ == "__main__":
    main()
