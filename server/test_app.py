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
