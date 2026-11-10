#!/usr/bin/env python3
"""Headless Chromium benchmark for the dashboard lab.

Runs on PVE. Drives the candidate stacks in CT 200 over the LAN, optionally
through a user-space latency/bandwidth proxy. Third-party requests are allowed
by default so the calendar iframe and Google Fonts cost is measured; pass
--block-third-party for a controlled delivery-only comparison.

    ~/.hermes/venvs/cron/bin/python runner/bench.py --iterations 6
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
LAB = HERE.parent
RESULTS = LAB / "results"

VARIANTS = {"current": 18080, "cached": 18081, "lean": 18082, "static": 18083}
PROFILES = {
    "direct": {"delay_ms": 0, "rate_kbps": 0},
    "lan5": {"delay_ms": 2.5, "rate_kbps": 0},
    "remote40": {"delay_ms": 20, "rate_kbps": 0},
    "mobile100": {"delay_ms": 50, "rate_kbps": 1500},
}
PROXY_BASE = 19080
TARGET_HOST = "10.10.10.20"
THIRD_PARTY = ("google.com", "gstatic.com", "googleapis.com")


def start_proxy(listen, target_port, delay_ms, rate_kbps):
    cmd = [
        sys.executable,
        str(LAB / "proxy" / "proxy.py"),
        "--listen", str(listen),
        "--target-host", TARGET_HOST,
        "--target-port", str(target_port),
        "--delay-ms", str(delay_ms),
        "--rate-kbps", str(rate_kbps),
    ]
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def make_blocker():
    def handler(route):
        if any(host in route.request.url for host in THIRD_PARTY):
            route.abort()
        else:
            route.continue_()

    return handler


MEASURE = """async () => {
  const nav = performance.getEntriesByType('navigation')[0] || {};
  const lcpP = new Promise(resolve => {
    let last = null;
    try { new PerformanceObserver(l => { for (const e of l.getEntries()) last = e; })
      .observe({ type: 'largest-contentful-paint', buffered: true }); } catch (e) {}
    setTimeout(() => resolve(last ? { at_ms: Math.round(last.startTime), size: last.size } : null), 200);
  });
  const ltP = new Promise(resolve => {
    let c = 0, t = 0, m = 0;
    try { new PerformanceObserver(l => { for (const e of l.getEntries()) { c++; t += e.duration; m = Math.max(m, e.duration); } })
      .observe({ type: 'longtask', buffered: true }); } catch (e) {}
    setTimeout(() => resolve({ count: c, total_ms: Math.round(t), max_ms: Math.round(m) }), 200);
  });
  const clsP = new Promise(resolve => {
    let v = 0;
    try { new PerformanceObserver(l => { for (const e of l.getEntries()) if (!e.hadRecentInput) v += e.value; })
      .observe({ type: 'layout-shift', buffered: true }); } catch (e) {}
    setTimeout(() => resolve(+v.toFixed(4)), 200);
  });
  const [lcp, long_tasks, cls] = await Promise.all([lcpP, ltP, clsP]);
  const resources = performance.getEntriesByType('resource').map(e => ({
    name: e.name, type: e.initiatorType,
    transfer: e.transferSize, encoded: e.encodedBodySize, decoded: e.decodedBodySize,
    proto: e.nextHopProtocol, delivery: e.deliveryType, dur_ms: Math.round(e.duration)
  }));
  const paints = Object.fromEntries(performance.getEntriesByType('paint').map(e => [e.name, Math.round(e.startTime)]));
  const same = resources.filter(r => r.name.startsWith(location.origin));
  return {
    ttfb_ms: Math.round(nav.responseStart - nav.startTime),
    dcl_ms: Math.round(nav.domContentLoadedEventEnd - nav.startTime),
    load_ms: Math.round(nav.loadEventEnd - nav.startTime),
    nav_transfer: nav.transferSize,
    requests: resources.length + 1,
    transfer_bytes: (nav.transferSize || 0) + resources.reduce((a, r) => a + (r.transfer || 0), 0),
    same_origin_requests: same.length,
    fcp_ms: paints['first-contentful-paint'] ?? null,
    lcp, long_tasks, cls,
    resources
  };
}"""


def run_cell(browser, variant, port, profile, proxy_port, iterations, out, block):
    url = (
        f"http://127.0.0.1:{proxy_port}/"
        if proxy_port
        else f"http://{TARGET_HOST}:{port}/"
    )
    for i in range(iterations):
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        if block:
            context.route("**/*", make_blocker())
        page = context.new_page()
        try:
            page.goto(url, wait_until="load", timeout=30000)
            page.wait_for_timeout(1200)
            cold = page.evaluate(MEASURE)
            page.reload(wait_until="load", timeout=30000)
            page.wait_for_timeout(1200)
            warm = page.evaluate(MEASURE)
        except Exception as exc:  # noqa: BLE001
            cold = warm = {"error": f"{type(exc).__name__}: {exc}"}
        finally:
            context.close()
        rec = {
            "variant": variant,
            "profile": profile,
            "iteration": i,
            "ts": time.time(),
            "url": url,
            "third_party_blocked": block,
            "cold": cold,
            "warm": warm,
        }
        out.write(json.dumps(rec) + "\n")
        out.flush()
        c = cold.get("ttfb_ms", "?")
        w = warm.get("ttfb_ms", "?")
        print(
            f"{variant:8s} {profile:10s} {i + 1:2d}/{iterations}  "
            f"cold_ttfb={c} warm_ttfb={w}",
            flush=True,
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=6)
    parser.add_argument("--variants", default=",".join(VARIANTS))
    parser.add_argument("--profiles", default=",".join(PROFILES))
    parser.add_argument("--block-third-party", action="store_true")
    parser.add_argument("--out", default="runs.jsonl")
    args = parser.parse_args()

    RESULTS.mkdir(exist_ok=True)
    out_path = RESULTS / args.out
    with out_path.open("w") as out, sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for variant in args.variants.split(","):
            port = VARIANTS[variant]
            for profile in args.profiles.split(","):
                cfg = PROFILES[profile]
                proxy_proc = None
                proxy_port = None
                if cfg["delay_ms"] or cfg["rate_kbps"]:
                    proxy_port = PROXY_BASE + (port - 18080)
                    proxy_proc = start_proxy(
                        proxy_port, port, cfg["delay_ms"], cfg["rate_kbps"]
                    )
                    time.sleep(0.4)
                try:
                    run_cell(
                        browser, variant, port, profile, proxy_port,
                        args.iterations, out, args.block_third_party,
                    )
                finally:
                    if proxy_proc:
                        proxy_proc.terminate()
                        try:
                            proxy_proc.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            proxy_proc.kill()
        browser.close()
    print(f"wrote {out_path}", flush=True)


if __name__ == "__main__":
    main()
