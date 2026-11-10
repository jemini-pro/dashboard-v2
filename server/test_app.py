"""Stdlib tests for the dashboard API: click hits and decayed bookmark scores.

Run from the repo root:
    python3 -m unittest discover -s server -p 'test_*.py' -v
"""
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import app  # noqa: E402

from http.server import ThreadingHTTPServer  # noqa: E402


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
        self._old_data, self._old_hits = app.DATA, app.HITS_FILE
        app.DATA = self.data
        app.HITS_FILE = self.data / "hits.json"
        self.write_bookmarks(WORK_BOOKMARKS)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        app.DATA, app.HITS_FILE = self._old_data, self._old_hits
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
        status, body = self.post_hit("https://mail.example.com")

        self.assertEqual(status, 204)
        self.assertEqual(body, b"")
        self.assertAlmostEqual(
            self.stored_hits()["https://mail.example.com"][0], time.time(), delta=5
        )

    def write_hits(self, hits):
        (self.data / "hits.json").write_text(json.dumps(hits))

    def test_never_clicked_link_scores_zero(self):
        self.assertEqual(self.score("https://docs.example.com"), 0)

    def test_a_click_raises_the_score_on_the_next_load(self):
        self.post_hit("https://mail.example.com")

        self.assertEqual(self.score("https://mail.example.com"), 1.0)

    def test_score_decays_with_age(self):
        now = time.time()
        self.write_hits({
            "https://mail.example.com": [now - 14 * 86400],
            "https://docs.example.com": [now - 28 * 86400],
        })

        self.assertEqual(self.score("https://mail.example.com"), 0.37)
        self.assertEqual(self.score("https://docs.example.com"), 0.14)

    def test_repeated_clicks_add_up(self):
        now = time.time()
        self.write_hits({"https://mail.example.com": [now, now - 14 * 86400]})

        self.assertEqual(self.score("https://mail.example.com"), 1.37)

    def test_hits_older_than_90_days_are_trimmed_on_write(self):
        now = time.time()
        self.write_hits({
            "https://mail.example.com": [now - 91 * 86400, now - 86400],
        })

        self.post_hit("https://mail.example.com")

        stamps = self.stored_hits()["https://mail.example.com"]
        self.assertEqual(len(stamps), 2)
        self.assertLessEqual(max(now - ts for ts in stamps), 90 * 86400 + 5)

    def test_unknown_url_is_accepted(self):
        status, body = self.post_hit("https://deleted.example.com")

        self.assertEqual(status, 204)
        self.assertEqual(body, b"")
        self.assertIn("https://deleted.example.com", self.stored_hits())

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

    def test_concurrent_hits_are_all_recorded(self):
        url = "https://mail.example.com"

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.post_hit(url), range(20)))

        self.assertTrue(all(status == 204 for status, _ in results))
        self.assertEqual(len(self.stored_hits()[url]), 20)
        self.assertEqual(self.score(url), 20.0)


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


class MostUsedStripTest(unittest.TestCase):
    """The Most used strip is browser JS; there is no JS runtime in this repo.

    So these are source-level assertions over the small pure helpers plus the
    markup they render. Manual check in a browser: serve the dashboard, click
    one link three times, reload, and confirm the strip appears above Network.
    """

    ROOT = Path(__file__).resolve().parent.parent

    def setUp(self):
        self.js = (self.ROOT / "app.js").read_text()
        self.html = (self.ROOT / "index.html").read_text()
        self.css = (self.ROOT / "style.css").read_text()

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
        self.assertRegex(body, r"score\s*>\s*(MIN_SCORE|minScore|MOST_USED_MIN_SCORE)")
        self.assertRegex(body, r"sort\(\s*\(a,\s*b\)\s*=>\s*b\.score\s*-\s*a\.score\s*\)")
        self.assertRegex(body, r"slice\(\s*0,\s*limit\s*\)")

    def test_strip_is_populated_only_after_the_scored_api_payload_arrives(self):
        load = function_body(self.js, "loadBookmarks")
        render = function_body(self.js, "renderMostUsed")

        self.assertIn("renderMostUsed(data.work)", load)
        self.assertIn("hidden = true", render)
        self.assertIn("hidden = false", render)

        # The scoreless embedded copy is painted by renderBookmarks, and that
        # path must never populate the strip.
        self.assertNotIn("renderMostUsed", function_body(self.js, "renderBookmarks"))
        self.assertNotIn("renderMostUsed", function_body(self.js, "updateDashboard"))

    def test_embedded_first_paint_bookmarks_carry_no_scores(self):
        embedded = self.js[self.js.index("const bookmarks = {"):self.js.index("const clockEl")]

        self.assertNotIn("score", embedded)

    def test_category_contents_and_order_are_never_sorted(self):
        body = function_body(self.js, "renderBookmarks")

        self.assertNotIn(".sort(", body)
        self.assertRegex(body, r"categories\.map\(cat\s*=>")
        self.assertRegex(body, r"cat\.items\.map\(")

    def test_strip_tiles_reuse_bookmark_card_markup_and_open_like_tiles(self):
        render = function_body(self.js, "renderMostUsed")
        card = function_body(self.js, "bookmarkCard")

        self.assertIn("bookmarkCard(", render)
        self.assertIn('class="bookmark-card"', card)
        self.assertIn('target="_blank"', card)

    def test_strip_is_hidden_by_style_and_survives_the_mobile_breakpoint(self):
        self.assertIn(".most-used[hidden]", self.css)
        self.assertNotRegex(
            self.css,
            r"\.most-used\s*\{[^}]*display\s*:\s*(block|flex|grid)",
        )


class FrontEndWiringTest(unittest.TestCase):
    """No JS runtime here, so guard the exact wiring the browser needs."""

    APP_JS = Path(__file__).resolve().parent.parent / "app.js"

    def setUp(self):
        self.source = self.APP_JS.read_text()

    def test_bookmark_clicks_use_sendbeacon_for_click_and_auxclick(self):
        self.assertIn("navigator.sendBeacon('/api/hit', JSON.stringify({ url }))", self.source)
        self.assertRegex(self.source, r"addEventListener\('click'")
        self.assertRegex(self.source, r"addEventListener\('auxclick'")

    def test_hits_do_not_use_cookies_or_fetch(self):
        self.assertNotIn("document.cookie", self.source)
        self.assertNotIn("fetch('/api/hit'", self.source)
        self.assertNotIn('fetch("/api/hit"', self.source)
