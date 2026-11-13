"""Stdlib tests for the dashboard API: click hits and decayed bookmark scores.

Run from the repo root:
    python3 -m unittest discover -s server -p 'test_*.py' -v
"""
import hmac
import json
import re
import sys
import tempfile
import threading
import time
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
        self._old_agents, self._old_metrics = app.AGENTS_FILE, app.METRICS_FILE
        app.DATA = self.data
        app.HITS_FILE = self.data / "hits.json"
        app.AGENTS_FILE = self.data / "agents.json"
        app.METRICS_FILE = self.data / "metrics.db"
        app._cache.update(at=0.0, body=None)
        app._availability_cache.update(at=0.0, by_id={})
        app._summary_cache.update(at=0.0, window=None, body=None)
        app._last_prune["at"] = 0.0
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
        app.AGENTS_FILE, app.METRICS_FILE = self._old_agents, self._old_metrics
        app._cache.update(at=0.0, body=None)
        app._availability_cache.update(at=0.0, by_id={})
        app._summary_cache.update(at=0.0, window=None, body=None)
        app._last_prune["at"] = 0.0
        self._tmp.cleanup()

    def write_bookmarks(self, store):
        (self.data / "bookmarks.json").write_text(json.dumps(store))

    def post(self, body, path="/api/hit", headers=None):
        req = urllib.request.Request(self.base + path, data=body, method="POST")
        for key, value in (headers or {}).items():
            req.add_header(key, value)
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

    def test_bookmarks_preserve_a_pinned_flag_on_items(self):
        """The Launchpad is data-driven: /api/bookmarks passes `pinned` through."""
        self.write_bookmarks({"work": [{
            "category": "Pinned",
            "items": [
                {"title": "Gmail", "desc": "Email", "icon": "M",
                 "url": "https://mail.example.com", "pinned": True},
                {"title": "Docs", "desc": "Docs", "icon": "D",
                 "url": "https://docs.example.com"},
            ],
        }]})

        _, data = self.get_json("/api/bookmarks")

        items = {item["title"]: item for item in data["work"][0]["items"]}
        self.assertIs(items["Gmail"]["pinned"], True)
        self.assertNotIn("pinned", items["Docs"])
        self.assertIn("score", items["Gmail"])

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


class PushTokenTest(ApiTestCase):
    """The agent heartbeat token gates writes, so its compare must not leak."""

    TOKEN = "correct-horse-battery-staple"

    def setUp(self):
        super().setUp()
        self._old_token, self._old_agents_file = app.TOKEN, app.AGENTS_FILE
        app.TOKEN = self.TOKEN
        app.AGENTS_FILE = self.data / "agents.json"
        # stats() caches for 5 s, which would leak one test's devices into the
        # next one's /api/stats assertion.
        app._cache = {"at": 0.0, "body": None}

    def tearDown(self):
        app.TOKEN, app.AGENTS_FILE = self._old_token, self._old_agents_file
        super().tearDown()

    def push(self, token=None, name="pve"):
        headers = {} if token is None else {"X-Dash-Token": token}
        return self.post(
            json.dumps({"kind": "linux", "cpu_pct": 1.5}).encode(),
            path="/api/push/" + name,
            headers=headers,
        )

    def push_payload(self, payload, name="JEM-PC"):
        return self.post(
            json.dumps(payload).encode(),
            path="/api/push/" + name,
            headers={"X-Dash-Token": self.TOKEN},
        )

    def stored_device(self, name):
        _, body = self.get_json("/api/stats")
        return next(d for d in body["devices"] if d["id"] == "agent:" + name)

    def test_correct_token_is_accepted_and_the_agent_is_stored(self):
        status, body = self.push(self.TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["stored"], "pve")
        stored = json.loads((self.data / "agents.json").read_text())
        self.assertEqual(stored["pve"]["cpu_pct"], 1.5)

    def test_wrong_token_is_rejected_and_nothing_is_stored(self):
        status, body = self.push("wrong")
        self.assertEqual(status, 401)
        self.assertIn("bad token", body.decode())
        self.assertFalse((self.data / "agents.json").exists())

    def test_missing_token_is_rejected(self):
        self.assertEqual(self.push()[0], 401)

    def test_a_container_agent_is_listed_in_the_docker_block(self):
        self.push_payload(
            {
                "kind": "container",
                "name": "sams-uwu-gf",
                "cpu_pct": 0.4,
                "mem_pct": 5.1,
                "mem_used": 104857600,
                "mem_total": 2048000000,
                "state": "running",
                "role": "discord bot · healthy",
            },
            name="sams-uwu-gf",
        )
        app._cache.update(at=0.0, body=None)

        with frozen_time():
            body = app.stats()

        containers = {c["name"]: c for c in body["topology"]["containers"]}
        self.assertIn("sams-uwu-gf", containers)
        self.assertEqual(containers["sams-uwu-gf"]["state"], "running")
        self.assertEqual(containers["sams-uwu-gf"]["role"], "discord bot · healthy")
        # The static entries are still described alongside it.
        self.assertIn("caddy", containers)
        self.assertIn("dash", containers)

        device = next(d for d in body["devices"] if d["id"] == "agent:sams-uwu-gf")
        self.assertEqual(device["kind"], "container")
        self.assertEqual(device["mem_used"], 104857600)

    def test_a_container_that_stops_reporting_reads_as_exited(self):
        self.push_payload(
            {"kind": "container", "name": "uwu", "state": "running"}, name="uwu"
        )
        stored = json.loads((self.data / "agents.json").read_text())
        stored["uwu"]["last_seen"] = int(time.time()) - 3600
        (self.data / "agents.json").write_text(json.dumps(stored))

        app._cache.update(at=0.0, body=None)
        containers = {c["name"]: c for c in app.stats()["topology"]["containers"]}

        # A stored "running" must not outlive the heartbeat.
        self.assertEqual(containers["uwu"]["state"], "exited")

    def test_container_state_and_role_are_sanitised(self):
        self.push_payload(
            {"kind": "container", "name": "odd", "state": "run\x00ning",
             "role": "r" * 200},
            name="odd",
        )
        app._cache.update(at=0.0, body=None)

        with frozen_time():
            body = app.stats()

        container = next(c for c in body["topology"]["containers"] if c["name"] == "odd")
        self.assertNotIn("\x00", container["state"])
        self.assertEqual(len(container["role"]), app.MAX_CONTAINER_TEXT)

    def test_stats_describes_the_pve_docker_stack(self):
        self.push_payload({"name": "pve", "kind": "host"}, name="pve")
        app._cache.update(at=0.0, body=None)

        with frozen_time():
            body = app.stats()

        self.assertEqual(body["topology"]["host_id"], "agent:pve")
        self.assertEqual(body["topology"]["docker_host_id"], "docker01")
        self.assertEqual(
            [container["name"] for container in body["topology"]["containers"]],
            ["caddy", "dash"],
        )
        self.assertTrue(all(
            container["state"] == "running"
            for container in body["topology"]["containers"]
        ))

    def test_stats_without_a_host_agent_still_describes_the_docker_layer(self):
        app._cache.update(at=0.0, body=None)

        with frozen_time():
            body = app.stats()

        self.assertIsNone(body["topology"]["host_id"])
        self.assertEqual(body["topology"]["docker_host_id"], "docker01")
        self.assertEqual(len(body["topology"]["containers"]), 2)

    def test_static_docker_containers_match_the_compose_services(self):
        compose = (Path(__file__).resolve().parent / "docker-compose.yml").read_text()
        for container in app.DOCKER_CONTAINERS:
            self.assertIn("\n  " + container["name"] + ":", compose,
                          "Docker container no longer matches a compose service")

    def test_token_compare_goes_through_hmac_compare_digest(self):
        with mock.patch("app.hmac.compare_digest", wraps=hmac.compare_digest) as spy:
            self.assertEqual(self.push(self.TOKEN)[0], 200)
        self.assertTrue(spy.called, "push token is compared with a plain ==")

    def test_the_annotation_endpoint_uses_the_same_token(self):
        payload = json.dumps({"kind": "deploy", "text": "v1.2"}).encode()
        self.assertEqual(self.post(payload, path="/api/annotations")[0], 401)
        self.assertEqual(
            self.post(payload, path="/api/annotations",
                      headers={"X-Dash-Token": "wrong"})[0], 401
        )
        self.assertEqual(
            self.post(payload, path="/api/annotations",
                      headers={"X-Dash-Token": self.TOKEN})[0], 201
        )

    def test_every_volume_is_stored_and_the_fullest_drives_the_summary(self):
        status, _ = self.push_payload({
            "kind": "pc",
            "disk_pct": 12.0,
            "disks": [
                {"mount": "C:\\", "label": "Windows", "total": 1000, "used": 120, "pct": 12.0},
                {"mount": "D:\\", "label": "Data", "total": 1000, "used": 987, "pct": 98.7},
                {"mount": "E:\\", "label": "External", "total": 2000, "used": 400, "pct": 20.0},
            ],
        })
        self.assertEqual(status, 200)
        device = self.stored_device("JEM-PC")
        self.assertEqual([d["mount"] for d in device["disks"]], ["C:\\", "D:\\", "E:\\"])
        self.assertEqual([d["label"] for d in device["disks"]], ["Windows", "Data", "External"])
        # The single DSK gauge must not stay pinned to C: when D: is nearly full.
        self.assertEqual(device["disk_pct"], 98.7)
        stored = json.loads((self.data / "agents.json").read_text())["JEM-PC"]
        self.assertEqual(len(stored["disks"]), 3)

    def test_malformed_disk_entries_are_dropped_and_values_clamped(self):
        status, _ = self.push_payload({
            "kind": "pc",
            "disks": [
                {"mount": "/good", "label": "Good", "total": 100, "used": 250, "pct": 250},
                {"mount": "", "label": "empty mount", "total": 100, "used": 1, "pct": 1},
                {"mount": "/negative", "total": -1, "used": 0, "pct": 0},
                {"mount": "/nan", "total": 100, "used": 10, "pct": float("nan")},
                {"mount": "/string", "total": 100, "used": 10, "pct": "high"},
                "not a dict",
                {"mount": "/missing", "label": "Computed", "total": 100, "used": 10},
            ],
        })
        self.assertEqual(status, 200)
        device = self.stored_device("JEM-PC")
        self.assertEqual([d["mount"] for d in device["disks"]], ["/good", "/nan", "/string", "/missing"])
        self.assertEqual(device["disks"][0]["used"], 100)  # clamped to total
        self.assertEqual(device["disks"][0]["pct"], 100.0)
        # A missing or non-numeric pct is recovered from used/total.
        self.assertEqual([d["pct"] for d in device["disks"][1:]], [10.0, 10.0, 10.0])

    def test_disk_list_is_capped_so_one_heartbeat_cannot_flood_the_panel(self):
        status, _ = self.push_payload({
            "kind": "pc",
            "disks": [
                {"mount": "/disk%d" % i, "total": 100, "used": 1, "pct": 1}
                for i in range(app.MAX_DISKS + 20)
            ],
        })
        self.assertEqual(status, 200)
        self.assertEqual(len(self.stored_device("JEM-PC")["disks"]), app.MAX_DISKS)

    def test_an_old_agent_without_disks_still_works(self):
        # Single-volume agents keep pushing only disk_pct; the page must not
        # invent a breakdown for them.
        self.assertEqual(self.push(self.TOKEN)[0], 200)
        device = self.stored_device("pve")
        self.assertEqual(device["disks"], [])
        self.assertIsNone(device["disk_pct"])


class FrontEndSourceTest(unittest.TestCase):
    """Shared harness for browser-only code, which has no JS runtime here."""

    def setUp(self):
        self.source = APP_JS.read_text()
        self.js = self.source
        self.html = (APP_JS.parent / "index.html").read_text()
        self.css = (APP_JS.parent / "style.css").read_text()


