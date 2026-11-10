"""Stdlib tests for the dashboard API: click hits and decayed bookmark scores.

Run from the repo root:
    python3 -m unittest discover -s server -p 'test_*.py' -v
"""
import json
import re
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import app  # noqa: E402

from http.server import ThreadingHTTPServer  # noqa: E402

APP_JS = Path(__file__).resolve().parent.parent / "app.js"

# A fixed clock keeps decay assertions exact instead of racing wall time.
NOW = 1_700_000_000.0


def frozen_time(ts=NOW):
    """Patch the one clock app.py reads, so scores are deterministic."""
    return mock.patch("time.time", return_value=ts)


def js_number_constant(name):
    source = APP_JS.read_text()
    match = re.search(r"\b" + name + r"\s*=\s*([0-9.]+)", source)
    if not match:
        raise AssertionError("constant not found in app.js: " + name)
    return float(match.group(1))


class QuietHandler(app.Handler):
    def log_message(self, fmt, *args):
        pass


WORK_BOOKMARKS = {
    "work": [
        {
            "category": "Mail",
            "items": [
                {"title": "Gmail", "desc": "Email", "icon": "M", "url": "https://mail.example.com"},
                {"title": "Docs", "desc": "Docs", "icon": "D", "url": "https://docs.example.com"},
            ],
        },
    ],
}