class MostUsedCardTest(FrontEndSourceTest):
    """Ticket #44: the Jump back in card is driven by server-stamped scores.

    There is no JS runtime here, so these assert the seams the card exposes:
    selection, normalisation, row markup, the empty state and beacon
    delegation. Manual check: click a link, reload, and confirm it rises in
    the card beside the Launchpad.
    """

    def _usage_card(self):
        start = self.html.index('class="card usage-card"')
        end = self.html.index('class="card launchpad-card"', start)
        return self.html[start:end]

    def test_card_sits_beside_launchpad_without_tab_chrome_and_keeps_an_empty_list(self):
        card = self._usage_card()

        self.assertIn("Jump back in", card)
        self.assertIn('id="most-used"', card)
        self.assertIn('class="usage-list"', card)
        self.assertNotIn("hidden", card)
        self.assertNotIn('role="tab', card)
        self.assertNotIn("data-usage-tab", card)
        self.assertNotIn("Recent", card)

    def test_selection_keeps_scores_above_half_sorted_desc_capped_at_six(self):
        self.assertRegex(self.js, r"MOST_USED_MIN_SCORE\s*=\s*0\.5\b")
        self.assertRegex(self.js, r"MOST_USED_LIMIT\s*=\s*6\b")

        body = function_body(self.js, "selectMostUsed")
        self.assertRegex(body, r"function selectMostUsed\(categories\)")
        self.assertRegex(body, r"score\s*>\s*MOST_USED_MIN_SCORE\b")
        self.assertRegex(body, r"sort\(\s*\(a,\s*b\)\s*=>\s*b\.score\s*-\s*a\.score\s*\)")
        self.assertRegex(body, r"slice\(\s*0,\s*MOST_USED_LIMIT\s*\)")

    def test_score_bar_is_normalised_against_the_highest_visible_score(self):
        render = function_body(self.js, "renderMostUsed")
        row = function_body(self.js, "usageRow")
        percent = function_body(self.js, "usageBarPercent")

        self.assertIn("entries[0].score", render)
        self.assertIn("usageRow(entry, maxScore)", render)
        self.assertIn("usageBarPercent(entry.score, maxScore)", row)
        self.assertIn("(score / maxScore) * 100", percent)
        self.assertIn("Math.round", percent)
        self.assertIn("Math.max(0,", percent)
        self.assertIn("Math.min(100,", percent)

    def test_rows_show_monogram_title_description_and_bar_without_the_raw_score(self):
        row = function_body(self.js, "usageRow")

        self.assertIn("const item = entry.item", row)
        self.assertIn("monogram(item)", row)
        self.assertIn('class="usage-row"', row)
        self.assertIn("escapeHtml(item.title)", row)
        self.assertIn("escapeHtml(item.desc || '')", row)
        self.assertIn('target="_blank"', row)
        self.assertIn('class="usage-bar"', row)
        self.assertIn("width:", row)
        self.assertNotIn("${entry.score}", row)
        self.assertNotIn("usage-score", row)
        self.assertNotIn("usage-count", row)

    def test_empty_state_keeps_the_card_and_shows_no_usage_yet(self):
        render = function_body(self.js, "renderMostUsed")
        update = function_body(self.js, "updateDashboard")
        injected = function_body(self.js, "injectedBookmarks")

        self.assertIn("No usage yet", render)
        self.assertIn("hidden = false", render)
        self.assertNotIn("hidden = true", render)

        # Server-injected first paint uses the scored store; with no injection
        # (static-only hosting) the fetch fallback takes over.
        self.assertIn("injectedBookmarks()", update)
        self.assertIn("renderMostUsed(bookmarkStore)", update)
        self.assertIn("loadBookmarks()", update)
        self.assertIn("bookmarks-data", injected)
        self.assertIn("JSON.parse", injected)

    def test_first_paint_and_a_failed_fetch_both_show_no_usage_yet(self):
        # The injected store is scored; when /api/bookmarks is unreachable the
        # card keeps its quiet empty state instead of a blank panel.
        update = function_body(self.js, "updateDashboard")
        load = function_body(self.js, "loadBookmarks")

        self.assertIn("renderMostUsed(bookmarkStore)", update)
        self.assertIn("renderMostUsed(bookmarkStore)", load)

        # Scoreless data can never populate rows: selection requires a score
        # above the threshold.
        select = function_body(self.js, "selectMostUsed")
        self.assertIn("score > MOST_USED_MIN_SCORE", select)

    def test_only_the_scored_api_payload_populates_the_card(self):
        load = function_body(self.js, "loadBookmarks")

        self.assertIn("renderMostUsed(data.work)", load)

    def test_jump_back_in_never_reorders_the_tools_lists(self):
        # Score-driven ordering belongs to the card alone; All tools keeps the
        # store order.
        for name in ("renderAllTools", "filterToolCategories", "renderMostUsed"):
            with self.subTest(function=name):
                self.assertIn(name, self.js)
        self.assertNotIn(".sort(", function_body(self.js, "renderAllTools"))
        self.assertNotIn(".sort(", function_body(self.js, "filterToolCategories"))

    def test_old_most_used_strip_is_gone(self):
        self.assertNotIn('class="most-used', self.html)
        self.assertNotIn(".most-used", self.css)
        self.assertNotIn("categorySection('Most used'", self.js)

    def test_usage_rows_have_their_own_compact_styles(self):
        self.assertRegex(self.css, r"\.usage-list\s*\{[^}]*list-style:\s*none")
        self.assertRegex(self.css, r"\.usage-row\s*\{[^}]*display:\s*grid")
        self.assertRegex(self.css, r"\.usage-row\s*\{[^}]*grid-template-columns:")
        self.assertRegex(self.css, r"\.usage-bar\s*\{[^}]*height:\s*3px")
        self.assertRegex(
            self.css,
            r"\.usage-bar > span\s*\{[^}]*background:\s*var\(--accent-color\)",
        )


class LaunchpadSourceTest(FrontEndSourceTest):
    """Ticket #43: pinned Launchpad selection, tiles and beacon delegation."""

    def test_pinned_items_keep_store_order_and_cap_at_eight(self):
        self.assertRegex(self.js, r"PINNED_LIMIT\s*=\s*8\b")
        body = function_body(self.js, "selectLaunchpad")

        self.assertIn("pinned === true", body)
        self.assertIn("slice(0, PINNED_LIMIT)", body)
        # The count follows the store; never assume the embedded 24-link copy.
        self.assertNotIn("24", body)

    def test_no_pinned_items_fall_back_to_the_named_titles(self):
        fallback = re.search(r"PINNED_FALLBACK_TITLES\s*=\s*\[(.*?)\]", self.js, re.S)
        self.assertIsNotNone(fallback, "PINNED_FALLBACK_TITLES not found")
        titles = re.findall(r"'([^']+)'", fallback.group(1))
        self.assertEqual(titles, [
            "Gmail", "Google Calendar", "Google Sheets", "GitHub",
            "Slack", "T3 Code", "Proxmox", "AWS",
        ])

        body = function_body(self.js, "selectLaunchpad")
        # Fallback applies only when nothing opted in, walks the listed order
        # and skips titles the store does not carry.
        self.assertIn("if (!pinned.length)", body)
        self.assertIn("PINNED_FALLBACK_TITLES", body)
        self.assertIn(".map(", body)
        self.assertIn(".find(", body)
        self.assertIn("item.title === title", body)
        self.assertIn("slice(0, PINNED_LIMIT)", body)

    def test_pinned_items_keep_store_order_while_fallback_uses_the_listed_order(self):
        body = function_body(self.js, "selectLaunchpad")

        # The pinned path filters the flattened store and never sorts it.
        self.assertIn("items.filter(item => item && item.pinned === true)", body)
        self.assertNotIn(".sort(", body)
        # The fallback path derives its order from PINNED_FALLBACK_TITLES, not
        # from the store order the matched items happen to sit in.
        fallback = body[body.index("if (!pinned.length)"):]
        self.assertIn("PINNED_FALLBACK_TITLES", fallback)
        self.assertIn(".map(", fallback)
        self.assertIn(".find(", fallback)

    def test_tiles_use_monogram_and_a_two_line_label_and_open_a_new_tab(self):
        render = function_body(self.js, "renderLaunchpad")
        tile = function_body(self.js, "launchpadTile")

        self.assertIn("selectLaunchpad(categories)", render)
        self.assertIn(".map(launchpadTile)", render)
        self.assertIn('class="launchpad-tile"', tile)
        self.assertIn("monogram(item)", tile)
        self.assertIn('class="launchpad-tile-label"', tile)
        self.assertIn('target="_blank"', tile)
        self.assertIn('rel="noreferrer"', tile)

        self.assertRegex(
            self.css,
            r"\.launchpad-tile-label\s*\{[^}]*-webkit-line-clamp:\s*2",
        )

    def test_tile_count_follows_the_store_not_the_embedded_copy(self):
        render = function_body(self.js, "renderLaunchpad")
        select = function_body(self.js, "selectLaunchpad")

        self.assertIn("items.map(launchpadTile)", render)
        self.assertNotIn("24", render + select)
        self.assertNotIn("bookmarks.work", render + select)

        # The embedded copy paints the fallback first; the API response then
        # re-renders from whatever the store actually pins.
        self.assertIn(
            "renderLaunchpad(bookmarkStore)", function_body(self.js, "updateDashboard")
        )
        load = function_body(self.js, "loadBookmarks")
        self.assertIn("bookmarkStore = data.work", load)
        self.assertIn("renderLaunchpad(bookmarkStore)", load)

    def test_click_and_auxclick_delegate_to_sendbeacon_without_fetch(self):
        body = function_body(self.js, "recordHit")

        self.assertIn(".launchpad-tile", body)
        self.assertIn(
            "navigator.sendBeacon('/api/hit', JSON.stringify({ url }))", body
        )
        self.assertIn("event.type === 'auxclick' && event.button !== 1", body)
        self.assertNotIn("fetch('/api/hit'", self.js)


class FrontEndWiringTest(FrontEndSourceTest):
    """No JS runtime here, so guard the exact wiring the browser needs."""

    def test_one_delegated_handler_records_every_link_kind(self):
        body = function_body(self.js, "recordHit")

        for selector in (".usage-row", ".tool-link", ".launchpad-tile"):
            with self.subTest(selector=selector):
                self.assertIn(selector, body)
        self.assertIn("closest(", body)
        self.assertIn("getAttribute('href')", body)
        self.assertIn("navigator.sendBeacon('/api/hit', JSON.stringify({ url }))", body)
        self.assertIn("event.type === 'auxclick' && event.button !== 1", body)

        # A single document-level listener pair records one hit per click no
        # matter which container rendered the link.
        self.assertEqual(self.js.count("addEventListener('click', recordHit)"), 1)
        self.assertEqual(self.js.count("addEventListener('auxclick', recordHit)"), 1)
        self.assertIn("document.addEventListener('click', recordHit)", self.js)
        self.assertIn("document.addEventListener('auxclick', recordHit)", self.js)

        # The old per-container handlers are gone, so the same click can never
        # be recorded twice.
        for dead in ("recordBookmarkHit", "recordToolHit", "recordLaunchpadHit"):
            with self.subTest(handler=dead):
                self.assertNotIn(dead, self.js)

    def test_hits_do_not_use_cookies_or_fetch(self):
        self.assertNotIn("document.cookie", self.source)
        self.assertNotIn("fetch('/api/hit'", self.source)
        self.assertNotIn('fetch("/api/hit"', self.source)


class WorkbenchShellTest(FrontEndSourceTest):
    """Ticket #41: the Workbench shell, toolbar and body layout.

    The page shell is static markup plus CSS; these assertions pin the ordering
    and the two-column grid the other Workbench tickets fill in.
    """

    def test_toolbar_sits_above_a_two_column_body(self):
        toolbar = self.html.index('class="toolbar')
        body = self.html.index('class="body-grid"')
        main = self.html.index('class="main-column"')
        rail = self.html.index('class="rail"')

        self.assertLess(toolbar, body)
        self.assertLess(main, rail)

    def test_main_column_stacks_usage_and_launchpad_above_all_tools(self):
        top_row = self.html.index('class="top-row"')
        usage = self.html.index('id="most-used"')
        launchpad = self.html.index('id="launchpad-grid"')
        all_tools = self.html.index('id="bookmarks-grid"')

        self.assertLess(top_row, usage)
        self.assertLess(usage, all_tools)
        self.assertLess(launchpad, all_tools)

    def test_network_tops_the_rail(self):
        rail = self.html.index('class="rail"')
        network = self.html.index('class="network-panel')

        self.assertLess(rail, network)
        self.assertLess(network, self.html.index("</aside>"))

    def test_network_panel_reuses_the_card_surface_instead_of_repeating_it(self):
        self.assertIn('class="network-panel card"', self.html)

        panel = re.search(r"\.network-panel\s*\{(.*?)\}", self.css, re.S)
        self.assertIsNotNone(panel, ".network-panel rule not found")
        body = panel.group(1)
        for duplicated in ("background:", "border:", "border-radius:",
                           "box-shadow:", "backdrop-filter:"):
            with self.subTest(property=duplicated):
                self.assertNotIn(duplicated, body)
        self.assertRegex(body, r"padding:")  # Network keeps its own padding

    def test_body_grid_uses_the_specified_columns_and_gap(self):
        self.assertRegex(
            self.css,
            r"\.body-grid\s*\{[^}]*grid-template-columns:\s*"
            r"minmax\(0,\s*1fr\)\s+clamp\(320px,\s*25vw,\s*390px\)",
        )
        self.assertRegex(self.css, r"\.body-grid\s*\{[^}]*gap:\s*16px")

    def test_main_starts_with_a_1_35fr_by_0_9fr_row(self):
        self.assertRegex(self.css, r"\.top-row\s*\{[^}]*1\.35fr[^}]*0\.9fr")

    def test_page_never_scrolls_only_inner_regions_do(self):
        self.assertRegex(self.css, r"body\s*\{[^}]*overflow:\s*hidden")
        self.assertRegex(self.css, r"\.body-grid\s*\{[^}]*min-height:\s*0")

    def test_agenda_calendar_embed_and_greeting_are_gone(self):
        self.assertNotIn("calendar.google.com/embed", self.html)
        self.assertNotIn("<iframe", self.html)
        self.assertNotIn("Agenda", self.html)
        self.assertNotIn("greeting", self.html)
        self.assertNotIn("greeting", self.js)
        self.assertNotIn("Time to Focus", self.js)
        self.assertNotIn("Time to Relax", self.js)


class SearchWiringTest(FrontEndSourceTest):
    """Ticket #41: search placeholder, slash focus, Escape clear/blur."""

    def test_search_field_has_placeholder_and_accessible_label(self):
        self.assertIn('placeholder="Search all tools…"', self.html)
        self.assertRegex(
            self.html, r'<input[^>]*id="search"[^>]*aria-label="Search all tools"'
        )

    def test_slash_focuses_the_field_unless_already_typing(self):
        self.assertIn("e.key !== '/'", self.js)
        self.assertIn("searchInput.focus()", self.js)

        guard = function_body(self.js, "isTypingTarget")
        self.assertIn("INPUT", guard)
        self.assertIn("TEXTAREA", guard)
        self.assertIn("isContentEditable", guard)

        # The global shortcut must consult the guard before focusing.
        handler = self.js[self.js.index("e.key !== '/'"):]
        self.assertIn("isTypingTarget", handler[:400])

    def test_escape_clears_and_blurs_the_search(self):
        self.assertIn("e.key !== 'Escape'", self.js)
        self.assertIn("searchInput.value = ''", self.js)
        self.assertIn("searchInput.blur()", self.js)

    def test_arrow_keys_are_never_intercepted(self):
        self.assertNotIn("Arrow", self.js)


class ThemeAndDebugTest(FrontEndSourceTest):
    """Ticket #41: theme keeps flipping at 09:00-17:00 and debug time wins."""

    def test_work_hours_drive_the_theme(self):
        self.assertRegex(self.js, r"WORK_START_HOUR\s*=\s*9\b")
        self.assertRegex(self.js, r"WORK_END_HOUR\s*=\s*17\b")

        body = function_body(self.js, "updateDashboard")
        self.assertIn("getCurrentTime()", body)
        self.assertIn("WORK_START_HOUR", body)
        self.assertIn("data-theme", body)
        self.assertIn("setAttribute", body)

    def test_debug_time_overrides_the_clock_and_theme(self):
        body = function_body(self.js, "getCurrentTime")
        self.assertIn("debugTimeOffset", body)
        self.assertIn(
            "debugTimeOffset = debugDate.getTime() - now.getTime()", self.js
        )
        self.assertIn("debugTimeOffset = null", self.js)

    def test_clock_and_date_live_in_the_toolbar(self):
        self.assertIn('id="clock"', self.html)
        self.assertIn('id="date"', self.html)
        self.assertRegex(self.css, r"\.clock\s*\{[^}]*font-size:\s*1\.55rem")


class MonogramIconTest(FrontEndSourceTest):
    """Ticket #41: monograms only, no emoji, no third-party icon requests."""

    def test_monogram_helper_renders_the_first_letter_only(self):
        body = function_body(self.js, "monogram")

        self.assertIn('class="monogram"', body)
        self.assertIn("charAt(0).toUpperCase()", body)
        self.assertNotIn("http", body)
        self.assertNotIn("<img", body)

    def test_rows_use_the_monogram_helper_not_emoji(self):
        # Every surviving link renderer goes through the monogram helper.
        for name in ("usageRow", "launchpadTile"):
            with self.subTest(function=name):
                body = function_body(self.js, name)
                self.assertIn("monogram(item)", body)
                self.assertNotIn("item.icon", body)

        # The embedded bookmark copy is gone: the server injects the scored
        # store, so there is no in-page icon field to render.
        self.assertNotIn("const bookmarks = {", self.js)
        self.assertNotIn("s2/favicons", self.js)
        for emoji in ("📧", "📅", "📊", "🐙", "🤖", "☁️"):
            self.assertNotIn(emoji, self.js)


class HealthIndicatorTest(FrontEndSourceTest):
    """Ticket #41: dot plus n/m online; amber partial, green only all online."""

    def test_health_shows_online_counts_and_only_goes_green_when_all_are_online(self):
        body = function_body(self.js, "renderHealth")

        self.assertIn("online + '/' + total + ' online'", body)
        self.assertIn("online === total", body)
        self.assertIn("'warn'", body)
        self.assertIn("'online'", body)

    def test_health_dot_amber_when_partial_and_green_when_all_online(self):
        self.assertRegex(self.css, r"\.health-dot\.online\s*\{[^}]*#48bb78")
        self.assertRegex(self.css, r"\.health-dot\.warn\s*\{[^}]*#ed8936")

    def test_stats_poll_updates_health_without_hiding_the_panel(self):
        body = function_body(self.js, "refreshDevices")

        self.assertIn("renderHealth(", body)
        self.assertNotIn("display = 'none'", body)


class AllToolsTest(FrontEndSourceTest):
    """Ticket #42: numbered, searchable category lists.

    No JS runtime is available here, so the behaviour is pinned at the function
    boundaries the browser calls: filtering, row markup, counter/empty text and
    the delegated beacon. Manual check: serve the dashboard, search for part of
    a description and confirm only matching rows survive with "x of N tools".
    """

    def test_rows_show_a_two_digit_index_title_description_and_hover_arrow(self):
        row = function_body(self.js, "toolRow")

        self.assertIn("padStart(2, '0')", row)
        self.assertIn('class="tool-num"', row)
        self.assertIn('class="tool-link-title"', row)
        self.assertIn('class="tool-link-desc"', row)
        self.assertIn('class="tool-link-arrow"', row)
        self.assertIn("↗", row)
        self.assertIn("escapeHtml(item.url)", row)
        self.assertIn("escapeHtml(item.title)", row)
        self.assertIn("escapeHtml(item.desc || '')", row)
        self.assertIn('target="_blank"', row)
        self.assertIn('rel="noreferrer"', row)

    def test_filter_matches_title_description_and_category_case_insensitively(self):
        body = function_body(self.js, "filterToolCategories")

        self.assertIn(".toLowerCase()", body)
        self.assertIn("item.title", body)
        self.assertIn("item.desc", body)
        self.assertIn("cat.category", body)
        self.assertIn(".includes(needle)", body)
        self.assertIn(".trim()", body)

    def test_filter_keeps_store_order_and_drops_categories_without_matches(self):
        body = function_body(self.js, "filterToolCategories")

        self.assertIn(".map(", body)
        self.assertIn(".filter(cat => cat.items.length)", body)
        self.assertNotIn(".sort(", body)

        render = function_body(self.js, "renderAllTools")
        self.assertIn("filterToolCategories(", render)
        self.assertIn(".map(cat => toolCategorySection(cat))", render)
        self.assertNotIn(".sort(", render)

    def test_category_titles_and_numbering_stay_within_each_category(self):
        section = function_body(self.js, "toolCategorySection")

        self.assertIn('class="tool-category"', section)
        self.assertIn('class="tool-category-title"', section)
        self.assertIn("escapeHtml(cat.category)", section)
        self.assertIn("cat.items.map((item, i) => toolRow(item, i))", section)
        self.assertNotIn(".sort(", section)

    def test_filtering_shows_x_of_n_tools_and_names_the_query_when_empty(self):
        render = function_body(self.js, "renderAllTools")
        count = function_body(self.js, "resultCountText")
        empty = function_body(self.js, "allToolsEmptyText")

        self.assertIn("resultCountText(", render)
        self.assertIn("allToolsEmptyText(", render)
        self.assertIn("resultCountEl.textContent", render)
        self.assertIn("filtered.length", render)

        self.assertIn("shown + ' of ' + total + ' tools'", count)
        self.assertIn("'No tools match “'", empty)
        self.assertIn("'”.'", empty)

    def test_counter_is_empty_when_not_filtering(self):
        # The spec only asks for "x of N tools" while a query is active; with
        # no query the card head stays quiet instead of showing a full count.
        count = function_body(self.js, "resultCountText")

        self.assertIn(": ''", count)
        self.assertNotIn("' tools · '", count)
        self.assertNotIn("groups", count)

    def test_result_count_lives_in_the_all_tools_card_head(self):
        card = self.html[self.html.index('class="card all-tools-card"'):]
        self.assertIn('id="result-count"', card[:card.index("</section>")])
        self.assertIn('id="bookmarks-grid"', card[:card.index("</section>")])

    def test_dead_bookmark_render_path_is_removed(self):
        # There is one All tools renderer; the old card-grid path had no
        # runtime callers and must not linger as dead code.
        for name in ("renderBookmarks", "categorySection", "bookmarkCard"):
            with self.subTest(function=name):
                self.assertNotIn("function " + name + "(", self.js)
                self.assertNotIn(name + "(", self.js)

        for selector in (".category-section", ".items-grid", ".bookmark-card",
                         ".bookmark-icon-text", ".bookmark-details",
                         ".bookmark-title", ".bookmark-desc", ".category-title"):
            with self.subTest(selector=selector):
                self.assertNotIn(selector, self.css)

        # The All tools container and its column rules stay.
        self.assertIn('id="bookmarks-grid"', self.html)
        self.assertRegex(self.css, r"\.bookmarks-grid\s*\{[^}]*columns:\s*3\b")

    def test_search_filter_comment_does_not_claim_another_ticket_owns_it(self):
        self.assertNotIn("#42 owns the All tools filtering", self.js)
        self.assertNotIn("leaves the full list visible until then", self.js)

    def test_three_balanced_columns_then_two_then_one(self):
        self.assertRegex(self.css, r"\.bookmarks-grid\s*\{[^}]*columns:\s*3\b")
        self.assertRegex(
            self.css,
            re.compile(
                r"@media\s*\(max-width:\s*900px\)\s*\{.*?"
                r"\.bookmarks-grid\s*\{[^}]*columns:\s*2\b",
                re.S,
            ),
        )
        self.assertRegex(
            self.css,
            re.compile(
                r"@media\s*\(max-width:\s*600px\)\s*\{.*?"
                r"\.bookmarks-grid\s*\{[^}]*columns:\s*1\b",
                re.S,
            ),
        )
        self.assertRegex(self.css, r"\.tool-category\s*\{[^}]*break-inside:\s*avoid")

    def test_all_tools_clicks_use_the_delegated_beacon_for_click_and_auxclick(self):
        handler = function_body(self.js, "recordHit")

        self.assertIn(".tool-link", handler)
        self.assertIn("event.button !== 1", handler)
        self.assertIn("getAttribute('href')", handler)
        self.assertIn(
            "navigator.sendBeacon('/api/hit', JSON.stringify({ url }))", handler
        )
        self.assertIn("document.addEventListener('click', recordHit)", self.js)
        self.assertIn("document.addEventListener('auxclick', recordHit)", self.js)
        self.assertNotIn("fetch('/api/hit'", handler)


class WorkbenchResponsiveTest(FrontEndSourceTest):
    """Ticket #46: acceptance breakpoints, one-screen fit and cleanup.

    The shell is static CSS; there is no layout engine here. These pin the
    media-query contract, while the browser pass measured 1280x800,
    1440x900 and 390x844 against the real app.
    """

    def media_block(self, condition):
        marker = "@media " + condition
        start = self.css.index(marker)
        depth = 0
        for offset, char in enumerate(self.css[start:], start):
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return self.css[start:offset + 1]
        raise AssertionError("unbalanced media block for " + condition)

    def test_below_1181_the_body_is_one_column_and_sized_to_its_content(self):
        block = self.media_block("(max-width: 1180px)")

        self.assertRegex(
            block,
            r"\.body-grid\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\)",
        )
        # The grid becomes the scroll container, so its rows must size to the
        # content. Stretching them squeezes All tools to zero height and lets
        # the rail overlap the main column at phone widths.
        self.assertRegex(block, r"\.body-grid\s*\{[^}]*grid-auto-rows:\s*max-content")
        # Agenda is dropped, so Network sits below the whole main column in a
        # single column rather than occupying half of a two-column rail.
        self.assertRegex(
            block,
            r"\.rail\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\)",
        )

    def test_tablets_stack_the_launchpad_and_use_two_list_columns(self):
        block = self.media_block("(max-width: 900px)")

        self.assertRegex(block, r"\.top-row\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\)")
        self.assertRegex(block, r"\.bookmarks-grid\s*\{[^}]*columns:\s*2\b")

    def test_phones_use_one_list_column_and_four_launchpad_tiles_per_row(self):
        block = self.media_block("(max-width: 600px)")

        self.assertRegex(block, r"\.bookmarks-grid\s*\{[^}]*columns:\s*1\b")
        # auto-fit + a quarter-width minimum (minus the three 8px gaps) fits
        # exactly four tiles per row at any phone width, while collapsing empty
        # tracks when fewer than four tools are pinned.
        self.assertRegex(
            block,
            r"\.launchpad-grid\s*\{[^}]*repeat\(auto-fit,\s*"
            r"minmax\(calc\(25%\s*-\s*6px\),\s*1fr\)\)",
        )

    def test_short_viewports_tighten_the_shell_cards_and_device_rows(self):
        block = self.media_block("(max-height: 850px)")

        self.assertRegex(block, r"\.workbench\s*\{[^}]*padding:\s*14px")
        self.assertRegex(block, r"\.card\s*\{[^}]*padding:\s*12px 14px")
        self.assertRegex(block, r"\.network-panel\s*\{[^}]*padding:\s*12px 14px")
        self.assertRegex(block, r"\.device-row\s*\{[^}]*padding:\s*3px 0")
        self.assertRegex(block, r"\.usage-list\s*\{[^}]*max-height:\s*150px")

    def test_no_prototype_directory_or_variant_switcher_remains(self):
        root = APP_JS.parent
        self.assertFalse((root / "prototype").exists(), "prototype/ is still present")

        for name in ("app.js", "stack.js", "index.html", "style.css",
                     "metrics.js", "metrics.html", "metrics.css"):
            text = (root / name).read_text()
            with self.subTest(file=name):
                self.assertNotIn("?variant", text)
                self.assertNotIn("variant=", text)
                self.assertNotIn("proto-bar", text)