class ApiTestCase(unittest.TestCase):
    """Runs the real handler on an ephemeral port against a temp data dir."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name)
        self._old_data, self._old_hits, self._old_static = app.DATA, app.HITS_FILE, app.STATIC
        app.DATA = self.data
        app.HITS_FILE = self.data / "hits.json"
        self.static = self.data / "static"
        self.static.mkdir()
        (self.static / "index.html").write_text(
            '<script id="bookmarks-data" type="application/json">__BOOKMARKS_JSON__</script>'
        )
        app.STATIC = self.static
        self.write_bookmarks(WORK_BOOKMARKS)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        app.DATA, app.HITS_FILE, app.STATIC = self._old_data, self._old_hits, self._old_static
        self._tmp.cleanup()

    def write_bookmarks(self, store):
        (self.data / "bookmarks.json").write_text(json.dumps(store))

    def post(self, body, path="/api/hit"):
        req = urllib.request.Request(self.base + path, data=body, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def post_hit(self, url):
        return self.post(json.dumps({"url": url}).encode())

    def get_json(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as resp:
            return resp.status, json.loads(resp.read())

    def score(self, url):
        _, data = self.get_json("/api/bookmarks")
        for category in data["work"]:
            for item in category["items"]:
                if item["url"] == url:
                    return item["score"]
        self.fail("url not found in bookmark payload: " + url)

    def stored_hits(self):
        return json.loads((self.data / "hits.json").read_text())


class HitEndpointTest(ApiTestCase):
    def test_post_hit_returns_204_and_stores_a_timestamp(self):
        with frozen_time():
            status, body = self.post_hit("https://mail.example.com")

        self.assertEqual(status, 204)
        self.assertEqual(body, b"")
        self.assertEqual(self.stored_hits()["https://mail.example.com"], [NOW])

    def write_hits(self, hits):
        (self.data / "hits.json").write_text(json.dumps(hits))

    def test_never_clicked_link_scores_zero(self):
        with frozen_time():
            score = self.score("https://docs.example.com")
        self.assertEqual(score, 0)

    def test_a_single_fresh_hit_scores_1_0_and_clears_the_strip_threshold(self):
        with frozen_time():
            self.post_hit("https://mail.example.com")
            score = self.score("https://mail.example.com")

        self.assertEqual(score, 1.0)
        # One fresh click already qualifies: the strip threshold is a floor, not
        # the spec's descriptive "~3 clicks".
        self.assertGreater(score, js_number_constant("MOST_USED_MIN_SCORE"))

    def test_score_decays_with_age(self):
        self.write_hits({
            "https://mail.example.com": [NOW - 14 * 86400],
            "https://docs.example.com": [NOW - 28 * 86400],
        })

        with frozen_time():
            self.assertEqual(self.score("https://mail.example.com"), 0.37)
            self.assertEqual(self.score("https://docs.example.com"), 0.14)

    def test_repeated_clicks_add_up(self):
        self.write_hits({"https://mail.example.com": [NOW, NOW - 14 * 86400]})

        with frozen_time():
            self.assertEqual(self.score("https://mail.example.com"), 1.37)

    def test_hits_older_than_90_days_are_trimmed_on_write(self):
        self.write_hits({
            "https://mail.example.com": [NOW - 91 * 86400, NOW - 86400],
        })

        with frozen_time():
            self.post_hit("https://mail.example.com")
            stamps = self.stored_hits()["https://mail.example.com"]

        self.assertEqual(len(stamps), 2)
        self.assertLessEqual(max(NOW - ts for ts in stamps), 90 * 86400)

    def test_unknown_url_is_accepted(self):
        status, body = self.post_hit("https://deleted.example.com")

        self.assertEqual(status, 204)
        self.assertEqual(body, b"")
        self.assertIn("https://deleted.example.com", self.stored_hits())

    def test_empty_or_whitespace_url_is_rejected_and_not_stored(self):
        for url in ("", "   ", "\t\n"):
            with self.subTest(url=url):
                status, _ = self.post_hit(url)
                self.assertEqual(status, 400)

        self.assertFalse((self.data / "hits.json").exists())

    def test_malformed_payloads_are_rejected_and_the_server_survives(self):
        for body in (b"", b"not json", b"{}", b'{"url": 42}', b"[]"):
            with self.subTest(body=body):
                status, _ = self.post(body)
                self.assertGreaterEqual(status, 400)
                self.assertLess(status, 500)

        status, _ = self.get_json("/api/health")
        self.assertEqual(status, 200)

    def test_renaming_a_title_keeps_its_score(self):
        self.post_hit("https://mail.example.com")
        self.write_bookmarks({"work": [{
            "category": "Mail",
            "items": [
                {"title": "Mail", "desc": "Email", "icon": "M",
                 "url": "https://mail.example.com"},
            ],
        }]})

        _, data = self.get_json("/api/bookmarks")

        item = data["work"][0]["items"][0]
        self.assertEqual(item["title"], "Mail")
        self.assertEqual(item["score"], 1.0)

    def test_missing_bookmark_store_degrades_gracefully(self):
        (self.data / "bookmarks.json").unlink()

        status, data = self.get_json("/api/bookmarks")

        self.assertEqual(status, 200)
        self.assertEqual(data.get("work"), [])

    def test_index_injects_scored_bookmarks_and_revalidates(self):
        with frozen_time():
            self.post_hit("https://mail.example.com")
            with urllib.request.urlopen(self.base + "/", timeout=5) as resp:
                html = resp.read().decode()
                etag = resp.headers.get("ETag")
                cache = resp.headers.get("Cache-Control")

            self.assertEqual(cache, "no-cache")
            self.assertNotIn("__BOOKMARKS_JSON__", html)
            self.assertIn('id="bookmarks-data"', html)
            self.assertIn("https://mail.example.com", html)
            self.assertIn('"score": 1.0', html)

            req = urllib.request.Request(self.base + "/", headers={"If-None-Match": etag})
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(caught.exception.code, 304)

    def test_concurrent_hits_are_all_recorded(self):
        url = "https://mail.example.com"

        with frozen_time():
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: self.post_hit(url), range(20)))
            score = self.score(url)

        self.assertTrue(all(status == 204 for status, _ in results))
        self.assertEqual(len(self.stored_hits()[url]), 20)
        self.assertEqual(score, 20.0)


def function_body(source, name):
    """Return the text of a top-level `function <name>(...) { ... }` block.

    This is the seam the browser-only JS offers from a stdlib test: we cannot
    execute app.js here, so assert against the function that owns the behaviour.
    """
    start = source.index("function " + name + "(")
    depth = 0
    for offset, char in enumerate(source[start:], start):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start:offset + 1]
    raise AssertionError("unbalanced function body for " + name)


class FrontEndSourceTest(unittest.TestCase):
    """Shared harness for browser-only code, which has no JS runtime here."""

    def setUp(self):
        self.source = APP_JS.read_text()
        self.js = self.source
        self.html = (APP_JS.parent / "index.html").read_text()
        self.css = (APP_JS.parent / "style.css").read_text()


class MostUsedStripTest(FrontEndSourceTest):
    """Source-level assertions over the strip helpers and the markup they render.

    Manual check in a browser: serve the dashboard, click one link, reload, and
    confirm the strip appears above Network.
    """

    def test_strip_sits_directly_above_the_network_panel_and_starts_hidden(self):
        strip = self.html.index('id="most-used"')
        panel = self.html.index('class="network-panel"')
        self.assertLess(strip, panel)

        tag_start = self.html.rindex("<", 0, strip)
        tag_end = self.html.index(">", strip)
        self.assertIn(" hidden", self.html[tag_start:tag_end])

    def test_selection_keeps_scores_above_half_sorted_desc_capped_at_six(self):
        self.assertRegex(self.js, r"MOST_USED_MIN_SCORE\s*=\s*0\.5\b")
        self.assertRegex(self.js, r"MOST_USED_LIMIT\s*=\s*6\b")

        body = function_body(self.js, "selectMostUsed")
        self.assertRegex(body, r"function selectMostUsed\(categories\)")
        self.assertRegex(body, r"score\s*>\s*MOST_USED_MIN_SCORE\b")
        self.assertRegex(body, r"sort\(\s*\(a,\s*b\)\s*=>\s*b\.score\s*-\s*a\.score\s*\)")
        self.assertRegex(body, r"slice\(\s*0,\s*MOST_USED_LIMIT\s*\)")

    def test_strip_is_populated_from_the_injected_payload_or_the_api_fallback(self):
        load = function_body(self.js, "loadBookmarks")
        render = function_body(self.js, "renderMostUsed")
        update = function_body(self.js, "updateDashboard")
        injected = function_body(self.js, "injectedBookmarks")

        self.assertIn("renderMostUsed(data.work)", load)
        self.assertIn("hidden = true", render)
        self.assertIn("hidden = false", render)

        # The server injects the scored store into index.html, so the first
        # paint can populate the strip without a second request.
        self.assertIn("renderMostUsed(inline.work)", update)
        self.assertIn("renderBookmarks(inline.work)", update)
        self.assertIn("loadBookmarks()", update)
        self.assertIn("bookmarks-data", injected)
        self.assertIn("JSON.parse", injected)

        # The shared renderer stays single-purpose; the strip is populated by
        # its caller, never as a side effect of rendering the categories.
        self.assertNotIn("renderMostUsed", function_body(self.js, "renderBookmarks"))

    def test_category_contents_and_order_are_never_sorted(self):
        body = function_body(self.js, "renderBookmarks")
        section = function_body(self.js, "categorySection")

        self.assertNotIn(".sort(", body)
        self.assertNotIn(".sort(", section)
        self.assertRegex(
            body,
            r"categories\.map\(\s*cat\s*=>\s*categorySection\(cat\.category,\s*cat\.items\)",
        )
        self.assertRegex(section, r"items\.map\(item\s*=>\s*bookmarkCard\(item\)\)")

    def test_bookmarks_and_strip_share_one_category_section_renderer(self):
        self.assertIn(
            "categorySection(cat.category, cat.items)",
            function_body(self.js, "renderBookmarks"),
        )
        self.assertIn(
            "categorySection('Most used', items)",
            function_body(self.js, "renderMostUsed"),
        )
        section = function_body(self.js, "categorySection")
        self.assertIn('class="category-section"', section)
        self.assertIn('class="category-title"', section)
        self.assertIn('class="items-grid"', section)

    def test_strip_tiles_reuse_bookmark_card_markup_and_open_like_tiles(self):
        render = function_body(self.js, "renderMostUsed")
        section = function_body(self.js, "categorySection")
        card = function_body(self.js, "bookmarkCard")

        self.assertIn("categorySection('Most used', items)", render)
        self.assertIn("bookmarkCard(", section)
        self.assertIn('class="bookmark-card"', card)
        self.assertIn('target="_blank"', card)

    def test_strip_is_hidden_by_style_and_survives_the_mobile_breakpoint(self):
        self.assertIn(".most-used[hidden]", self.css)
        self.assertNotRegex(
            self.css,
            r"\.most-used\s*\{[^}]*display\s*:\s*(block|flex|grid)",
        )


class FrontEndWiringTest(FrontEndSourceTest):
    """No JS runtime here, so guard the exact wiring the browser needs."""

    def test_bookmark_clicks_use_sendbeacon_for_click_and_auxclick(self):
        self.assertIn("navigator.sendBeacon('/api/hit', JSON.stringify({ url }))", self.source)
        self.assertRegex(self.source, r"addEventListener\('click'")
        self.assertRegex(self.source, r"addEventListener\('auxclick'")

    def test_hits_do_not_use_cookies_or_fetch(self):
        self.assertNotIn("document.cookie", self.source)
        self.assertNotIn("fetch('/api/hit'", self.source)
        self.assertNotIn('fetch("/api/hit"', self.source)

    def test_strip_has_its_own_click_and_auxclick_listeners(self):
        self.assertIn(
            "mostUsedEl.addEventListener('click', recordBookmarkHit)", self.source
        )
        self.assertIn(
            "mostUsedEl.addEventListener('auxclick', recordBookmarkHit)", self.source
        )