class NetworkCardTest(FrontEndSourceTest):
    """Ticket #45: kind averages, compact device rows and in-place polling.

    Same stdlib constraint as the rest of this file: assert against the helper
    that owns each rule rather than executing browser-only JavaScript.
    """

    def test_summary_reads_online_count_and_average_cpu(self):
        body = function_body(self.js, "networkSummary")

        self.assertIn("online + '/' + total + ' online", body)
        self.assertIn("' avg CPU'", body)
        self.assertIn("avgText(", body)

    def test_summary_always_renders_avg_cpu_even_without_a_sample(self):
        # No numeric sample must read "— avg CPU", never silently drop the
        # metric from the summary.
        body = function_body(self.js, "networkSummary")

        self.assertIn("+ ' avg CPU'", body)
        self.assertNotIn("cpu === null ?", body)
        self.assertIn("'—'", function_body(self.js, "avgText"))

    def test_summary_average_uses_only_online_numeric_cpu_samples(self):
        body = function_body(self.js, "averageSample")

        self.assertIn("d.online", body)
        self.assertIn("isNumericSample(d[field])", body)
        self.assertIn("samples.length", body)

        numeric = function_body(self.js, "isNumericSample")
        self.assertIn("typeof value === 'number'", numeric)
        self.assertIn("Number.isFinite", numeric)

    def test_averages_clamp_samples_like_the_metric_bars(self):
        # The local cgroup CPU reading can exceed 100% on a multi-core host.
        # Averages must clamp each sample just as the device bars do, or a
        # kind chip can read "125% cpu".
        body = function_body(self.js, "averageSample")

        self.assertIn("clampPct(d[field])", body)
        self.assertIn(
            "Math.max(0, Math.min(100, value))",
            function_body(self.js, "clampPct"),
        )

    def test_dashboard_loads_the_shared_stack_helper_before_app_js(self):
        self.assertLess(
            self.html.index('src="stack.js"'),
            self.html.index('src="app.js"'),
        )
        self.assertNotIn("kind-chips", self.html)

    def test_network_tree_nests_pve_guests_and_the_docker_engine(self):
        body = function_body(self.js, "networkTree")

        self.assertIn("dashboardStack.group(devices, topology)", body)
        self.assertIn("'PVE host'", body)
        self.assertIn("'LXC guests'", body)
        self.assertIn("'Elsewhere'", body)
        self.assertIn("dashboardStack.dockerEngine(stack.containers)", body)
        self.assertIn("sortNetworkDevices(stack.others)", body)
        # Standalone machines must not be folded into the pve branches.
        self.assertIn("stack.others", body)

    def test_shared_stack_helper_trusts_the_server_topology(self):
        stack = (APP_JS.parent / "stack.js").read_text()
        body = function_body(stack, "group")

        self.assertIn("topology.host_id", body)
        self.assertIn("topology.docker_host_id", body)
        self.assertIn("topology.containers", body)
        self.assertIn("device.kind === 'lxc'", body)
        self.assertIn("device.kind === 'lxc-guest'", body)

    def test_docker_engine_names_containers_and_carries_state_as_text(self):
        stack = (APP_JS.parent / "stack.js").read_text()
        engine = function_body(stack, "dockerEngine")
        row = function_body(stack, "containerRow")

        self.assertIn("containerRow", engine)
        self.assertIn(" running", engine)
        self.assertIn("docker-glyph", engine)
        self.assertIn("docker-container-name", row)
        # Colour alone must not carry container health.
        self.assertIn("docker-container-role", row)
        self.assertIn("state", row)

    def test_unknown_kinds_get_a_readable_label(self):
        body = function_body(self.js, "kindLabel")

        self.assertIn("KIND_LABELS[kind]", body)
        self.assertIn("replace(/[-_]+/g, ' ')", body)
        self.assertIn("toUpperCase", body)
        self.assertIn("'Other'", body)

    def test_kind_labels_and_order_share_one_definition(self):
        # Adding a kind must edit one list, not two parallel constants that can
        # drift apart.
        self.assertRegex(self.js, r"KIND_DEFS\s*=\s*\[")
        defs = re.search(r"KIND_DEFS\s*=\s*\[(.*?)\];", self.js, re.S)
        self.assertIsNotNone(defs, "KIND_DEFS not found")
        self.assertIn("label:", defs.group(1))
        self.assertIn("key:", defs.group(1))

        self.assertRegex(self.js, r"KIND_LABELS\s*=\s*Object\.fromEntries\(")
        self.assertRegex(self.js, r"KIND_ORDER\s*=\s*KIND_DEFS\.map\(")

    def test_device_key_helper_is_used_for_rows_and_poll_identity(self):
        key = function_body(self.js, "deviceKey")
        self.assertIn("d.id || d.name", key)
        self.assertIn("deviceKey(d)", function_body(self.js, "deviceRow"))
        self.assertIn("deviceKey(d)", function_body(self.js, "networkKey"))

    def test_metric_warning_threshold_is_eighty_percent_amber(self):
        self.assertRegex(self.js, r"METRIC_WARN_PCT\s*=\s*80\b")

        cell = function_body(self.js, "metricCell")
        self.assertIn("value >= METRIC_WARN_PCT", cell)
        self.assertIn('data-metric="', cell)
        self.assertIn('class="metric-fill"', cell)

        self.assertRegex(self.css, r"\.metric\.is-warn \.metric-value\s*\{[^}]*#ed8936")
        self.assertRegex(self.css, r"\.metric\.is-warn \.metric-fill\s*\{[^}]*#ed8936")
        self.assertRegex(self.css, r"\.metric-bar\s*\{[^}]*height:\s*3px")

    def test_device_rows_show_status_dot_name_sub_and_three_metrics(self):
        body = function_body(self.js, "deviceRow")

        self.assertIn('class="device-dot', body)
        self.assertIn("escapeHtml(d.name)", body)
        self.assertIn("deviceSub(d)", body)

        # One metric definition drives both the full row render and the poll
        # patch, so a new metric is added in one place.
        self.assertRegex(self.js, r"DEVICE_METRICS\s*=\s*\[")
        metrics = re.search(r"DEVICE_METRICS\s*=\s*\[(.*?)\];", self.js, re.S)
        self.assertIsNotNone(metrics, "DEVICE_METRICS not found")
        self.assertIn("'CPU'", metrics.group(1))
        self.assertIn("'RAM'", metrics.group(1))
        self.assertIn("'DSK'", metrics.group(1))
        self.assertIn("DEVICE_METRICS", body)
        self.assertIn("DEVICE_METRICS", function_body(self.js, "patchNetwork"))

    def test_offline_and_online_rows_state_status_in_text_as_well_as_the_dot(self):
        sub = function_body(self.js, "deviceSub")

        # Colour alone is not an accessible status: the row text carries it too.
        self.assertIn("' · offline'", sub)
        self.assertIn("' · online'", sub)
        # The dot remains alongside the text.
        self.assertIn("device-dot", function_body(self.js, "deviceRow"))

    def test_offline_rows_show_last_seen_and_online_rows_show_uptime(self):
        body = function_body(self.js, "deviceSub")

        self.assertIn("!d.online", body)
        self.assertIn("d.last_seen", body)
        self.assertIn("fmtAgo(", body)
        self.assertIn("d.uptime_s", body)
        self.assertIn("fmtUptime(", body)

    def test_device_error_is_rendered_inline(self):
        body = function_body(self.js, "deviceRow")

        self.assertIn('class="device-error"', body)
        self.assertIn("escapeHtml(d.error || '')", body)
        self.assertRegex(self.css, r"\.device-error\s*\{[^}]*#e53e3e")

    def test_failed_poll_keeps_last_good_data_and_shows_a_stale_indicator(self):
        body = function_body(self.js, "refreshDevices")

        self.assertIn("setNetworkStale(true)", body)
        self.assertIn("renderHealth(networkDevices", body)
        # A failed poll must not clear the last good reading or the panel.
        self.assertNotIn("networkDevices = []", body)
        self.assertNotIn("renderNetwork([])", body)
        self.assertNotIn("innerHTML = ''", body)

        stale = function_body(self.js, "setNetworkStale")
        self.assertIn("networkStaleEl.hidden", stale)
        self.assertIn("classList.toggle('is-stale'", stale)

        self.assertIn('id="network-stale"', self.html)
        self.assertRegex(self.css, r"\.network-stale\s*\{")
        self.assertRegex(self.css, r"\.device-grid\.is-stale\s*\{")

    def test_poll_patches_metrics_in_place_without_rebuilding_the_rows(self):
        patch = function_body(self.js, "patchNetwork")
        self.assertNotIn("innerHTML", patch)
        self.assertIn(".device-row", patch)
        self.assertIn("querySelector", patch)
        self.assertIn("textContent", patch)

        metric = function_body(self.js, "patchMetric")
        self.assertNotIn("innerHTML", metric)
        self.assertIn("style.width", metric)
        self.assertIn("classList.toggle('is-warn'", metric)

        apply = function_body(self.js, "applyNetworkDevices")
        self.assertIn("networkKey(devices)", apply)
        self.assertIn("renderNetwork(", apply)
        self.assertIn("patchNetwork(", apply)

        # Polling uses setTimeout with backoff, and pauses while the tab is
        # hidden, rather than a fixed interval that keeps waking a background
        # tab.
        schedule = function_body(self.js, "scheduleRefresh")
        self.assertIn("setTimeout(", schedule)
        self.assertIn("document.hidden", schedule)
        self.assertIn("10000", schedule)
        self.assertIn("Math.min(60000", schedule)
        self.assertIn("visibilitychange", self.js)
        self.assertNotIn("setInterval(refreshDevices", self.js)

    def test_updated_label_uses_a_locale_independent_24_hour_clock(self):
        body = function_body(self.js, "updatedLabel")

        self.assertIn("'updated '", body)
        # getHours/getMinutes are 24-hour regardless of the browser locale; a
        # 12-hour locale would still print HH:MM. toLocaleTimeString would not.
        self.assertIn("getHours()", body)
        self.assertIn("getMinutes()", body)
        self.assertIn("padStart(2, '0')", body)
        self.assertNotIn("toLocaleTimeString", body)
        self.assertNotIn("hour12", body)

    def test_devices_are_ordered_by_kind_only_and_keep_server_order_within_one(self):
        body = function_body(self.js, "sortNetworkDevices")

        self.assertIn("kindRank(", body)
        # Re-ordering inside a kind invents an order the server did not send.
        self.assertNotIn("localeCompare", body)
        self.assertNotIn("a.name", body)
        self.assertNotIn("b.name", body)

    def test_tree_and_compact_rows_fit_the_rail_without_page_scroll(self):
        self.assertRegex(self.css, r"\.network-branch\s*\{[^}]*border-left")
        self.assertRegex(self.css, r"\.network-branch\.is-host\s*\{[^}]*--tree-host")
        self.assertRegex(self.css, r"\.device-row\s*\{[^}]*padding:\s*5px 0")
        self.assertRegex(self.css, r"\.device-row\s*\{[^}]*repeat\(3,\s*46px\)")
        self.assertNotRegex(self.css, r"\.device-grid\s*\{[^}]*max-height")


class MetricsHistoryTest(ApiTestCase):
    """The server-side history store, its derived signals and its endpoints."""

    # Aligned to an hour so every 60 s sample lands in the intended bucket.
    BASE = int(NOW) - (int(NOW) % 3600)

    def device(self, device_id="docker01", name=None, kind="lxc", online=True,
               cpu=10.0, mem=40.0, disk=50.0, uptime=1000, **extra):
        record = {
            "id": device_id,
            "name": name or device_id,
            "kind": kind,
            "online": online,
            "cpu_pct": cpu,
            "mem_pct": mem,
            "mem_used": 400,
            "mem_total": 1000,
            "disk_pct": disk,
            "uptime_s": uptime,
            "last_seen": self.BASE,
        }
        record.update(extra)
        return record

    def sample(self, devices, now=None):
        return app.record_samples(devices, now=self.BASE if now is None else now)

    def stored_samples(self):
        con = app._connect_metrics()
        try:
            return con.execute(
                "SELECT device_id, ts, cpu_pct, online FROM samples ORDER BY ts"
            ).fetchall()
        finally:
            con.close()

    def get_status(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=5) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def test_one_bucketed_row_per_device_and_a_repeat_within_the_bucket_updates_it(self):
        self.sample([self.device(cpu=10.0)], now=self.BASE)
        self.sample([self.device(cpu=22.0)], now=self.BASE + 10)

        rows = self.stored_samples()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][2], 22.0)

        payload = app.metrics_series("docker01", "1h", 60, now=self.BASE + 60)
        self.assertEqual(len(payload["points"]), 1)
        self.assertEqual(payload["points"][0][1], 22.0)

    def test_non_numeric_agent_values_are_stored_as_missing(self):
        self.sample([self.device(cpu="99", mem=True, disk=None)])

        payload = app.metrics_series("docker01", "1h", 60, now=self.BASE + 60)
        _ts, cpu, mem, disk, _online = payload["points"][0]
        self.assertIsNone(cpu)
        self.assertIsNone(mem)
        self.assertIsNone(disk)

    def test_offline_run_is_availability_downtime_and_an_event(self):
        pve = "agent:pve"
        for offset, online, uptime in (
            (0, True, 1000), (60, True, 1060), (120, False, 1120),
            (180, False, 1180), (240, True, 1240),
        ):
            self.sample([self.device(pve, "pve", "host", online=online, uptime=uptime)],
                        now=self.BASE + offset)

        summary = app.metrics_summary("1h", now=self.BASE + 240)["devices"][0]
        self.assertEqual(summary["samples"], 5)
        self.assertEqual(summary["availability_pct"], 60.0)
        self.assertEqual(summary["offline_events"], 1)
        self.assertEqual(summary["downtime_s"], 120)
        self.assertEqual(summary["last_offline"], self.BASE + 120)

        payload = app.metrics_series(pve, "1h", 60, now=self.BASE + 240)
        self.assertEqual(
            [(event["type"], event["ts"]) for event in payload["events"]],
            [("online", self.BASE + 240), ("offline", self.BASE + 120)],
        )
        self.assertEqual(payload["events"][0]["down_s"], 120)

    def test_a_dropped_uptime_is_a_restart_not_an_offline_event(self):
        pve = "agent:pve"
        self.sample([self.device(pve, uptime=5000)], now=self.BASE)
        self.sample([self.device(pve, uptime=5050)], now=self.BASE + 60)
        self.sample([self.device(pve, uptime=40)], now=self.BASE + 120)

        summary = app.metrics_summary("1h", now=self.BASE + 120)["devices"][0]
        self.assertEqual(summary["restarts"], 1)
        self.assertEqual(summary["offline_events"], 0)

    def test_a_restart_across_an_offline_gap_is_still_detected(self):
        pve = "agent:pve"
        self.sample([self.device(pve, uptime=5000)], now=self.BASE)
        self.sample([self.device(pve, online=False, uptime=5060)], now=self.BASE + 60)
        self.sample([self.device(pve, uptime=30)], now=self.BASE + 120)

        summary = app.metrics_summary("1h", now=self.BASE + 120)["devices"][0]
        self.assertEqual(summary["restarts"], 1)
        self.assertEqual(summary["offline_events"], 1)

    def test_disk_growth_is_a_linear_trend_and_projects_days_to_90(self):
        for offset, disk in ((0, 50.0), (3600, 50.5), (7200, 51.0)):
            self.sample([self.device(disk=disk)], now=self.BASE + offset)
        # The sampler buckets to a minute; these are whole hours, so no overlap.
        summary = app.metrics_summary("24h", now=self.BASE + 7200)["devices"][0]
        self.assertAlmostEqual(summary["disk_growth_per_day"], 12.0, places=2)
        self.assertAlmostEqual(summary["days_to_90"], 3.2, places=1)

    def test_retention_prunes_samples_older_than_the_history_window(self):
        self.sample([self.device()], now=self.BASE - app.HISTORY_SECONDS - 600)
        self.assertEqual(len(self.stored_samples()), 1)

        app._last_prune["at"] = 0.0  # force the hourly prune to run now
        self.sample([self.device()], now=self.BASE)

        rows = self.stored_samples()
        self.assertEqual(len(rows), 1)
        self.assertGreaterEqual(rows[0][1], self.BASE - 60)

    def test_step_is_clamped_and_buckets_average_the_samples_inside_them(self):
        for index in range(6):
            self.sample([self.device(cpu=float(index * 10))], now=self.BASE + index * 60)

        payload = app.metrics_series("docker01", "1h", 180, now=self.BASE + 300)
        self.assertEqual(payload["step"], 180)
        self.assertEqual(len(payload["points"]), 2)
        self.assertEqual(payload["points"][0][1], 10.0)  # 0, 10, 20
        self.assertEqual(payload["points"][1][1], 40.0)  # 30, 40, 50

        tiny = app.metrics_series("docker01", "1h", 1, now=self.BASE + 300)
        self.assertEqual(tiny["step"], 60)

    def test_a_step_too_fine_for_the_window_is_raised_to_cap_the_point_count(self):
        step = app._step_for(app.METRIC_WINDOWS["30d"], 60)
        self.assertLessEqual(app.METRIC_WINDOWS["30d"] / step, app.MAX_POINTS)
        self.assertGreaterEqual(step, 60)

    def test_metrics_endpoints_validate_window_device_and_step(self):
        self.sample([self.device()], now=time.time())

        self.assertEqual(self.get_status("/api/metrics/summary?window=2h")[0], 400)
        self.assertEqual(self.get_status("/api/metrics?device=docker01&window=2h")[0], 400)
        self.assertEqual(self.get_status("/api/metrics?window=1h")[0], 400)
        self.assertEqual(self.get_status("/api/metrics?device=docker01&window=1h&step=x")[0], 400)
        self.assertEqual(self.get_status("/api/metrics?device=nope&window=1h")[0], 404)

        status, body = self.get_status("/api/metrics?device=docker01&window=1h")
        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(payload["device"]["id"], "docker01")
        self.assertEqual(payload["window"], "1h")
        self.assertTrue(payload["points"])

    def test_summary_returns_every_device_with_a_sparkline(self):
        self.sample([
            self.device("docker01", cpu=10.0),
            self.device("agent:pve", "pve", "host", cpu=20.0),
        ])

        payload = app.metrics_summary("1h", now=self.BASE + 60)
        self.assertEqual({device["id"] for device in payload["devices"]},
                         {"docker01", "agent:pve"})
        for device in payload["devices"]:
            self.assertEqual(len(device["spark"]), 12)

    def test_stats_stamps_24h_availability_from_the_history_store(self):
        for offset, online in ((0, True), (60, True), (120, False)):
            self.sample([self.device(online=online)], now=self.BASE + offset)
        app._cache.update(at=0.0, body=None)
        app._availability_cache.update(at=0.0, by_id={})

        with frozen_time(self.BASE + 180):
            payload = app.stats()

        docker = next(device for device in payload["devices"] if device["id"] == "docker01")
        self.assertEqual(docker["availability_24h"], 66.7)

    def test_summary_carries_the_same_stack_topology_as_stats(self):
        self.sample([
            self.device(device_id="docker01", kind="lxc"),
            self.device(device_id="agent:pve", name="pve", kind="host"),
        ])

        payload = app.metrics_summary("1h", now=self.BASE + 60)

        self.assertEqual(payload["topology"]["host_id"], "agent:pve")
        self.assertEqual(payload["topology"]["docker_host_id"], "docker01")
        self.assertEqual(
            [container["name"] for container in payload["topology"]["containers"]],
            ["caddy", "dash"],
        )

    def test_a_fresh_install_returns_an_empty_summary_without_creating_a_database(self):
        payload = app.metrics_summary("24h", now=self.BASE)
        self.assertEqual(payload["devices"], [])
        self.assertEqual(
            payload["topology"],
            {"host_id": None, "docker_host_id": None, "containers": []},
        )
        self.assertEqual(app.metrics_series("docker01", "24h", None, now=self.BASE), None)

    # ---------- per-volume history ----------

    def stored_disk_samples(self):
        con = app._connect_metrics()
        try:
            return con.execute(
                "SELECT device_id, mount, ts, label, total, used, pct "
                "FROM disk_samples ORDER BY ts, mount"
            ).fetchall()
        finally:
            con.close()

    def volume(self, mount, pct, label="", total=1000, used=None):
        return {
            "mount": mount,
            "label": label,
            "total": total,
            "used": int(total * pct / 100.0) if used is None else used,
            "pct": pct,
        }

    def test_a_push_with_disks_writes_a_row_per_volume(self):
        body = {
            "name": "volpc", "kind": "pc", "cpu_pct": 5.0, "mem_pct": 20.0,
            "mem_used": 1, "mem_total": 2, "disk_pct": 91.0, "uptime_s": 10,
            "disks": [
                self.volume("/", 10.0, "System", 1000, 100),
                self.volume("/mnt/data", 91.0, "Data", 2000, 1820),
            ],
        }
        status, _ = self.post(json.dumps(body).encode(), path="/api/push/volpc")
        self.assertEqual(status, 200)

        app.record_samples(app.agent_devices(), now=self.BASE)

        rows = self.stored_disk_samples()
        self.assertEqual(
            [(row[0], row[1], row[3], row[4], row[5], row[6]) for row in rows],
            [
                ("agent:volpc", "/", "System", 1000, 100, 10.0),
                ("agent:volpc", "/mnt/data", "Data", 2000, 1820, 91.0),
            ],
        )

    def test_a_repeat_in_the_same_bucket_replaces_that_bucket_s_volumes(self):
        self.sample([self.device(disks=[
            self.volume("/", 10.0, "System"),
            self.volume("/mnt/data", 91.0, "Data"),
        ])], now=self.BASE)
        self.sample([self.device(disks=[self.volume("/", 40.0, "System")])],
                    now=self.BASE + 10)

        rows = self.stored_disk_samples()
        self.assertEqual([(row[1], row[6]) for row in rows], [("/", 40.0)])

    def test_malformed_volume_entries_are_dropped(self):
        self.sample([self.device(disks=[
            self.volume("/", 10.0, "System"),
            {"mount": "", "total": 100, "used": 1, "pct": 1.0},
            {"mount": "/bad-total", "total": 0, "used": 0, "pct": 0.0},
            {"mount": "/bad-pct", "total": 100},
            {"mount": "/bool", "total": True, "used": True, "pct": True},
            "not-a-volume",
            None,
        ])], now=self.BASE)

        rows = self.stored_disk_samples()
        self.assertEqual([row[1] for row in rows], ["/"])

    def test_oversized_volume_lists_are_capped(self):
        disks = [self.volume("/vol%02d" % index, 10.0 + index, "V%d" % index)
                 for index in range(20)]
        self.sample([self.device(disks=disks)], now=self.BASE)
        self.assertEqual(len(self.stored_disk_samples()), app.MAX_DISKS)

    def test_retention_prunes_old_volume_rows_too(self):
        self.sample([self.device(disks=[self.volume("/", 50.0)])],
                    now=self.BASE - app.HISTORY_SECONDS - 600)
        self.assertEqual(len(self.stored_disk_samples()), 1)

        app._last_prune["at"] = 0.0
        self.sample([self.device(disks=[self.volume("/", 50.0)])], now=self.BASE)

        rows = self.stored_disk_samples()
        self.assertEqual(len(rows), 1)
        self.assertGreaterEqual(rows[0][2], self.BASE - 60)

    def test_volume_history_survives_a_reconnect(self):
        self.sample([self.device(disks=[self.volume("/", 50.0)])], now=self.BASE)
        # Every read opens its own connection and re-applies the schema
        # idempotently; the row must still be there afterwards.
        self.assertEqual(len(self.stored_disk_samples()), 1)
        self.assertEqual(len(self.stored_disk_samples()), 1)

    def test_metrics_series_adds_bucketed_volumes_and_their_growth(self):
        for offset, pct in ((0, 50.0), (3600, 50.5), (7200, 51.0)):
            self.sample([self.device(disk=pct, disks=[
                self.volume("/", pct, "System", 1000, int(pct * 10)),
                self.volume("/mnt/data", 80.0, "Data", 2000, 1600),
            ])], now=self.BASE + offset)

        payload = app.metrics_series("docker01", "24h", 3600, now=self.BASE + 7200)

        # The existing aggregate payload keeps its shape.
        self.assertIsInstance(payload["points"], list)
        self.assertEqual(len(payload["points"][0]), 5)

        volumes = {volume["mount"]: volume for volume in payload["volumes"]}
        self.assertEqual(set(volumes), {"/", "/mnt/data"})
        root = volumes["/"]
        self.assertEqual(root["label"], "System")
        self.assertEqual(root["total"], 1000)
        self.assertEqual(root["used"], 510)
        self.assertEqual(root["pct"], 51.0)
        self.assertEqual([point[0] for point in root["points"]],
                         [self.BASE, self.BASE + 3600, self.BASE + 7200])
        self.assertEqual([point[1] for point in root["points"]], [50.0, 50.5, 51.0])
        self.assertAlmostEqual(root["growth_per_day"], 12.0, places=2)
        self.assertAlmostEqual(root["days_to_90"], 3.2, places=1)

        data = volumes["/mnt/data"]
        self.assertEqual(data["growth_per_day"], 0.0)
        self.assertIsNone(data["days_to_90"])
        # The fullest volume leads.
        self.assertEqual(payload["volumes"][0]["mount"], "/mnt/data")

    def test_metrics_summary_adds_a_compact_volume_summary(self):
        for offset, pct in ((0, 50.0), (3600, 50.5), (7200, 51.0)):
            self.sample([self.device(disk=pct, disks=[
                self.volume("/", pct, "System", 1000, int(pct * 10)),
                self.volume("/mnt/data", 80.0, "Data", 2000, 1600),
            ])], now=self.BASE + offset)

        summary = app.metrics_summary("24h", now=self.BASE + 7200)["devices"][0]
        volumes = summary["volumes"]
        self.assertEqual(volumes["count"], 2)
        fullest = volumes["fullest"]
        self.assertEqual(fullest["mount"], "/mnt/data")
        self.assertEqual(fullest["label"], "Data")
        self.assertEqual(fullest["pct"], 80.0)
        self.assertEqual(fullest["total"], 2000)
        self.assertEqual(fullest["growth_per_day"], 0.0)
        self.assertIsNone(fullest["days_to_90"])
        # The existing single-gauge summary is unchanged.
        self.assertEqual(summary["disk_pct"], 51.0)

    def test_a_device_without_volumes_reports_empty_volume_fields(self):
        self.sample([self.device()], now=self.BASE)
        summary = app.metrics_summary("1h", now=self.BASE + 60)["devices"][0]
        self.assertEqual(summary["volumes"], {"count": 0, "fullest": None})
        series_payload = app.metrics_series("docker01", "1h", 60, now=self.BASE + 60)
        self.assertEqual(series_payload["volumes"], [])

    # ---------- peaks and rollups ----------

    def rollup_rows(self, table, columns="*"):
        con = app._connect_metrics()
        try:
            return con.execute(
                f"SELECT {columns} FROM {table} ORDER BY ts"  # fixed internal names
            ).fetchall()
        finally:
            con.close()

    def test_the_sampler_writes_five_minute_and_hourly_rollups(self):
        for index, cpu in enumerate((10.0, 90.0, 50.0)):
            self.sample([self.device(cpu=cpu, mem=20.0, disk=50.0)],
                        now=self.BASE + index * 60)

        five = self.rollup_rows(
            "rollup_5m",
            "device_id, ts, samples, online_samples, cpu_avg, cpu_min, cpu_max",
        )
        self.assertEqual(len(five), 1)
        self.assertEqual(five[0][:4], ("docker01", self.BASE, 3, 3))
        self.assertAlmostEqual(five[0][4], 50.0, places=1)
        self.assertEqual(five[0][5], 10.0)
        self.assertEqual(five[0][6], 90.0)

        hour = self.rollup_rows(
            "rollup_1h", "ts, samples, online_samples, cpu_min, cpu_max"
        )
        self.assertEqual(len(hour), 1)
        self.assertEqual(hour[0][0], self.BASE)
        self.assertEqual(hour[0][1:], (3, 3, 10.0, 90.0))

    def test_metrics_series_bands_keep_min_and_max_per_bucket(self):
        for index, cpu in enumerate((10.0, 90.0)):
            self.sample([self.device(cpu=cpu)], now=self.BASE + index * 60)

        payload = app.metrics_series("docker01", "1h", 300, now=self.BASE + 120)

        self.assertEqual(payload["source"], "raw")
        self.assertEqual(len(payload["points"]), 1)
        # The old five-tuple is untouched; the average is still the point.
        self.assertEqual(payload["points"][0][1], 50.0)
        self.assertEqual(len(payload["bands"]["cpu"]), 1)
        ts, low, high = payload["bands"]["cpu"][0]
        self.assertEqual(ts, self.BASE)
        self.assertEqual(low, 10.0)
        self.assertEqual(high, 90.0)
        for key in ("cpu", "mem", "disk"):
            self.assertIn(key, payload["bands"])

    def test_long_windows_read_five_minute_rollups_not_raw_rows(self):
        now = self.BASE + 7200
        for offset, cpu in ((0, 10.0), (3600, 20.0)):
            self.sample([self.device(cpu=cpu)], now=now - 7200 + offset)

        con = app._connect_metrics()
        try:
            con.execute("DELETE FROM samples")  # keep only the rollups
            con.commit()
        finally:
            con.close()

        payload = app.metrics_series("docker01", "7d", 3600, now=now)
        self.assertEqual(payload["source"], "5m")
        self.assertEqual([point[1] for point in payload["points"]], [10.0, 20.0])
        self.assertGreaterEqual(payload["summary"]["samples"], 2)
        self.assertEqual(len(payload["bands"]["cpu"]), len(payload["points"]))

    def test_windows_beyond_thirty_days_read_hourly_rollups(self):
        now = self.BASE + 7200
        self.sample([self.device(cpu=42.0)], now=self.BASE)

        con = app._connect_metrics()
        try:
            con.execute("DELETE FROM samples")
            con.execute("DELETE FROM rollup_5m")
            con.execute("DELETE FROM disk_rollup_5m")
            con.commit()
        finally:
            con.close()

        payload = app.metrics_series("docker01", "90d", 21600, now=now)
        self.assertEqual(payload["source"], "1h")
        self.assertEqual([point[1] for point in payload["points"]], [42.0])

    def test_a_long_summary_does_not_need_raw_rows(self):
        now = self.BASE + 7200
        self.sample([self.device(cpu=20.0, online=True)], now=self.BASE)
        self.sample([self.device(cpu=40.0, online=False)], now=self.BASE + 3600)

        con = app._connect_metrics()
        try:
            con.execute("DELETE FROM samples")
            con.commit()
        finally:
            con.close()

        summary = app.metrics_summary("7d", now=now)["devices"][0]
        self.assertEqual(summary["source"], "5m")
        self.assertEqual(summary["samples"], 2)
        self.assertEqual(summary["availability_pct"], 50.0)
        self.assertEqual(len(summary["spark"]), 12)

    def test_long_summary_reports_offline_runs_and_restarts_from_rollups(self):
        # One sample a minute for 15 minutes: two full 5-minute online runs
        # around one full offline run, then a reboot in the final run.
        now = self.BASE + 900
        for minute in range(15):
            online = not (5 <= minute < 10)
            uptime = 1000 + minute * 60 if minute < 10 else 30 + minute * 60
            self.sample([self.device(online=online, uptime=uptime)],
                        now=self.BASE + minute * 60)

        con = app._connect_metrics()
        try:
            con.execute("DELETE FROM samples")
            con.commit()
        finally:
            con.close()

        summary = app.metrics_summary("7d", now=now)["devices"][0]
        self.assertEqual(summary["availability_pct"], 66.7)
        self.assertEqual(summary["offline_events"], 1)
        self.assertEqual(summary["restarts"], 1)
        self.assertEqual(summary["downtime_s"], 300)

    def test_summary_is_cached_until_a_write_invalidates_it(self):
        self.sample([self.device(cpu=10.0)], now=self.BASE)
        with frozen_time(self.BASE + 60):
            first = app.metrics_summary("24h")
            cached = app.metrics_summary("24h")
            self.assertIs(first, cached)
            # An explicit clock always computes a fresh payload.
            explicit = app.metrics_summary("24h", now=self.BASE + 60)
            self.assertIsNot(explicit, first)
            # A write invalidates the live cache instead of hiding a new value.
            # Same bucket, so this replaces the row the window already sees.
            self.sample([self.device(cpu=90.0)], now=self.BASE)
            refreshed = app.metrics_summary("24h")
        self.assertNotEqual(first["devices"], refreshed["devices"])

    def test_retention_prunes_each_rollup_tier_on_its_own_window(self):
        old = self.BASE - 10 * 86400
        self.sample([self.device(disks=[self.volume("/", 50.0)])], now=old)

        with mock.patch.object(app, "ROLLUP_5M_RETENTION", 5 * 86400):
            app._last_prune["at"] = 0.0
            self.sample([self.device(disks=[self.volume("/", 50.0)])], now=self.BASE)

        self.assertEqual(len(self.rollup_rows("rollup_5m")), 1)  # new bucket only
        self.assertEqual(len(self.rollup_rows("rollup_1h")), 2)  # 1 h keeps both
        self.assertEqual(len(self.rollup_rows("disk_rollup_5m")), 1)
        self.assertEqual(len(self.rollup_rows("disk_rollup_1h")), 2)
        # Raw retention is still 30 d, so both raw rows survive.
        self.assertEqual(len(self.stored_disk_samples()), 2)

    def test_the_new_range_windows_are_accepted(self):
        self.sample([self.device()], now=time.time())
        for window in ("90d", "1y"):
            self.assertEqual(
                self.get_status("/api/metrics/summary?window=" + window)[0], 200
            )
            status, body = self.get_status(
                "/api/metrics?device=docker01&window=" + window
            )
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["window"], window)

    # ---------- extra signals ----------

    def stored_metric_samples(self):
        con = app._connect_metrics()
        try:
            return con.execute(
                "SELECT metric, ts, value FROM metric_samples ORDER BY ts, metric"
            ).fetchall()
        finally:
            con.close()

    def signal_values(self):
        return {metric: value for metric, _ts, value in self.stored_metric_samples()}

    def test_a_push_with_extra_signals_writes_generic_metric_rows(self):
        body = {
            "name": "sigpc", "kind": "pc", "cpu_pct": 5.0, "mem_pct": 20.0,
            "mem_used": 1, "mem_total": 2, "disk_pct": 10.0, "uptime_s": 10,
            "load_1": 0.8, "load_5": 0.6, "load_15": 0.5,
            "swap_used": 100, "swap_total": 400,
            "net_rx_bps": 1200, "net_tx_bps": 300,
            "cpu_iowait": 1.5, "cpu_steal": 0.2,
        }
        status, _ = self.post(json.dumps(body).encode(), path="/api/push/sigpc")
        self.assertEqual(status, 200)

        app.record_samples(app.agent_devices(), now=self.BASE)

        self.assertEqual(self.signal_values(), {
            "load_1": 0.8, "load_5": 0.6, "load_15": 0.5,
            "swap_used": 100.0, "swap_total": 400.0,
            "net_rx_bps": 1200.0, "net_tx_bps": 300.0,
            "cpu_iowait": 1.5, "cpu_steal": 0.2,
        })

    def test_a_repeat_in_the_same_bucket_replaces_every_signal(self):
        self.sample([self.device(load_1=0.5)], now=self.BASE)
        self.sample([self.device(load_1=1.5, load_5=1.0)], now=self.BASE + 10)
        self.assertEqual(self.signal_values(),
                         {"load_1": 1.5, "load_5": 1.0})

        # A metric that disappears from the heartbeat disappears from the bucket.
        self.sample([self.device(load_1=2.0)], now=self.BASE + 20)
        self.assertEqual(self.signal_values(), {"load_1": 2.0})

    def test_malformed_extra_signals_are_dropped_or_clamped(self):
        self.sample([self.device(
            load_1=-1, load_5="high", load_15=True,
            net_rx_bps=None, cpu_iowait=250, cpu_steal=-3,
            swap_used=500, swap_total=0,
        )], now=self.BASE)

        self.assertEqual(self.signal_values(), {
            "cpu_iowait": 100.0, "swap_used": 0.0, "swap_total": 0.0,
        })

    def test_an_old_agent_without_signals_still_works(self):
        self.sample([self.device()], now=self.BASE)
        self.assertEqual(self.stored_metric_samples(), [])
        payload = app.metrics_series("docker01", "1h", 60, now=self.BASE + 60)
        self.assertEqual(payload["signals"], {})

    def test_metrics_series_exposes_signal_points_and_a_derived_swap_pct(self):
        for offset, load, rx, used in (
            (0, 0.5, 100, 100), (300, 1.5, 300, 200), (600, 1.0, 200, 300),
        ):
            self.sample([self.device(
                load_1=load, swap_used=used, swap_total=1000,
                net_rx_bps=rx, net_tx_bps=rx / 2, cpu_iowait=2.0,
            )], now=self.BASE + offset)

        signals = app.metrics_series(
            "docker01", "1h", 300, now=self.BASE + 600
        )["signals"]
        self.assertEqual([point[1] for point in signals["load_1"]["points"]],
                         [0.5, 1.5, 1.0])
        self.assertEqual(signals["load_1"]["value"], 1.0)
        self.assertEqual(signals["net_rx_bps"]["value"], 200.0)
        self.assertAlmostEqual(signals["swap"]["pct"], 30.0)
        self.assertEqual(signals["swap"]["used"], 300.0)
        self.assertEqual(signals["swap"]["total"], 1000.0)
        self.assertEqual(signals["cpu_iowait"]["value"], 2.0)
        self.assertNotIn("load_5", signals)
        self.assertNotIn("cpu_steal", signals)

    def test_signal_history_is_capped_even_for_a_long_window(self):
        self.sample([self.device(load_1=9.0)], now=self.BASE - 20 * 86400)
        self.sample([self.device(load_1=1.0)], now=self.BASE)

        signals = app.metrics_series(
            "docker01", "30d", None, now=self.BASE
        )["signals"]
        # Raw signal rows are kept for 30 d, but the strip only asks for 24 h.
        self.assertEqual([point[1] for point in signals["load_1"]["points"]],
                         [1.0])

    def test_summary_adds_the_latest_signal_values(self):
        self.sample([self.device(
            load_1=0.7, swap_used=250, swap_total=1000,
            net_rx_bps=500, cpu_iowait=3.0,
        )], now=self.BASE)

        summary = app.metrics_summary("1h", now=self.BASE + 60)["devices"][0]
        self.assertEqual(summary["signals"], {
            "load_1": 0.7, "net_rx_bps": 500.0,
            "swap_pct": 25.0, "cpu_iowait": 3.0,
        })

    def test_retention_prunes_signal_rows_with_raw_samples(self):
        self.sample([self.device(load_1=1.0)],
                    now=self.BASE - app.HISTORY_SECONDS - 600)
        self.assertEqual(len(self.stored_metric_samples()), 1)

        app._last_prune["at"] = 0.0
        self.sample([self.device(load_1=1.0)], now=self.BASE)

        rows = self.stored_metric_samples()
        self.assertEqual(len(rows), 1)
        self.assertGreaterEqual(rows[0][1], self.BASE - 60)

    # ---------- annotations ----------

    def post_annotation(self, payload, headers=None):
        return self.post(json.dumps(payload).encode(),
                         path="/api/annotations", headers=headers)

    def stored_annotations(self):
        con = app._connect_metrics()
        try:
            return con.execute(
                "SELECT device_id, ts, kind, text FROM annotations ORDER BY ts"
            ).fetchall()
        finally:
            con.close()

    def test_annotation_endpoint_stores_a_note_and_validates_input(self):
        status, body = self.post_annotation({
            "device": "docker01", "kind": "deploy", "text": "v1.2",
            "ts": self.BASE,
        })
        self.assertEqual(status, 201)
        self.assertTrue(json.loads(body)["id"])
        self.assertEqual(self.stored_annotations(),
                         [("docker01", self.BASE, "deploy", "v1.2")])

        self.assertEqual(self.post_annotation({"kind": "party", "text": "x"})[0], 400)
        self.assertEqual(self.post_annotation({"kind": "deploy", "text": "  "})[0], 400)
        self.assertEqual(
            self.post_annotation({"kind": "deploy", "text": "x", "ts": "soon"})[0],
            400,
        )
        self.assertEqual(
            self.post_annotation({"kind": "deploy", "text": "x", "device": 7})[0],
            400,
        )
        oversized = {"kind": "deploy", "text": "x" * 5000}
        self.assertEqual(self.post_annotation(oversized)[0], 413)

    def test_annotations_appear_in_the_window_events_and_as_markers(self):
        self.sample([self.device()], now=self.BASE + 60)
        self.post_annotation({"device": "docker01", "kind": "deploy",
                              "text": "rolled out", "ts": self.BASE})
        self.post_annotation({"kind": "backup", "text": "nightly backup",
                              "ts": self.BASE + 30})
        self.post_annotation({"device": "agent:other", "kind": "note",
                              "text": "other device", "ts": self.BASE + 40})
        self.post_annotation({"device": "docker01", "kind": "note",
                              "text": "too old", "ts": self.BASE - 7200})

        payload = app.metrics_series(
            "docker01", "1h", 60, now=self.BASE + 60
        )
        self.assertEqual(
            [(a["kind"], a["text"], a["auto"]) for a in payload["annotations"]],
            [("deploy", "rolled out", False), ("backup", "nightly backup", False)],
        )
        annotation_events = [
            event for event in payload["events"] if event["type"] == "annotation"
        ]
        self.assertEqual({event["text"] for event in annotation_events},
                         {"rolled out", "nightly backup"})

    def test_detected_restarts_become_auto_annotations(self):
        self.sample([self.device(uptime=5000)], now=self.BASE)
        self.sample([self.device(uptime=40)], now=self.BASE + 60)

        payload = app.metrics_series(
            "docker01", "1h", 60, now=self.BASE + 60
        )
        auto = [a for a in payload["annotations"] if a["auto"]]
        self.assertEqual(len(auto), 1)
        self.assertEqual(auto[0]["kind"], "restart")
        self.assertEqual(auto[0]["ts"], self.BASE + 60)
        self.assertEqual(self.stored_annotations(), [])

    def test_auto_restart_markers_are_bounded(self):
        for index in range(50):
            self.sample([self.device(uptime=1000 if index % 2 == 0 else 10)],
                        now=self.BASE + index * 60)
        payload = app.metrics_series(
            "docker01", "1h", 60, now=self.BASE + 3000
        )
        auto = [a for a in payload["annotations"] if a["auto"]]
        self.assertGreater(len(auto), 0)
        self.assertLessEqual(len(auto), app.MAX_AUTO_ANNOTATIONS)

    # ---------- curated warnings ----------

    def test_warning_levels_cover_the_five_signals(self):
        warnings = app.evaluate_warnings({
            "disk_pct": 96.0,
            "days_to_90": 2.0,
            "disk_growth_per_day": 6.0,
            "mem_pct": 92.0,
            "online": False,
            "last_seen": self.BASE - 7200,
        }, now=self.BASE)
        levels = {warning["code"]: warning["level"] for warning in warnings}
        self.assertEqual(levels, {
            "disk": "critical",
            "days_to_90": "critical",
            "growth": "critical",
            "mem": "warning",
            "offline": "critical",
        })
        for warning in warnings:
            self.assertTrue(warning["text"])

    def test_a_healthy_device_has_no_warnings(self):
        self.assertEqual(app.evaluate_warnings({
            "disk_pct": 40.0, "days_to_90": None,
            "disk_growth_per_day": None, "mem_pct": 30.0,
            "online": True, "last_seen": self.BASE,
        }, now=self.BASE), [])

    def test_summary_and_series_carry_warnings(self):
        device_id = "agent:gone"
        self.sample([self.device(device_id, disk=96.0, mem=95.0, online=False)],
                    now=self.BASE)

        summary = app.metrics_summary("24h", now=self.BASE + 7200)["devices"][0]
        codes = {warning["code"] for warning in summary["warnings"]}
        self.assertEqual(codes, {"disk", "mem", "offline"})

        series = app.metrics_series(
            device_id, "24h", 300, now=self.BASE + 7200
        )
        self.assertEqual(
            {warning["code"] for warning in series["warnings"]},
            {"disk", "mem", "offline"},
        )

    def test_live_warnings_are_the_cheap_rail_subset(self):
        warnings = app.live_warnings({
            "disk_pct": 96.0, "mem_pct": 95.0,
            "online": True, "last_seen": self.BASE,
        }, now=self.BASE)
        self.assertEqual({warning["code"] for warning in warnings},
                         {"disk", "mem"})

        status, _ = self.post(json.dumps({
            "name": "warnpc", "kind": "pc", "disk_pct": 96.0,
            "mem_pct": 95.0, "cpu_pct": 1.0,
        }).encode(), path="/api/push/warnpc")
        self.assertEqual(status, 200)
        app._cache.update(at=0.0, body=None)
        with frozen_time(self.BASE):
            payload = app.stats()
        agent = next(d for d in payload["devices"] if d["id"] == "agent:warnpc")
        self.assertEqual(agent["warning_count"], 2)
        self.assertEqual(agent["warning_level"], "critical")


class MetricsPageSourceTest(FrontEndSourceTest):
    """The metrics page, chart markup and deploy wiring, asserted at the source."""

    def setUp(self):
        super().setUp()
        self.metrics_html = (APP_JS.parent / "metrics.html").read_text()
        self.metrics_js = (APP_JS.parent / "metrics.js").read_text()
        self.metrics_css = (APP_JS.parent / "metrics.css").read_text()

    def test_page_loads_the_shared_stack_helper_before_metrics_js(self):
        self.assertLess(
            self.metrics_html.index('src="stack.js"'),
            self.metrics_html.index('src="metrics.js"'),
        )

    def test_device_rail_uses_the_same_containment_tree_as_the_dashboard(self):
        body = function_body(self.metrics_js, "metricsTree")

        self.assertIn("dashboardStack.group(devices, summary && summary.topology)", body)
        # A container row is the only handle on a container's series, so the
        # tree resolves each Docker row back to its device.
        self.assertIn("dashboardStack.dockerEngine(stack.containers,", body)
        self.assertIn("device.kind === 'container'", body)
        self.assertIn("'PVE host'", body)
        self.assertIn("'LXC guests'", body)
        self.assertIn("'Elsewhere'", body)

    def test_page_has_range_tabs_a_chart_a_table_view_and_the_device_rail(self):
        self.assertIn('id="range-tabs"', self.metrics_html)
        for window in ("1h", "6h", "24h", "7d", "30d"):
            self.assertIn('data-range="' + window + '"', self.metrics_html)
        self.assertIn('id="chart"', self.metrics_html)
        self.assertIn('id="table-view"', self.metrics_html)
        self.assertIn('id="device-list"', self.metrics_html)
        self.assertIn('id="events"', self.metrics_html)
        self.assertIn("metrics.js", self.metrics_html)
        self.assertIn("metrics.css", self.metrics_html)

    def test_chart_has_three_series_a_legend_and_a_table_fallback(self):
        self.assertRegex(self.metrics_js, r"SERIES\s*=\s*\[")
        for key in ("cpu", "ram", "disk"):
            self.assertIn("key: '" + key + "'", self.metrics_js)
        self.assertIn("chart-legend", self.metrics_js)
        self.assertIn("renderTable", self.metrics_js)
        self.assertIn("chart-table", self.metrics_html)
        # Identity is never colour alone: the legend names and values each line.
        self.assertIn("legend-label", self.metrics_js)

    def test_lines_are_two_pixels_with_surface_ringed_dots_and_hairline_grid(self):
        self.assertRegex(self.metrics_css, r"\.chart-line\s*\{[^}]*stroke-width:\s*2px")
        self.assertRegex(self.metrics_css, r"\.chart-dot\s*\{[^}]*stroke:\s*var\(--chart-surface\)")
        self.assertRegex(self.metrics_css, r"\.chart-grid\s*\{[^}]*stroke-width:\s*1px")

    def test_series_colours_are_the_validated_first_three_slots_per_theme(self):
        self.assertIn("--series-cpu: #2a78d6", self.metrics_css)
        self.assertIn("--series-ram: #eb6834", self.metrics_css)
        self.assertIn("--series-disk: #1baf7a", self.metrics_css)
        dark = self.metrics_css.split('[data-theme="personal"]', 1)[1]
        self.assertIn("--series-cpu: #3987e5", dark)
        self.assertIn("--series-ram: #d95926", dark)
        self.assertIn("--series-disk: #199e70", dark)

    def test_chart_is_interactive_with_a_crosshair_tooltip_and_resize_redraw(self):
        self.assertIn("pointermove", self.metrics_js)
        self.assertIn("chart-crosshair", self.metrics_js)
        self.assertIn("chart-tooltip", self.metrics_js)
        self.assertIn("ResizeObserver", self.metrics_js)

    def test_page_fetches_only_the_local_metrics_api(self):
        self.assertIn("'/api/metrics/summary?window='", self.metrics_js)
        self.assertIn("'/api/metrics?device='", self.metrics_js)
        self.assertNotRegex(self.metrics_js, r"https?://")
        self.assertNotIn("document.cookie", self.metrics_js)

    def test_dashboard_rail_links_to_the_page_and_shows_24h_availability(self):
        self.assertIn('href="metrics.html"', self.html)
        availability = function_body(self.source, "deviceAvailability")
        self.assertIn("availability_24h", availability)
        row = function_body(self.source, "deviceRow")
        self.assertIn("deviceAvailability(d)", row)
        self.assertIn("device-avail", row)
        patch = function_body(self.source, "patchNetwork")
        self.assertIn("availabilityEl", patch)
        self.assertIn(".device-avail", patch)
        self.assertRegex(self.css, r"\.device-avail\s*\{")

    def test_deploy_ships_the_metrics_page_and_checks_it(self):
        deploy = (Path(__file__).resolve().parent / "deploy.sh").read_text()
        for name in ("stack.js", "metrics.html", "metrics.js", "metrics.css"):
            self.assertIn(name, deploy)
        smoke = deploy.split('say "smoke"', 1)[1]
        self.assertIn("metrics.html", smoke)
        # A bind-mounted app.py does not reload on its own, so the deploy must
        # recreate the container; the smoke test must also prove the API is
        # live, not just that the static page was installed.
        self.assertIn("--force-recreate", deploy)
        self.assertIn("/api/metrics/summary", smoke)
class MultiDiskTest(FrontEndSourceTest):
    """A PC with more than one drive must show every volume, not just root.

    Single-volume devices keep the compact three-metric row; the breakdown is
    added only when the payload carries more than one disk.
    """

    def test_breakdown_only_appears_for_more_than_one_volume(self):
        body = function_body(self.js, "deviceDisks")

        self.assertIn("Array.isArray(d.disks)", body)
        self.assertIn("disks.length < 2", body)
        self.assertIn("disks.map(diskRow)", body)
        self.assertIn("deviceDisks(d)", function_body(self.js, "deviceRow"))

    def test_disk_row_shows_label_size_bar_and_percentage(self):
        body = function_body(self.js, "diskRow")

        self.assertIn("diskLabel(disk)", body)
        self.assertIn("escapeHtml(", body)
        self.assertIn("fmtBytes(disk.total)", body)
        self.assertIn('data-mount="', body)
        self.assertIn('class="disk-fill"', body)
        self.assertIn("clampPct(disk.pct)", body)
        self.assertIn("METRIC_WARN_PCT", body)
        self.assertIn("'—'", body)

        fmt = function_body(self.js, "fmtBytes")
        self.assertIn("1024", fmt)
        self.assertIn("'TB'", fmt)

    def test_disk_identity_is_part_of_the_row_key_so_names_cannot_go_stale(self):
        key = function_body(self.js, "diskKey")

        self.assertIn("disk.mount", key)
        self.assertIn("disk.label", key)
        self.assertIn("diskKey(d)", function_body(self.js, "networkKey"))
        self.assertIn("deviceKey(d)", function_body(self.js, "networkKey"))

    def test_poll_patches_disk_values_in_place_without_rebuilding_the_rows(self):
        body = function_body(self.js, "patchDisk")

        self.assertNotIn("innerHTML", body)
        self.assertIn("style.width", body)
        self.assertIn("disk-value", body)
        self.assertIn("disk-size", body)

        patch = function_body(self.js, "patchNetwork")
        self.assertIn(".disk-row", patch)
        self.assertIn("patchDisk(", patch)

    def test_each_volume_reads_as_a_compact_line_under_the_device(self):
        self.assertRegex(
            self.css, r"\.device-disks\s*\{[^}]*grid-column:\s*1 / -1"
        )
        self.assertRegex(self.css, r"\.disk-row\s*\{[^}]*grid-template-columns:")
        self.assertRegex(self.css, r"\.disk-bar\s*\{[^}]*height:\s*3px")
        self.assertRegex(self.css, r"\.disk-row\.is-warn \.disk-fill\s*\{[^}]*#ed8936")
        # The base row keeps its three fixed metric cells, so single-disk
        # devices are no taller than before.
        self.assertRegex(self.css, r"\.device-row\s*\{[^}]*repeat\(3,\s*46px\)")


class MetricsVolumeSourceTest(FrontEndSourceTest):
    """Phase 1: per-volume history under the chart and inside the table view."""

    def setUp(self):
        super().setUp()
        self.metrics_html = (APP_JS.parent / "metrics.html").read_text()
        self.metrics_js = (APP_JS.parent / "metrics.js").read_text()
        self.metrics_css = (APP_JS.parent / "metrics.css").read_text()

    def test_volume_table_lives_under_the_chart_and_in_the_table_view(self):
        self.assertIn('id="volume-section"', self.metrics_html)
        self.assertIn('id="volume-table"', self.metrics_html)
        self.assertIn('id="chart-volume-table"', self.metrics_html)
        chart = self.metrics_html.index('id="chart"')
        volumes = self.metrics_html.index('id="volume-section"')
        rail = self.metrics_html.index('class="rail metrics-rail"')
        self.assertLess(chart, volumes)
        self.assertLess(volumes, rail)

    def test_volume_rows_show_label_mount_pct_size_growth_and_days_to_full(self):
        body = function_body(self.metrics_js, "volumeRow")
        for needle in ("volume.mount", "volumeLabel(volume)", "pct(volume.pct)",
                       "fmtBytes(volume.total)", "fmtGrowth(volume.growth_per_day)",
                       "fmtDays(volume.days_to_90)"):
            self.assertIn(needle, body)
        self.assertIn("volume.label", function_body(self.metrics_js, "volumeLabel"))
        growth = function_body(self.metrics_js, "fmtGrowth")
        self.assertIn("/day", growth)

    def test_volume_section_only_appears_for_more_than_one_volume(self):
        body = function_body(self.metrics_js, "renderVolumes")
        self.assertIn("series.volumes", body)
        self.assertIn("volumes.length < 2", body)
        self.assertIn("volumeSectionEl.hidden", body)
        self.assertIn("renderVolumes()", function_body(self.metrics_js, "load"))

    def test_table_view_includes_volume_rows(self):
        body = function_body(self.metrics_js, "renderTable")
        self.assertIn("renderVolumeTable()", body)
        volume_body = function_body(self.metrics_js, "renderVolumeTable")
        self.assertIn("volume.points", volume_body)
        self.assertIn("chartVolumeTableEl", volume_body)

    def test_volume_table_is_compact_and_scrolls_on_a_phone(self):
        self.assertRegex(self.metrics_css, r"\.volume-table\s*\{")
        self.assertRegex(self.metrics_css, r"\.volume-scroll\s*\{[^}]*overflow-x:\s*auto")
        self.assertRegex(self.metrics_css, r"\.volume-table th,\s*\.volume-table td\s*\{")


class MetricsPeaksSourceTest(FrontEndSourceTest):
    """Phase 2: min/max bands and the 90d / 1y ranges."""

    def setUp(self):
        super().setUp()
        self.metrics_html = (APP_JS.parent / "metrics.html").read_text()
        self.metrics_js = (APP_JS.parent / "metrics.js").read_text()
        self.metrics_css = (APP_JS.parent / "metrics.css").read_text()

    def test_the_api_bands_are_drawn_as_a_peak_band_behind_the_lines(self):
        body = function_body(self.metrics_js, "bandPaths")
        self.assertIn("point[4] !== 1", body)
        self.assertIn("isNumber", body)
        render = function_body(self.metrics_js, "renderChart")
        self.assertIn("bandPaths(", render)
        self.assertIn("chart-band", render)
        self.assertRegex(self.metrics_css, r"\.chart-band\s*\{")
        self.assertRegex(self.metrics_css, r"\.chart-band\.is-cpu\s*\{")

    def test_the_table_view_shows_min_average_and_max(self):
        body = function_body(self.metrics_js, "tableCells")
        self.assertIn("series.bands", body)
        self.assertIn("item.band", body)
        self.assertIn("Math.round", body)
        self.assertIn("'–'", body)
        self.assertIn("min–avg–max", self.metrics_html)
        # The RAM series key is 'ram' but the API names the band 'mem'.
        self.assertIn("{ key: 'ram', field: 2, label: 'RAM', band: 'mem' }",
                      self.metrics_js)

    def test_ninety_day_and_one_year_ranges_are_wired_end_to_end(self):
        for window in ("90d", "1y"):
            self.assertIn('data-range="' + window + '"', self.metrics_html)
            self.assertIn("'" + window + "'", self.metrics_js)
        self.assertIn("RANGES = ['1h', '6h', '24h', '7d', '30d', '90d', '1y']",
                      self.metrics_js)


class MetricsSignalsSourceTest(FrontEndSourceTest):
    """Phase 3: load/swap/network/iowait live outside the 0-100 chart."""

    def setUp(self):
        super().setUp()
        self.metrics_html = (APP_JS.parent / "metrics.html").read_text()
        self.metrics_js = (APP_JS.parent / "metrics.js").read_text()
        self.metrics_css = (APP_JS.parent / "metrics.css").read_text()

    def test_signals_have_their_own_strip_outside_the_chart(self):
        self.assertIn('id="signal-strip"', self.metrics_html)
        chart = self.metrics_html.index('id="chart"')
        strip = self.metrics_html.index('id="signal-strip"')
        self.assertGreater(strip, chart)
        body = function_body(self.metrics_js, "renderSignals")
        self.assertIn("series.signals", body)
        self.assertIn("signalTile(", body)
        self.assertIn("signal-strip", self.metrics_css)

    def test_missing_signals_render_absent_never_zero(self):
        body = function_body(self.metrics_js, "renderSignals")
        self.assertIn("isNumber", body)
        self.assertIn("signals.length", body)
        self.assertIn("signalStripEl.hidden", body)
        self.assertNotIn("?? 0", body)
        self.assertNotIn("|| 0", body)

    def test_network_rates_have_their_own_units(self):
        rate = function_body(self.metrics_js, "fmtRate")
        self.assertIn("fmtBytes", rate)
        self.assertIn("/s", rate)
        tiles = function_body(self.metrics_js, "signalTile")
        self.assertIn("fmtRate", tiles)

    def test_signal_strip_is_compact_on_a_phone(self):
        self.assertRegex(self.metrics_css, r"\.signal-strip\s*\{")
        self.assertRegex(self.metrics_css, r"\.signal-tile\s*\{")
        self.assertRegex(self.metrics_css, r"\.signal-spark\s*\{")


class MetricsAnnotationsWarningsSourceTest(FrontEndSourceTest):
    """Phase 4: warning banner, chart markers and annotation Events rows."""

    def setUp(self):
        super().setUp()
        self.metrics_html = (APP_JS.parent / "metrics.html").read_text()
        self.metrics_js = (APP_JS.parent / "metrics.js").read_text()
        self.metrics_css = (APP_JS.parent / "metrics.css").read_text()

    def test_warning_banner_lists_active_warnings_and_hides_when_clean(self):
        self.assertIn('id="warning-banner"', self.metrics_html)
        body = function_body(self.metrics_js, "renderWarnings")
        self.assertIn("device.warnings", body)
        self.assertIn("warningBannerEl.hidden", body)
        self.assertIn("warning.level", body)
        self.assertIn("warning.text", body)
        self.assertIn("renderWarnings()", function_body(self.metrics_js, "load"))

    def test_chart_draws_annotation_markers(self):
        body = function_body(self.metrics_js, "renderChart")
        self.assertIn("series.annotations", body)
        self.assertIn("chart-marker", body)
        self.assertIn("annotation.text", body)

    def test_events_show_manual_annotations(self):
        body = function_body(self.metrics_js, "eventItem")
        self.assertIn("event.type === 'annotation'", body)
        self.assertIn("event.kind", body)
        self.assertIn("event.text", body)
        self.assertIn("is-annotation", self.metrics_css)
        self.assertRegex(self.metrics_css, r"\.chart-marker\s*\{")
        self.assertRegex(self.metrics_css, r"\.warning-banner\s*\{")

    def test_dashboard_rail_shows_a_compact_warning_count(self):
        row = function_body(self.source, "deviceRow")
        self.assertIn("warning_count", row)
        self.assertIn("device-warn", row)
        self.assertIn("warning_count", function_body(self.source, "networkKey"))
        self.assertRegex(self.css, r"\.device-warn\s*\{")


class DockerContainerRailTest(FrontEndSourceTest):
    """A container is listed once: in the Docker block, never as a card too.

    There is no JS runtime in this suite, so these assert the seams: the group
    filter, the selectable row markup, and the resolver the metrics tree passes
    in. The rendered result was checked against a live payload separately.
    """

    def setUp(self):
        super().setUp()
        root = APP_JS.parent
        self.stack = (root / "stack.js").read_text()
        self.metrics = (root / "metrics.js").read_text()

    def test_containers_are_excluded_from_the_free_standing_cards(self):
        self.assertIn("device.kind !== 'container'", self.stack)
        # The filter that builds `others` is the one that has to carry it.
        others = self.stack.split("const others =", 1)[1].split(";", 1)[0]
        self.assertIn("container", others)

    def test_a_container_row_becomes_a_series_selector_when_it_has_a_device(self):
        row = self.stack.split("function containerRow", 1)[1].split("function dockerEngine", 1)[0]
        self.assertIn("device", row)
        self.assertIn("data-device=", row)
        self.assertIn("is-selectable", row)

    def test_the_metrics_tree_resolves_each_container_to_its_device(self):
        self.assertIn("dockerEngine(stack.containers,", self.metrics)
        self.assertIn("device.kind === 'container'", self.metrics)

    def test_the_selectable_row_is_styled_not_like_a_default_button(self):
        self.assertIn(".docker-container.is-selectable", self.css)
        block = self.css.split(".docker-container.is-selectable {", 1)[1].split("}", 1)[0]
        self.assertIn("border: 0", block)
        self.assertIn("background: none", block)
