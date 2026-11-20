"""Stdlib tests for the dashboard API: click hits and decayed bookmark scores.

Run from the repo root:
    python3 -m unittest discover -s server -p 'test_*.py' -v
"""
import hmac
import importlib.util
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

FRONTEND = Path(__file__).resolve().parent.parent
APP_JS = FRONTEND / "app.js"
UI_CORE = FRONTEND / "ui-core.js"
DASH_DATA = FRONTEND / "dash-data.js"
STACK_JS = FRONTEND / "stack.js"
METRICS_JS = FRONTEND / "metrics.js"

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
        self._old_checks = app.CHECKS_FILE
        app.DATA = self.data
        app.HITS_FILE = self.data / "hits.json"
        app.AGENTS_FILE = self.data / "agents.json"
        app.METRICS_FILE = self.data / "metrics.db"
        app.CHECKS_FILE = self.data / "checks.json"
        app._cache.update(at=0.0, body=None)
        app._availability_cache.update(at=0.0, by_id={})
        app._summary_cache.update(at=0.0, window=None, body=None)
        app._brief_cache.update(at=0.0, window=None, body=None)
        app._last_prune["at"] = 0.0
        app._check_state.update(results={})
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
        app.CHECKS_FILE = self._old_checks
        app._cache.update(at=0.0, body=None)
        app._availability_cache.update(at=0.0, by_id={})
        app._summary_cache.update(at=0.0, window=None, body=None)
        app._brief_cache.update(at=0.0, window=None, body=None)
        app._last_prune["at"] = 0.0
        app._check_state.update(results={})
        self._tmp.cleanup()

    def write_bookmarks(self, store):
        (self.data / "bookmarks.json").write_text(json.dumps(store))

    def write_checks(self, checks):
        (self.data / "checks.json").write_text(json.dumps({"checks": checks}))

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

    def test_a_pve_guest_carries_its_role_and_identity(self):
        self.push_payload(
            {
                "kind": "lxc-guest",
                "name": "agent",
                "state": "running",
                "vmid": 210,
                "vcpu": 4,
                "cpu_pct": 0.1,
                "mem_pct": 7.1,
                "mem_used": 612667392,
                "mem_total": 8589934592,
                "uptime_s": 373817,
            },
            name="agent",
        )
        app._cache.update(at=0.0, body=None)

        with frozen_time():
            body = app.stats()

        device = next(d for d in body["devices"] if d["id"] == "agent:agent")
        self.assertEqual(device["kind"], "lxc-guest")
        self.assertEqual(device["vmid"], 210)
        self.assertEqual(device["vcpu"], 4)
        # The dashboard owns what a guest runs, so an installed agent older than
        # the deploy still gets a role on the page.
        self.assertEqual(device["role"], "T3 Code")
        # A guest is a device of its own, never a row in the Docker block.
        names = [c["name"] for c in body["topology"]["containers"]]
        self.assertNotIn("agent", names)

    def test_a_guest_role_reported_by_its_agent_wins_over_the_table(self):
        self.push_payload(
            {"kind": "lxc-guest", "name": "hermes", "role": "Hermes agent"},
            name="hermes",
        )
        app._cache.update(at=0.0, body=None)

        with frozen_time():
            body = app.stats()

        device = next(d for d in body["devices"] if d["id"] == "agent:hermes")
        self.assertEqual(device["role"], "Hermes agent")

    def test_a_guest_with_no_known_role_is_not_given_one(self):
        self.push_payload({"kind": "lxc-guest", "name": "mystery"}, name="mystery")
        app._cache.update(at=0.0, body=None)

        with frozen_time():
            body = app.stats()

        device = next(d for d in body["devices"] if d["id"] == "agent:mystery")
        self.assertNotIn("role", device)

    def test_a_nonsense_guest_identity_is_not_stored(self):
        self.push_payload(
            {"kind": "lxc-guest", "name": "odd", "vmid": "210", "vcpu": -4},
            name="odd",
        )
        app._cache.update(at=0.0, body=None)

        with frozen_time():
            body = app.stats()

        device = next(d for d in body["devices"] if d["id"] == "agent:odd")
        self.assertNotIn("vmid", device)
        self.assertNotIn("vcpu", device)

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


class AgentDownloadTest(ApiTestCase):
    """An installed agent re-reads its own copy from /agents to stay current."""

    def test_agent_scripts_are_served_as_text(self):
        # The PowerShell agent compares the response with the file on disk, so
        # octet-stream (which .ps1 would otherwise get) would hand it bytes.
        agents = self.static / "agents"
        agents.mkdir()
        (agents / "dash-agent.ps1").write_text("# agent\n")

        with urllib.request.urlopen(self.base + "/agents/dash-agent.ps1", timeout=5) as resp:
            self.assertEqual(resp.headers.get_content_type(), "text/plain")
            self.assertEqual(resp.read(), b"# agent\n")


class AgentFieldAllowlistTest(unittest.TestCase):
    """The agents' wire vocabulary has one home; the server still validates.

    server/app.py deliberately does not import the agents, so the shared
    module can stay a plain file installed beside them. This test is the pin:
    if either side grows a signal the other has not heard of, it fails here
    rather than silently dropping the value at the push endpoint.
    """

    def test_agent_extra_metrics_match_the_server_allowlist(self):
        path = Path(__file__).resolve().parent.parent / "agents" / "dash_common.py"
        spec = importlib.util.spec_from_file_location("dash_common_crosscheck", path)
        common = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(common)

        self.assertEqual(tuple(common.EXTRA_METRICS), tuple(app.EXTRA_METRICS))


class FrontEndSourceTest(unittest.TestCase):
    """Shared harness for browser-only code, which has no JS runtime here."""

    def setUp(self):
        self.source = APP_JS.read_text()
        self.js = self.source
        self.ui = UI_CORE.read_text()
        self.dash_data = DASH_DATA.read_text()
        self.stack = STACK_JS.read_text()
        self.html = (FRONTEND / "index.html").read_text()
        self.css = (FRONTEND / "style.css").read_text()
        self.metrics_html = (FRONTEND / "metrics.html").read_text()
        self.metrics_js = METRICS_JS.read_text()
        self.metrics_css = (FRONTEND / "metrics.css").read_text()


class SharedRuntimeSourceTest(FrontEndSourceTest):
    """One shared runtime module and one data seam for both pages.

    The repo still has no JS runtime, so these are source-level contracts: the
    pages must load the shared modules first, keep no copy of their helpers,
    and route fetching, clocking and polling through the shared interfaces.
    """

    def test_pages_load_the_shared_modules_before_their_own_script(self):
        for html, page in ((self.html, "app.js"), (self.metrics_html, "metrics.js")):
            with self.subTest(page=page):
                core = html.index('src="ui-core.js"')
                data = html.index('src="dash-data.js"')
                stack = html.index('src="stack.js"')
                script = html.index('src="' + page + '"')
                self.assertLess(core, data)
                self.assertLess(data, stack)
                self.assertLess(stack, script)

    def test_ui_core_exposes_the_shared_runtime(self):
        self.assertIn("window.dashboardUI = {", self.ui)
        for name in ("escapeHtml", "isNumber", "kindLabel", "KIND_DEFS",
                     "KIND_ORDER", "fmtBytes", "fmtBytesCompact",
                     "fmtDuration", "fmtUptime", "createClock", "createPoller"):
            with self.subTest(export=name):
                self.assertRegex(self.ui, r"\b" + name + r":\s*" + name + r"\b")

    def test_escaping_kind_labels_and_formats_have_one_definition(self):
        # Neither page carries its own copy of a shared helper.
        for source in (self.js, self.metrics_js):
            for absent in ("function escapeHtml(", "function kindLabel(",
                           "KIND_DEFS = [", "WORK_START_HOUR ="):
                with self.subTest(absent=absent):
                    self.assertNotIn(absent, source)
        self.assertIn("function escapeHtml(", self.ui)
        self.assertIn("function kindLabel(", self.ui)
        self.assertRegex(self.ui, r"KIND_DEFS\s*=\s*\[")

        # The dashboard keeps its compact byte/uptime policy; the history page
        # keeps its em-dash/duration one, both from the shared scale.
        self.assertIn("fmtBytesCompact", self.js)
        self.assertIn("fmtUptime", self.js)
        self.assertIn("fmtBytes,", self.metrics_js)
        self.assertIn("fmtDuration", self.metrics_js)
        self.assertIn("function scaleBytes(", self.ui)
        self.assertIn("function durationText(", self.ui)

    def test_one_poller_owns_backoff_and_visibility(self):
        poller = function_body(self.ui, "createPoller")
        self.assertIn("setTimeout(", poller)
        self.assertIn("clearTimeout(", poller)
        self.assertIn("document.hidden", poller)
        self.assertIn("visibilitychange", poller)
        self.assertIn(
            "Math.min(maxInterval, interval * 2 ** Math.min(failures, 3))",
            poller,
        )

        # The dashboard polls through it; the history page keeps its minute.
        start = function_body(self.js, "startDevicePolling")
        self.assertIn("dashboardUI.createPoller(", start)
        self.assertIn("interval: 10000", start)
        self.assertIn("maxInterval: 60000", start)
        self.assertIn("run: refreshDevices", start)
        self.assertRegex(self.metrics_js, r"dashboardUI\.createPoller\(\{")
        self.assertRegex(self.metrics_js, r"interval:\s*REFRESH_MS")
        for source in (self.js, self.metrics_js):
            self.assertNotIn("function scheduleRefresh(", source)
        self.assertNotIn("setInterval(refreshDevices", self.js)
        # Visibility is handled inside the one poller, not per page.
        self.assertNotIn("document.addEventListener('visibilitychange'", self.js)
        self.assertNotIn("document.addEventListener('visibilitychange'", self.metrics_js)

    def test_one_fetch_client_serves_both_pages(self):
        self.assertIn("window.dashData = {", self.dash_data)
        get = function_body(self.dash_data, "getJson")
        self.assertIn("cache: 'no-store'", get)
        self.assertIn("response.ok", get)
        self.assertIn("response.json()", get)

        read = function_body(self.dash_data, "read")
        self.assertIn("ok: true", read)
        self.assertIn("ok: false", read)
        self.assertIn("console.warn", read)
        self.assertIn("keeping the last good reading", read)

        for source in (self.js, self.metrics_js):
            self.assertNotIn("function getJson(", source)
            self.assertNotIn("await fetch(", source)

        self.assertIn("dashData.read('/api/stats'",
                      function_body(self.js, "refreshDevices"))
        self.assertIn("dashData.read('/api/bookmarks'",
                      function_body(self.js, "loadBookmarks"))
        self.assertIn("dashData.read(", function_body(self.metrics_js, "load"))

    def test_stack_exposes_the_model_plus_one_render_entry(self):
        # The pure model stays; markup lives behind a single render entry.
        self.assertIn("countedDevices: countedDevices", self.stack)
        self.assertIn("group: group", self.stack)
        self.assertIn("keyOf: keyOf", self.stack)
        self.assertIn("render: render", self.stack)
        self.assertNotIn("function escapeHtml(", self.stack)
        self.assertIn("dashboardUI.escapeHtml", self.stack)

        for source in (self.js, self.metrics_js):
            self.assertNotIn("dashboardStack.branch(", source)
            self.assertNotIn("dashboardStack.dockerEngine(", source)
        self.assertIn("dashboardStack.render.branch(", self.js)
        self.assertIn("dashboardStack.render.dockerEngine(", self.js)
        self.assertIn("dashboardStack.render.dockerEngine(", self.metrics_js)


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
        self.assertRegex(self.ui, r"WORK_START_HOUR\s*=\s*9\b")
        self.assertRegex(self.ui, r"WORK_END_HOUR\s*=\s*17\b")

        body = function_body(self.ui, "update")
        self.assertIn("getCurrentTime()", body)
        self.assertIn("WORK_START_HOUR", body)
        self.assertIn("data-theme", body)
        self.assertIn("setAttribute", body)

        # Both pages drive their clock, including the one-second tick, through
        # the shared factory.
        self.assertIn("dashboardUI.createClock(", self.js)
        self.assertIn("clock.update()", function_body(self.js, "updateDashboard"))
        self.assertIn("dashboardUI.createClock(", self.metrics_js)
        self.assertIn("clock.start()", self.js)
        self.assertIn("clock.start()", self.metrics_js)
        self.assertIn("setInterval(update, 1000)", function_body(self.ui, "createClock"))
        self.assertNotIn("setInterval(updateClock", self.metrics_js)

    def test_debug_time_overrides_the_clock_and_theme(self):
        body = function_body(self.ui, "getCurrentTime")
        self.assertIn("debugTimeOffset", body)
        self.assertIn(
            "debugTimeOffset = debugDate.getTime() - now.getTime()", self.ui
        )
        self.assertIn("debugTimeOffset = null", self.ui)

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

        for name in ("app.js", "ui-core.js", "dash-data.js", "stack.js",
                     "index.html", "style.css",
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
        self.assertIn("isNumber(d[field])", body)
        self.assertIn("samples.length", body)

        numeric = function_body(self.ui, "isNumber")
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

    def test_dashboard_loads_the_shared_modules_before_app_js(self):
        for name in ("ui-core.js", "dash-data.js", "stack.js"):
            with self.subTest(module=name):
                self.assertLess(
                    self.html.index('src="' + name + '"'),
                    self.html.index('src="app.js"'),
                )
        self.assertNotIn("kind-chips", self.html)

    def test_network_tree_nests_pve_guests_and_the_docker_engine(self):
        body = function_body(self.js, "networkTree")

        self.assertIn("dashboardStack.group(devices, topology)", body)
        self.assertIn("'PVE host'", body)
        self.assertIn("'LXC guests'", body)
        self.assertIn("'Elsewhere'", body)
        self.assertIn("dashboardStack.render.dockerEngine(stack.containers)", body)
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
        body = function_body(self.ui, "kindLabel")

        self.assertIn("KIND_LABELS[kind]", body)
        self.assertIn("replace(/[-_]+/g, ' ')", body)
        self.assertIn("toUpperCase", body)
        self.assertIn("'Other'", body)

    def test_kind_labels_and_order_share_one_definition(self):
        # Adding a kind must edit one list, not two parallel constants that can
        # drift apart.
        self.assertRegex(self.ui, r"KIND_DEFS\s*=\s*\[")
        defs = re.search(r"KIND_DEFS\s*=\s*\[(.*?)\];", self.ui, re.S)
        self.assertIsNotNone(defs, "KIND_DEFS not found")
        self.assertIn("label:", defs.group(1))
        self.assertIn("key:", defs.group(1))

        self.assertRegex(self.ui, r"KIND_LABELS\s*=\s*Object\.fromEntries\(")
        self.assertRegex(self.ui, r"KIND_ORDER\s*=\s*KIND_DEFS\.map\(")

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

    def test_a_reported_role_replaces_the_kind_label_in_the_row_sub_line(self):
        sub = function_body(self.js, "deviceSub")

        # Every pve guest reads "LXC guests"; the role is what tells agent from
        # hermes, so it wins over the kind label.
        self.assertIn("deviceRole(d)", sub)
        self.assertIn("role || kindLabel(d.kind)", sub)
        role = function_body(self.js, "deviceRole")
        self.assertIn("typeof d.role === 'string'", role)

    def test_a_guest_row_keeps_its_size_and_vmid_out_of_the_crowded_sub_line(self):
        row = function_body(self.js, "deviceRow")
        sub = function_body(self.js, "deviceSub")

        # The rail gives the sub-line one narrow column beside three metrics, so
        # "role · up 4d 7h · online" clips the state away. A guest keeps role and
        # state on screen and puts uptime, vCPU, RAM and vmid in the tooltip.
        self.assertIn("isNumber(d.vmid)) return label + ' · online'", sub)
        self.assertIn("isNumber(d.vmid)", row)
        self.assertIn("isNumber(d.vcpu)", row)
        self.assertIn("isNumber(d.mem_total)", row)
        self.assertIn("fmtUptime(d.uptime_s)", row)
        self.assertIn("title=", row)

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
        # tab. Both now live in the one shared poller.
        poller = function_body(self.ui, "createPoller")
        self.assertIn("setTimeout(", poller)
        self.assertIn("document.hidden", poller)
        self.assertIn("Math.min(maxInterval, interval * 2 ** Math.min(failures, 3))", poller)
        self.assertIn("visibilitychange", poller)
        start = function_body(self.js, "startDevicePolling")
        self.assertIn("interval: 10000", start)
        self.assertIn("maxInterval: 60000", start)
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

    def test_a_guest_keeps_its_role_on_the_metrics_rail_too(self):
        self.sample([
            self.device(device_id="agent:agent", name="agent", kind="lxc-guest"),
            self.device(device_id="agent:mystery", name="mystery", kind="lxc-guest"),
        ])

        payload = app.metrics_summary("1h", now=self.BASE + 60)
        by_id = {device["id"]: device for device in payload["devices"]}

        self.assertEqual(by_id["agent:agent"]["role"], "T3 Code")
        self.assertNotIn("role", by_id["agent:mystery"])

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

    # ---------- one window reader ----------

    def window_view(self, since, until, step, device_id="docker01"):
        """Read one window through the module the endpoints use, not a table."""
        con = app._connect_metrics()
        try:
            return app.read_window(con, device_id, since, until, step)
        finally:
            con.close()

    def seed_six_hours(self):
        """Six hours of minute samples: hour 2 offline, a reboot in hour 3.

        CPU and RAM change by the hour and the offline hour carries the
        window's peak, so parity also pins sample weighting and the rule that
        every recorded sample is averaged. One missing minute makes a
        5-minute bucket short, so a lost weight would be visible. Whole
        5-minute and whole-hour runs keep every store's resolution
        comparable.
        """
        for minute in range(360):
            if minute == 1:
                continue
            hour = minute // 60
            uptime = (
                1000 + minute * 60 if minute < 180
                else 60 + (minute - 180) * 60
            )
            cpu = 99.0 if hour == 2 else 10.0 + hour * 10
            if hour == 0:
                cpu += minute % 5
            self.sample([self.device(
                online=hour != 2, cpu=cpu,
                mem=95.0 if hour == 2 else 30.0 + hour,
                disk=50.0, uptime=uptime,
                disks=[self.volume("/", 50.0, "System")],
            )], now=self.BASE + minute * 60)

    def test_the_window_reader_agrees_across_raw_five_minute_and_hourly_stores(self):
        self.seed_six_hours()
        until = self.BASE + 360 * 60
        raw = self.window_view(self.BASE, until, 300)
        five = self.window_view(until - 7 * 86400, until, 300)
        hourly = self.window_view(until - 90 * 86400, until, 3600)

        # The store is chosen from the window length, not by the caller.
        self.assertEqual(raw["source"], "raw")
        self.assertEqual(five["source"], "5m")
        self.assertEqual(hourly["source"], "1h")

        # Independent values: the offline hour's peak counts in the averages
        # and its 99% CPU is the window peak, exactly as a rollup weights it.
        self.assertEqual(raw["summary"]["samples"], 359)
        self.assertEqual(raw["summary"]["availability_pct"], 83.3)
        self.assertEqual(raw["summary"]["cpu_avg"], 46.9)
        self.assertEqual(raw["summary"]["cpu_max"], 99.0)
        self.assertEqual(raw["summary"]["mem_avg"], 43.0)

        for name, other in (("5m", five), ("1h", hourly)):
            self.assertEqual(other["summary"], raw["summary"], name)
            self.assertEqual(other["events"], raw["events"], name)
            self.assertEqual(other["online"], raw["online"], name)

        # The same buckets, so the chart data must be identical too.
        self.assertEqual(five["points"], raw["points"])
        self.assertEqual(five["bands"], raw["bands"])
        self.assertEqual(
            hourly["points"],
            self.window_view(self.BASE, until, 3600)["points"],
        )
        self.assertEqual(
            hourly["bands"],
            self.window_view(self.BASE, until, 3600)["bands"],
        )
        self.assertEqual(five["volumes"], raw["volumes"])

    def test_an_empty_window_has_the_same_summary_shape_as_a_populated_one(self):
        self.sample([self.device()], now=self.BASE)
        populated = self.window_view(self.BASE, self.BASE + 60, 60)["summary"]
        empty = self.window_view(
            self.BASE + 86400, self.BASE + 86400 + 7 * 86400, 300
        )
        self.assertEqual(empty["source"], "5m")
        self.assertEqual(empty["summary"]["samples"], 0)
        self.assertEqual(set(empty["summary"]), set(populated))
        self.assertEqual(empty["points"], [])
        self.assertEqual(empty["bands"], {"cpu": [], "mem": [], "disk": []})
        self.assertEqual(empty["volumes"], [])
        self.assertEqual(empty["signals"], {})
        for key in ("cpu_pct", "mem_pct", "disk_pct"):
            self.assertIsNone(empty["summary"][key])

    def test_the_series_endpoint_keeps_the_summary_shape_when_the_window_is_empty(self):
        self.sample([self.device()], now=self.BASE)
        populated = app.metrics_series(
            "docker01", "1h", 60, now=self.BASE + 60
        )["summary"]
        empty = app.metrics_series(
            "docker01", "1h", 60, now=self.BASE + 86400
        )["summary"]
        self.assertEqual(empty["samples"], 0)
        self.assertEqual(set(empty), set(populated))

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


class BriefTest(ApiTestCase):
    """The "what changed" read model: one ranked list of recent events."""

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

    def write_agents(self, agents):
        (self.data / "agents.json").write_text(json.dumps(agents))

    def test_a_fresh_install_is_quiet_with_an_empty_item_list(self):
        payload = app.brief("24h", now=self.BASE)

        self.assertEqual(payload["generated"], self.BASE)
        self.assertEqual(payload["window"], "24h")
        self.assertEqual(payload["window_s"], 86400)
        self.assertTrue(payload["quiet"])
        self.assertEqual(payload["items"], [])
        self.assertEqual(payload["counts"],
                         {"critical": 0, "warning": 0, "info": 0})

    def test_an_offline_episode_becomes_one_plain_language_item(self):
        pve = "agent:pve"
        for offset, online, uptime in (
            (0, True, 1000), (60, True, 1060), (120, False, 1120),
            (180, False, 1180), (240, True, 1240),
        ):
            self.sample([self.device(pve, "pve", "host", online=online,
                                     uptime=uptime)], now=self.BASE + offset)

        payload = app.brief("1h", now=self.BASE + 240)
        offline = [item for item in payload["items"] if item["kind"] == "offline"]

        self.assertEqual(len(offline), 1)
        item = offline[0]
        self.assertEqual(item["device_id"], pve)
        self.assertEqual(item["device_name"], "pve")
        self.assertEqual(item["level"], "info")
        self.assertEqual(item["ts"], self.BASE + 120)
        self.assertEqual(item["href"], "metrics.html?device=agent%3Apve")
        self.assertRegex(item["text"], r"^pve was offline for 2m at \d\d:\d\d$")
        self.assertIn("2m total", item["detail"])
        self.assertFalse(payload["quiet"])

    def test_a_restart_is_one_info_item(self):
        pve = "agent:pve"
        for offset, uptime in ((0, 5000), (60, 5050), (120, 40)):
            self.sample([self.device(pve, "pve", "host", uptime=uptime)],
                        now=self.BASE + offset)

        payload = app.brief("1h", now=self.BASE + 120)
        restarts = [item for item in payload["items"] if item["kind"] == "restart"]

        self.assertEqual(len(restarts), 1)
        self.assertEqual(restarts[0]["level"], "info")
        self.assertEqual(restarts[0]["ts"], self.BASE + 120)
        self.assertRegex(restarts[0]["text"], r"^pve restarted at \d\d:\d\d$")
        self.assertFalse(payload["quiet"])

    def test_a_warning_at_the_window_end_is_named_with_its_device(self):
        self.sample([self.device(disk=86.0)], now=self.BASE)

        payload = app.brief("1h", now=self.BASE + 60)
        warnings = [item for item in payload["items"] if item["kind"] == "warning"]

        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["level"], "warning")
        self.assertEqual(warnings[0]["device_name"], "docker01")
        self.assertEqual(warnings[0]["text"], "docker01: Disk 86% full")
        self.assertEqual(warnings[0]["href"], "metrics.html?device=docker01")
        self.assertEqual(payload["counts"]["warning"], 1)
        self.assertEqual(payload["counts"]["critical"], 0)
        self.assertFalse(payload["quiet"])

    def post_annotation(self, device, text, ts, kind="note"):
        return self.post(json.dumps({
            "device": device, "text": text, "ts": ts, "kind": kind,
        }).encode(), path="/api/annotations")

    def test_manual_and_fleet_wide_annotations_appear_once_each(self):
        self.sample([self.device("docker01"),
                     self.device("agent:pve", "pve", "host")], now=self.BASE)
        self.assertEqual(self.post_annotation(
            "docker01", "root volume resized", self.BASE + 30)[0], 201)
        self.assertEqual(self.post_annotation(
            "", "cluster reboot", self.BASE + 60, kind="deploy")[0], 201)

        payload = app.brief("1h", now=self.BASE + 120)
        notes = [item for item in payload["items"] if item["kind"] == "annotation"]

        self.assertEqual(len(notes), 2)
        self.assertEqual(
            [(note["device_id"], note["text"]) for note in notes],
            [("", "fleet: cluster reboot"),
             ("docker01", "docker01: root volume resized")],
        )
        fleet = notes[0]
        self.assertEqual(fleet["device_name"], "fleet")
        self.assertNotIn("href", fleet)
        self.assertEqual(notes[1]["href"], "metrics.html?device=docker01")
        self.assertIn("deploy", fleet["detail"])
        self.assertFalse(payload["quiet"])

    def test_volume_growth_names_the_mount_the_bytes_and_the_percentage(self):
        total = 107374182400  # 100 GiB, so 1.5% is 1.5 GB on screen
        for offset, pct in ((0, 78.0), (43200, 79.0), (86400, 79.5)):
            self.sample([self.device(
                uptime=None, disk=pct,
                disks=[{"mount": "/", "label": "root", "total": total,
                        "used": int(total * pct / 100), "pct": pct}],
            )], now=self.BASE + offset)

        payload = app.brief("24h", now=self.BASE + 86400)
        disks = [item for item in payload["items"] if item["kind"] == "disk"]

        self.assertEqual(len(disks), 1)
        self.assertEqual(disks[0]["text"],
                         "docker01 root grew 1.5 GB (78% → 79.5%)")
        self.assertEqual(disks[0]["level"], "warning")
        self.assertIn("7d to 90%", disks[0]["detail"])
        # The aggregate disk/days/growth warnings would repeat this line.
        self.assertEqual(
            [item for item in payload["items"] if item["kind"] == "warning"], [])
        self.assertEqual(payload["counts"]["warning"], 1)

    def test_a_stale_agent_is_named_with_how_long_it_has_been_quiet(self):
        pve = "agent:pve"
        self.sample([self.device(pve, "pve", "host")], now=self.BASE)
        # The agent stopped pushing 15 minutes ago; the sample above is older
        # history, not evidence that it is alive now.
        self.write_agents({"pve": {
            "name": "pve", "kind": "host", "last_seen": self.BASE - 840,
            "cpu_pct": 4.0, "mem_pct": 20.0,
        }})

        with frozen_time(self.BASE + 60):
            payload = app.brief("1h", now=self.BASE + 60)
        stale = [item for item in payload["items"] if item["kind"] == "stale"]

        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0]["level"], "warning")
        self.assertEqual(stale[0]["ts"], self.BASE - 840)
        self.assertEqual(stale[0]["text"], "pve last reported 15m ago")
        self.assertIn("Last seen at", stale[0]["detail"])
        # The same fact is not repeated as an offline warning.
        self.assertEqual(
            [item for item in payload["items"] if item["kind"] == "warning"], [])
        self.assertEqual(payload["counts"]["warning"], 1)

    def test_availability_is_compared_with_the_previous_equal_window(self):
        for offset in (-5400, -4800):
            self.sample([self.device(online=True, uptime=1000)],
                        now=self.BASE + offset)
        for offset, online in ((0, True), (60, False), (120, False), (180, True)):
            self.sample([self.device(online=online, uptime=1000 + offset)],
                        now=self.BASE + offset)

        payload = app.brief("1h", now=self.BASE + 180)
        drops = [item for item in payload["items"] if item["kind"] == "availability"]

        self.assertEqual(len(drops), 1)
        self.assertEqual(drops[0]["level"], "warning")
        self.assertEqual(drops[0]["text"],
                         "docker01 availability fell to 50% (was 100%)")

    def test_a_recovery_from_before_the_window_is_still_reported(self):
        pve = "agent:pve"
        for offset, online in ((0, False), (60, False), (120, True)):
            self.sample([self.device(pve, "pve", "host", online=online,
                                     uptime=1000)], now=self.BASE + offset)

        payload = app.brief("1h", now=self.BASE + 120)
        recovered = [item for item in payload["items"] if item["kind"] == "offline"]

        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["ts"], self.BASE + 120)
        self.assertEqual(recovered[0]["level"], "info")
        self.assertRegex(recovered[0]["text"], r"^pve recovered at \d\d:\d\d$")
        self.assertIn("before this window", recovered[0]["detail"])

    def test_growth_critical_matches_the_shared_warning_rule(self):
        total = 107374182400
        for offset, pct in ((0, 58.0), (43200, 61.0), (86400, 64.0)):
            self.sample([self.device(
                uptime=None, disk=pct,
                disks=[{"mount": "/", "label": "root", "total": total,
                        "used": int(total * pct / 100), "pct": pct}],
            )], now=self.BASE + offset)

        disks = [item for item in app.brief("24h", now=self.BASE + 86400)["items"]
                 if item["kind"] == "disk"]

        self.assertEqual(len(disks), 1)
        # 6%/day is above WARN_GROWTH * 5, so evaluate_warnings calls it critical.
        self.assertEqual(disks[0]["level"], "critical")
        self.assertEqual(disks[0]["text"],
                         "docker01 root grew 6.0 GB (58% → 64%)")

    def test_a_seven_day_brief_reads_volume_growth_from_rollups(self):
        total = 107374182400
        for offset, pct in ((0, 50.0), (300, 55.0), (600, 60.0)):
            self.sample([self.device(
                uptime=None, disk=pct,
                disks=[{"mount": "/", "label": "root", "total": total,
                        "used": int(total * pct / 100), "pct": pct}],
            )], now=self.BASE + offset)

        con = app._connect_metrics()
        try:
            con.execute("DELETE FROM samples")
            con.execute("DELETE FROM disk_samples")
            con.commit()
        finally:
            con.close()

        disks = [item for item in app.brief("7d", now=self.BASE + 600)["items"]
                 if item["kind"] == "disk"]
        self.assertEqual(len(disks), 1)

    def test_a_quiet_brief_can_still_carry_info_rows(self):
        total = 107374182400
        for offset, pct in ((0, 50.0), (3 * 86400, 50.5), (7 * 86400, 51.0)):
            self.sample([self.device(
                uptime=None, disk=pct,
                disks=[{"mount": "/", "label": "root", "total": total,
                        "used": int(total * pct / 100), "pct": pct}],
            )], now=self.BASE + offset)

        payload = app.brief("7d", now=self.BASE + 7 * 86400)

        self.assertTrue(payload["quiet"])
        self.assertEqual({item["level"] for item in payload["items"]}, {"info"})
        self.assertIn("disk", {item["kind"] for item in payload["items"]})

    def test_an_agent_push_invalidates_the_live_brief(self):
        with frozen_time(self.BASE + 60):
            first = app.brief("1h")
            self.assertIs(app.brief("1h"), first)
            status, _ = self.post(json.dumps({
                "name": "pve", "kind": "host", "cpu_pct": 1.0,
                "mem_pct": 40.0, "disk_pct": 96.0,
            }).encode(), path="/api/push/pve")
            self.assertEqual(status, 200)
            self.assertIsNot(app.brief("1h"), first)

    def get_status(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=5) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def test_the_endpoint_validates_the_window_and_returns_the_documented_shape(self):
        self.sample([self.device(disk=86.0)], now=time.time())

        self.assertEqual(self.get_status("/api/brief?window=2h")[0], 400)
        # 30d is a valid metrics window but not a brief window.
        self.assertEqual(self.get_status("/api/brief?window=30d")[0], 400)

        status, body = self.get_status("/api/brief?window=1h")
        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(payload["window"], "1h")
        self.assertEqual(payload["window_s"], 3600)
        self.assertEqual(set(payload["counts"]), {"critical", "warning", "info"})
        self.assertTrue(payload["items"])
        for item in payload["items"]:
            for key in ("kind", "level", "device_id", "device_name", "ts", "text"):
                self.assertIn(key, item)
            self.assertIn(item["level"], {"critical", "warning", "info"})
            if "href" in item:
                self.assertTrue(item["href"].startswith("metrics.html"))

        status, body = self.get_status("/api/brief")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["window"], "24h")

    def test_items_sort_by_severity_before_recency(self):
        self.sample([
            self.device("z-critical", "z-critical", disk=96.0),
            self.device("y-warn", "y-warn", disk=86.0),
            self.device("x-restart", "x-restart", uptime=5000),
        ], now=self.BASE)
        self.sample([
            self.device("x-restart", "x-restart", uptime=40),
        ], now=self.BASE + 60)

        items = app.brief("1h", now=self.BASE + 60)["items"]

        self.assertEqual([item["level"] for item in items[:3]],
                         ["critical", "warning", "info"])
        self.assertEqual(items[0]["device_id"], "z-critical")
        self.assertEqual(items[1]["device_id"], "y-warn")
        self.assertEqual(items[2]["kind"], "restart")

    def test_the_item_list_is_capped_but_the_counts_report_everything(self):
        devices = [self.device(f"dev{index:02d}", f"dev{index:02d}", uptime=5000)
                   for index in range(25)]
        self.sample(devices, now=self.BASE)
        self.sample([self.device(f"dev{index:02d}", f"dev{index:02d}", uptime=40)
                     for index in range(25)], now=self.BASE + 60)

        payload = app.brief("1h", now=self.BASE + 60)

        self.assertEqual(len(payload["items"]), app.BRIEF_MAX_ITEMS)
        self.assertEqual(payload["counts"]["info"], 25)
        self.assertFalse(payload["quiet"])

    def test_a_seven_day_brief_reads_rollups_and_not_raw_rows(self):
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

        payload = app.brief("7d", now=self.BASE + 900)
        kinds = {item["kind"] for item in payload["items"]}

        self.assertIn("offline", kinds)
        self.assertIn("restart", kinds)

    def test_the_live_brief_is_cached_until_a_write_invalidates_it(self):
        self.sample([self.device(disk=50.0)], now=self.BASE)
        with frozen_time(self.BASE + 60):
            first = app.brief("1h")
            self.assertIs(app.brief("1h"), first)
            # An explicit clock always computes a fresh payload.
            self.assertIsNot(app.brief("1h", now=self.BASE + 60), first)
            # A write drops the cache instead of hiding the new warning.
            self.sample([self.device(disk=96.0)], now=self.BASE)
            refreshed = app.brief("1h")

        self.assertNotEqual(first["items"], refreshed["items"])
        self.assertIn("critical", {item["level"] for item in refreshed["items"]})

    def test_posting_an_annotation_invalidates_the_live_brief(self):
        self.sample([self.device()], now=self.BASE)
        with frozen_time(self.BASE + 60):
            first = app.brief("1h")
            self.assertEqual(self.post_annotation(
                "docker01", "patching", self.BASE + 30)[0], 201)
            refreshed = app.brief("1h")

        self.assertNotEqual(first["items"], refreshed["items"])
        self.assertIn("docker01: patching",
                      {item["text"] for item in refreshed["items"]})


class BriefSourceTest(FrontEndSourceTest):
    """The Workbench "What changed" card, asserted at the source level."""

    def test_the_card_sits_at_the_top_of_the_main_column_above_jump_back_in(self):
        self.assertIn('id="brief-card"', self.html)
        self.assertIn('id="brief-list"', self.html)
        self.assertIn("What changed", self.html)
        self.assertLess(self.html.index('id="brief-card"'),
                        self.html.index('id="jump-back-in-title"'))
        self.assertGreater(self.html.index('id="brief-card"'),
                           self.html.index('<main class="main-column">'))

    def test_the_card_reads_the_brief_from_the_shared_data_seam(self):
        self.assertIn("/api/brief?window=24h", self.source)
        self.assertIn("window.dashData.read", self.source)
        self.assertIn("function renderBrief", self.source)
        self.assertIn("payload.quiet", self.source)
        self.assertIn("All clear", self.source)

    def test_rows_show_time_device_text_and_a_level_marker(self):
        for needle in ("brief-row", "brief-time", "brief-device", "brief-text",
                       "brief-mark", "data-level"):
            with self.subTest(needle=needle):
                self.assertIn(needle, self.source)
        self.assertIn("escapeHtml", self.source)
        self.assertIn("device_name", self.source)
        self.assertIn("clockLabel", self.source)

    def test_device_items_link_to_the_metrics_page(self):
        self.assertIn("item.href", self.source)
        self.assertIn("briefSafeHref", self.source)
        # Only a relative metrics deep link is trusted; the server URL-encodes
        # the device id (agent ids contain a colon).
        self.assertIn("metrics\\.html\\?device=", self.source)

    def test_the_card_polls_slowly_and_starts_with_the_page(self):
        self.assertIn("startBriefPolling", self.source)
        self.assertIn("interval: 60000", self.source)
        self.assertIn("startBriefPolling();", self.source)

    def test_the_brief_does_not_push_all_tools_into_sideways_overflow(self):
        card = self.css[self.css.index(".all-tools-card {"):]
        self.assertIn("overflow-y: auto", card[:card.index("}")])
        grid = self.css[self.css.index(".bookmarks-grid {"):]
        self.assertIn("overflow: visible", grid[:grid.index("}")])

    def test_the_list_scrolls_inside_the_card_and_has_level_colours(self):
        self.assertIn(".brief-list", self.css)
        self.assertIn("max-height", self.css)
        self.assertIn("overflow-y: auto", self.css)
        for level in ("brief-level-critical", "brief-level-warning"):
            with self.subTest(level=level):
                self.assertIn(level, self.css)
        responsive = self.css[self.css.index("@media (max-height: 850px)"):]
        self.assertIn(".brief-list", responsive)


class MetricsPageSourceTest(FrontEndSourceTest):
    """The metrics page, chart markup and deploy wiring, asserted at the source."""

    def test_page_loads_the_shared_modules_before_metrics_js(self):
        for name in ("ui-core.js", "dash-data.js", "stack.js"):
            with self.subTest(module=name):
                self.assertLess(
                    self.metrics_html.index('src="' + name + '"'),
                    self.metrics_html.index('src="metrics.js"'),
                )

    def test_device_rail_uses_the_same_containment_tree_as_the_dashboard(self):
        body = function_body(self.metrics_js, "metricsTree")

        self.assertIn("dashboardStack.group(devices, summary && summary.topology)", body)
        # A container row is the only handle on a container's series, so the
        # tree resolves each Docker row back to its device.
        self.assertIn("dashboardStack.render.dockerEngine(stack.containers,", body)
        self.assertIn("device.kind === 'container'", body)
        self.assertIn("'PVE host'", body)
        self.assertIn("'LXC guests'", body)
        self.assertIn("'Elsewhere'", body)

    def test_page_opens_on_the_device_that_needs_attention_not_a_container(self):
        # Arriving from the dashboard has no ?device=, and the summary is
        # ordered by first sighting, so ids[0] was a container whose single
        # volume hid the Volumes table.
        body = function_body(self.metrics_js, "defaultDeviceId")
        self.assertIn("warning.level === 'critical'", body)
        self.assertIn("device.kind === 'container' ? 3 : 2", body)
        self.assertIn("selectedId = defaultDeviceId(summary.devices)", self.metrics_js)
        self.assertNotIn("selectedId = ids[0]", self.metrics_js)

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
        for name in ("ui-core.js", "dash-data.js", "stack.js",
                     "metrics.html", "metrics.js", "metrics.css"):
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
        self.assertIn("fmtBytesCompact(disk.total)", body)
        self.assertIn('data-mount="', body)
        self.assertIn('class="disk-fill"', body)
        self.assertIn("clampPct(disk.pct)", body)
        self.assertIn("METRIC_WARN_PCT", body)
        self.assertIn("'—'", body)

        fmt = function_body(self.ui, "scaleBytes")
        self.assertIn("1024", fmt)
        self.assertIn("'TB'", fmt)
        # The dashboard keeps its compact policy through the shared scale.
        self.assertIn("fmtBytesCompact", self.js)

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
        self.assertIn("dockerEngine(stack.containers,", self.metrics_js)
        self.assertIn("device.kind === 'container'", self.metrics_js)

    def test_the_selectable_row_is_styled_not_like_a_default_button(self):
        self.assertIn(".docker-container.is-selectable", self.css)
        block = self.css.split(".docker-container.is-selectable {", 1)[1].split("}", 1)[0]
        self.assertIn("border: 0", block)
        self.assertIn("background: none", block)


class DeviceCountTest(FrontEndSourceTest):
    """Containers run on the counted Docker host, so n/m online stays a device count.

    The container agent pushes each container as a device so its CPU/RAM can be
    charted. That device row must not make the fleet look two machines bigger,
    nor turn the health dot amber when a container exits. Both rails share the
    filter so the dashboard and metrics page cannot drift apart.
    """

    def test_the_shared_helper_drops_container_kind(self):
        body = function_body(self.stack, "countedDevices")
        self.assertIn("device.kind !== 'container'", body)
        self.assertIn("Array.isArray", body)
        self.assertIn("countedDevices: countedDevices", self.stack)

    def test_dashboard_counts_through_the_shared_helper(self):
        for name in ("deviceCounts", "averageSample"):
            self.assertIn(
                "dashboardStack.countedDevices(",
                function_body(self.js, name),
                name,
            )
        # Both the rail summary and the toolbar health read deviceCounts, so
        # filtering there fixes the status and the device count together.
        self.assertIn("deviceCounts(devices)",
                      function_body(self.js, "networkSummary"))
        self.assertIn("deviceCounts(devices)",
                      function_body(self.js, "renderHealth"))

    def test_metrics_page_counts_through_the_shared_helper(self):
        for name in ("renderTiles", "renderDevices", "renderHealth"):
            self.assertIn(
                "dashboardStack.countedDevices(",
                function_body(self.metrics_js, name),
                name,
            )
        # The tree still receives every device so a container row can select
        # its own series; only the tallies are filtered.
        self.assertIn("metricsTree(devices)",
                      function_body(self.metrics_js, "renderDevices"))


# ---------- service checks ----------
# The store is a JSON file beside bookmarks.json. These test the reader, not a
# live probe: every entry must normalise to one shape the sampler and API share.

class ServiceCheckConfigTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name)
        self._old_data = app.DATA
        self._old_checks = app.CHECKS_FILE
        app.DATA = self.data
        app.CHECKS_FILE = self.data / "checks.json"

    def tearDown(self):
        app.DATA = self._old_data
        app.CHECKS_FILE = self._old_checks
        self._tmp.cleanup()

    def write(self, payload):
        app.CHECKS_FILE.write_text(
            payload if isinstance(payload, str) else json.dumps(payload)
        )

    def test_a_valid_entry_is_normalised_with_defaults(self):
        self.write({"checks": [{
            "id": "gitea",
            "url": "https://git.example/",
            "device_id": "agent:pve",
        }]})

        checks = app.load_checks()

        self.assertEqual(len(checks), 1)
        check = checks[0]
        self.assertEqual(check["id"], "gitea")
        self.assertEqual(check["name"], "gitea")
        self.assertEqual(check["method"], "GET")
        self.assertEqual(check["timeout_s"], 5.0)
        self.assertIsNone(check["interval_s"])
        self.assertEqual(check["device_id"], "agent:pve")
        self.assertFalse(check["external"])
        self.assertIsNone(check["expect_body"])
        self.assertEqual(check["status_ranges"], ((200, 299),))

    def test_missing_empty_and_corrupt_stores_are_empty_not_fatal(self):
        # Zero checks must work, and a bad file must never take the API down.
        self.assertEqual(app.load_checks(), [])

        self.write("{not json")
        self.assertEqual(app.load_checks(), [])

        self.write({"checks": []})
        self.assertEqual(app.load_checks(), [])

        self.write({"checks": "nope"})
        self.assertEqual(app.load_checks(), [])

        self.write({"other": [{"id": "x"}]})
        self.assertEqual(app.load_checks(), [])

    def test_invalid_entries_are_dropped_and_duplicate_ids_keep_the_first(self):
        self.write({"checks": [
            "not a dict",
            {"name": "no id", "url": "https://example.com/"},
            {"id": "", "url": "https://example.com/"},
            {"id": "no-url"},
            {"id": "bad-scheme", "url": "ftp://example.com/"},
            {"id": "relative", "url": "/api/health"},
            {"id": "first", "name": "First", "url": "https://one.example/"},
            {"id": "first", "name": "Duplicate", "url": "https://two.example/"},
        ]})

        checks = app.load_checks()

        self.assertEqual([check["id"] for check in checks], ["first"])
        self.assertEqual(checks[0]["name"], "First")
        self.assertEqual(checks[0]["url"], "https://one.example/")

    def test_status_expectations_methods_and_bounds_are_normalised(self):
        self.write({"checks": [
            {"id": "exact", "url": "https://example.com/", "expect_status": 204},
            {"id": "class", "url": "https://example.com/", "expect_status": "3xx"},
            {"id": "range", "url": "https://example.com/",
             "expect_status": "200-299"},
            {"id": "union", "url": "https://example.com/",
             "expect_status": [200, "3xx"]},
            {"id": "bad", "url": "https://example.com/", "expect_status": "nope"},
            {"id": "head", "url": "https://example.com/", "method": "head",
             "timeout_s": 999, "interval_s": 1},
            {"id": "post", "url": "https://example.com/", "method": "post"},
        ]})

        checks = {check["id"]: check for check in app.load_checks()}

        self.assertEqual(checks["exact"]["status_ranges"], ((204, 204),))
        self.assertEqual(checks["class"]["status_ranges"], ((300, 399),))
        self.assertEqual(checks["range"]["status_ranges"], ((200, 299),))
        self.assertEqual(
            checks["union"]["status_ranges"], ((200, 200), (300, 399))
        )
        self.assertEqual(checks["bad"]["status_ranges"], ((200, 299),))
        self.assertEqual(checks["head"]["method"], "HEAD")
        self.assertEqual(checks["head"]["timeout_s"], 60.0)
        self.assertEqual(checks["head"]["interval_s"], 10)
        # An unsupported method falls back to a harmless GET rather than
        # letting a config typo issue a mutating request.
        self.assertEqual(checks["post"]["method"], "GET")


class FakeProbeTransport:
    """A stand-in for urllib: records the call and returns or raises."""

    def __init__(self, result=(200, b"ok", None), error=None):
        self.result = result
        self.error = error
        self.calls = []

    def __call__(self, url, method, timeout):
        self.calls.append((url, method, timeout))
        if self.error is not None:
            raise self.error
        return self.result


class ServiceCheckProbeTest(unittest.TestCase):
    def check(self, **overrides):
        raw = {"id": "svc", "name": "Service", "url": "https://example.com/"}
        raw.update(overrides)
        return app._normalise_check(raw)

    def test_a_matching_probe_records_status_and_latency(self):
        transport = FakeProbeTransport((200, b"hello", None))

        result = app.probe_check(self.check(), transport=transport)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], 200)
        self.assertIsNone(result["error"])
        self.assertIsNone(result["cert_days"])
        self.assertIsInstance(result["latency_ms"], int)
        self.assertGreaterEqual(result["latency_ms"], 0)
        self.assertEqual(
            transport.calls, [("https://example.com/", "GET", 5.0)]
        )

    def test_a_status_outside_the_expectation_fails_with_its_code(self):
        transport = FakeProbeTransport((503, b"down", None))

        result = app.probe_check(self.check(), transport=transport)

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 503)
        self.assertEqual(result["error"], "HTTP 503")

    def test_an_explicit_status_class_is_honoured(self):
        transport = FakeProbeTransport((301, b"", None))

        result = app.probe_check(
            self.check(expect_status="3xx"), transport=transport
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], 301)

    def test_expect_body_must_appear_in_the_response(self):
        transport = FakeProbeTransport((200, b"healthy dashboard", None))

        good = app.probe_check(
            self.check(expect_body="healthy"), transport=transport
        )
        bad = app.probe_check(
            self.check(expect_body="maintenance"), transport=transport
        )

        self.assertTrue(good["ok"])
        self.assertFalse(bad["ok"])
        self.assertTrue(bad["error"].startswith("body did not contain"))

    def test_a_network_error_is_a_failed_sample_not_an_exception(self):
        transport = FakeProbeTransport(error=TimeoutError("timed out"))

        result = app.probe_check(self.check(), transport=transport)

        self.assertFalse(result["ok"])
        self.assertIsNone(result["status"])
        self.assertIn("TimeoutError", result["error"])
        self.assertIn("timed out", result["error"])

    def test_certificate_not_after_becomes_days_remaining(self):
        now = NOW
        not_after = now + 10 * 86400
        transport = FakeProbeTransport((200, b"", not_after))

        result = app.probe_check(self.check(), now=now, transport=transport)

        self.assertEqual(result["cert_days"], 10.0)

    def test_ssl_not_after_text_is_parsed_with_the_stdlib(self):
        # Expected value comes from ssl.cert_time_to_seconds, not from the
        # implementation's own arithmetic.
        import ssl
        not_after = ssl.cert_time_to_seconds("Jun  1 12:00:00 2027 GMT")

        parsed = app._cert_not_after({"notAfter": "Jun  1 12:00:00 2027 GMT"})

        self.assertEqual(parsed, not_after)

    def test_an_expired_certificate_reports_negative_days(self):
        now = NOW
        transport = FakeProbeTransport((200, b"", now - 2 * 86400))

        result = app.probe_check(self.check(), now=now, transport=transport)

        self.assertEqual(result["cert_days"], -2.0)

    def test_method_and_timeout_reach_the_transport(self):
        transport = FakeProbeTransport((200, b"", None))

        app.probe_check(
            self.check(method="HEAD", timeout_s=2), transport=transport
        )

        self.assertEqual(transport.calls, [("https://example.com/", "HEAD", 2.0)])

    def test_a_redirect_is_observed_rather_than_followed(self):
        handler = app._NoRedirect()

        self.assertIsNone(handler.redirect_request(
            None, None, 301, "Moved", None, "https://elsewhere.example/"
        ))

    def test_certificate_lifetime_is_read_from_the_annotated_response(self):
        not_after = NOW + 5 * 86400
        response = mock.Mock()
        response.dash_connection = mock.Mock(cert_not_after=not_after)

        self.assertEqual(app._cert_from_response(response), not_after)
        self.assertIsNone(app._cert_from_response(object()))


class CheckSampleStoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name)
        self._old_data = app.DATA
        self._old_metrics = app.METRICS_FILE
        app.DATA = self.data
        app.METRICS_FILE = self.data / "metrics.db"
        app._last_prune["at"] = 0.0

    def tearDown(self):
        app.DATA = self._old_data
        app.METRICS_FILE = self._old_metrics
        app._last_prune["at"] = 0.0
        self._tmp.cleanup()

    def rows(self):
        con = app._connect_metrics()
        try:
            return con.execute(
                "SELECT check_id, ts, ok, status, latency_ms, cert_days, error "
                "FROM check_samples ORDER BY check_id"
            ).fetchall()
        finally:
            con.close()

    def sample(self, check_id, ok=True, status=200, **extra):
        return {
            "id": check_id, "ok": ok, "status": status,
            "latency_ms": extra.get("latency_ms", 12),
            "cert_days": extra.get("cert_days"),
            "error": extra.get("error"),
        }

    def test_a_probe_lands_in_its_bucket_with_every_field(self):
        app.record_check_samples(
            [self.sample("a", cert_days=9.5)], now=NOW
        )

        bucket = int(NOW) - (int(NOW) % app.CHECK_BUCKET_SECONDS)
        self.assertEqual(
            self.rows(), [("a", bucket, 1, 200, 12, 9.5, None)]
        )

    def test_a_repeat_inside_a_bucket_replaces_the_sample(self):
        app.record_check_samples([self.sample("a")], now=NOW)
        app.record_check_samples(
            [self.sample("a", ok=False, status=503, error="HTTP 503",
                         latency_ms=99)],
            now=NOW + 10,
        )

        bucket = int(NOW) - (int(NOW) % app.CHECK_BUCKET_SECONDS)
        self.assertEqual(
            self.rows(), [("a", bucket, 0, 503, 99, None, "HTTP 503")]
        )

    def test_the_next_bucket_appends_rather_than_overwriting(self):
        app.record_check_samples([self.sample("a")], now=NOW)
        app.record_check_samples([self.sample("a")], now=NOW + 60)

        buckets = [row[1] for row in self.rows()]
        self.assertEqual(len(buckets), 2)
        self.assertEqual(buckets[1] - buckets[0], app.CHECK_BUCKET_SECONDS)

    def test_retention_prunes_raw_check_samples_on_the_shared_pass(self):
        app.record_check_samples([self.sample("recent")], now=NOW)
        old = int(NOW) - (app.CHECK_RETENTION_DAYS + 1) * 86400
        con = app._connect_metrics()
        try:
            con.execute(
                "INSERT INTO check_samples "
                "(check_id, ts, ok, status, latency_ms, cert_days, error) "
                "VALUES (?, ?, 1, 200, 5, NULL, NULL)",
                ("old", old),
            )
            con.commit()
        finally:
            con.close()
        self.assertEqual(len(self.rows()), 2)

        app.prune_metrics(NOW)

        self.assertEqual([row[0] for row in self.rows()], ["recent"])


class ServiceCheckVerdictTest(unittest.TestCase):
    def check(self, **overrides):
        raw = {"id": "svc", "url": "https://example.com/"}
        raw.update(overrides)
        return app._normalise_check(raw)

    def test_verdict_precedence(self):
        # unknown: no samples at all, whatever the device says.
        self.assertEqual(app.check_verdict(self.check(), None, None), "unknown")
        self.assertEqual(app.check_verdict(self.check(), None, False), "unknown")

        # host_down: a tied device that is known offline outranks the sample.
        self.assertEqual(
            app.check_verdict(self.check(device_id="agent:pve"), True, False),
            "host_down",
        )
        self.assertEqual(
            app.check_verdict(
                self.check(device_id="agent:pve", external=True), False, False
            ),
            "host_down",
        )

        # ok: the newest sample passed while its host is up (or untied).
        self.assertEqual(app.check_verdict(self.check(), True, None), "ok")
        self.assertEqual(
            app.check_verdict(self.check(device_id="agent:pve"), True, True),
            "ok",
        )

        # service_down: the host is online (or unknown) and the probe failed.
        self.assertEqual(
            app.check_verdict(self.check(device_id="agent:pve"), False, True),
            "service_down",
        )
        self.assertEqual(app.check_verdict(self.check(), False, None),
                         "service_down")
        # An untied failing check that is not external is still a service.
        self.assertEqual(
            app.check_verdict(self.check(device_id="gone"), False, None),
            "service_down",
        )

        # path_down: an external probe failed.
        self.assertEqual(
            app.check_verdict(self.check(external=True), False, None),
            "path_down",
        )


class ServiceCheckSamplerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name)
        self._old_data = app.DATA
        self._old_metrics = app.METRICS_FILE
        self._old_checks = app.CHECKS_FILE
        self._old_agents = app.AGENTS_FILE
        app.DATA = self.data
        app.METRICS_FILE = self.data / "metrics.db"
        app.CHECKS_FILE = self.data / "checks.json"
        app.AGENTS_FILE = self.data / "agents.json"
        app._last_prune["at"] = 0.0
        app._check_state.update(results={})

    def tearDown(self):
        app.DATA = self._old_data
        app.METRICS_FILE = self._old_metrics
        app.CHECKS_FILE = self._old_checks
        app.AGENTS_FILE = self._old_agents
        app._last_prune["at"] = 0.0
        app._check_state.update(results={})
        self._tmp.cleanup()

    def write_checks(self, checks):
        app.CHECKS_FILE.write_text(json.dumps({"checks": checks}))

    def test_a_pass_probes_every_due_check_and_stores_the_samples(self):
        self.write_checks([
            {"id": "one", "url": "https://one.example/"},
            {"id": "two", "url": "https://two.example/"},
        ])
        transport = FakeProbeTransport((200, b"ok", None))

        probed = app.run_checks_once(now=NOW, transport=transport)

        self.assertEqual(probed, 2)
        self.assertEqual(len(transport.calls), 2)
        con = app._connect_metrics()
        try:
            rows = con.execute(
                "SELECT check_id, ok, status FROM check_samples ORDER BY check_id"
            ).fetchall()
        finally:
            con.close()
        self.assertEqual(rows, [("one", 1, 200), ("two", 1, 200)])
        self.assertEqual(app._check_state["results"]["one"]["verdict"], "ok")

    def test_a_per_check_interval_skips_a_recent_sample(self):
        self.write_checks([
            {"id": "slow", "url": "https://slow.example/", "interval_s": 300},
        ])
        first = FakeProbeTransport((200, b"", None))
        early = FakeProbeTransport((500, b"", None))

        self.assertEqual(
            app.run_checks_once(now=NOW, transport=first), 1
        )
        self.assertEqual(
            app.run_checks_once(now=NOW + 60, transport=early), 0
        )
        self.assertEqual(early.calls, [])
        self.assertEqual(
            app.run_checks_once(now=NOW + 300, transport=early), 1
        )

    def test_an_offline_tied_device_yields_host_down(self):
        self.write_checks([
            {"id": "pve-web", "url": "https://pve.example/",
             "device_id": "agent:pve"},
        ])
        app.AGENTS_FILE.write_text(json.dumps({
            "pve": {"name": "pve", "kind": "host", "last_seen": NOW - 1000},
        }))

        with frozen_time():
            app.run_checks_once(
                now=NOW, transport=FakeProbeTransport((200, b"", None))
            )

        self.assertEqual(
            app._check_state["results"]["pve-web"]["verdict"], "host_down"
        )

    def test_zero_checks_does_not_create_a_store(self):
        self.assertEqual(app.run_checks_once(now=NOW), 0)
        self.assertFalse(app.METRICS_FILE.exists())
        self.assertEqual(app._check_state["results"], {})

    def test_a_not_due_check_rehydrates_stored_state_after_restart(self):
        self.write_checks([{
            "id": "slow", "url": "https://slow.example/", "interval_s": 3600,
        }])
        app.record_check_samples(
            [{"id": "slow", "ok": False, "status": 503, "latency_ms": 9,
              "cert_days": 5.0, "error": "HTTP 503"}],
            now=NOW,
        )
        app._check_state["results"] = {}  # simulate a process restart
        transport = FakeProbeTransport((200, b"", None))

        probed = app.run_checks_once(now=NOW + 60, transport=transport)

        self.assertEqual(probed, 0)
        self.assertEqual(transport.calls, [])
        state = app._check_state["results"]["slow"]
        self.assertFalse(state["ok"])
        self.assertEqual(state["verdict"], "service_down")
        codes = [warning["code"] for warning in app.check_warnings()]
        self.assertIn("service", codes)
        self.assertIn("cert", codes)

    def test_a_not_due_check_still_follows_the_live_host_state(self):
        self.write_checks([{
            "id": "pve-web", "url": "https://pve.example/",
            "device_id": "agent:pve", "interval_s": 3600,
        }])
        app.record_check_samples(
            [{"id": "pve-web", "ok": True, "status": 200, "latency_ms": 9,
              "cert_days": None, "error": None}],
            now=NOW,
        )
        app.AGENTS_FILE.write_text(json.dumps({
            "pve": {"name": "pve", "kind": "host", "last_seen": NOW - 1000},
        }))
        transport = FakeProbeTransport((200, b"", None))

        with frozen_time():
            probed = app.run_checks_once(now=NOW + 60, transport=transport)

        self.assertEqual(probed, 0)
        self.assertEqual(
            app._check_state["results"]["pve-web"]["verdict"], "host_down"
        )
        self.assertIn(
            "service", [warning["code"] for warning in app.check_warnings()]
        )


class ServiceChecksPayloadTest(ApiTestCase):
    REQUIRED = {
        "id", "name", "url", "device_id", "ok", "status", "latency_ms",
        "cert_days", "error", "last_change", "uptime_24h", "uptime_7d",
        "verdict",
    }

    def sample(self, check_id, ok, status=200, error=None, cert_days=None):
        return {
            "id": check_id, "ok": ok, "status": status, "latency_ms": 12,
            "cert_days": cert_days, "error": error,
        }

    def test_empty_store_returns_an_empty_checks_list(self):
        status, data = self.get_json("/api/checks")

        self.assertEqual(status, 200)
        self.assertEqual(data["checks"], [])
        self.assertIn("generated", data)

    def test_a_corrupt_store_is_an_empty_list_not_a_500(self):
        (self.data / "checks.json").write_text("{not json")

        status, data = self.get_json("/api/checks")

        self.assertEqual(status, 200)
        self.assertEqual(data["checks"], [])

    def test_the_endpoint_serves_the_documented_shape(self):
        self.write_checks([{
            "id": "web", "name": "Web", "url": "https://web.example/health",
            "device_id": "docker01",
        }])
        now = time.time()
        app.record_check_samples(
            [{"id": "web", "ok": True, "status": 200, "latency_ms": 12,
              "cert_days": 9.0, "error": None}],
            now=now,
        )

        status, data = self.get_json("/api/checks")

        self.assertEqual(status, 200)
        self.assertEqual(len(data["checks"]), 1)
        entry = data["checks"][0]
        self.assertEqual(set(entry), self.REQUIRED)
        self.assertEqual(entry["id"], "web")
        self.assertEqual(entry["name"], "Web")
        self.assertEqual(entry["url"], "https://web.example/health")
        self.assertEqual(entry["device_id"], "docker01")
        self.assertIs(entry["ok"], True)
        self.assertEqual(entry["status"], 200)
        self.assertEqual(entry["uptime_24h"], 100.0)
        self.assertEqual(entry["uptime_7d"], 100.0)
        self.assertEqual(entry["verdict"], "ok")

    def payload(self, check, samples, now=NOW):
        self.write_checks([check])
        for sample, ts in samples:
            app.record_check_samples([sample], now=ts)
        with frozen_time():
            return app.checks_payload(now=now)["checks"][0]

    def test_uptime_math_uses_the_24h_and_7d_windows(self):
        entry = self.payload(
            {"id": "web", "url": "https://web.example/"},
            [
                (self.sample("web", True), NOW - 2 * 86400),
                (self.sample("web", False), NOW - 3 * 3600),
                (self.sample("web", True), NOW - 2 * 3600),
                (self.sample("web", True), NOW - 3600),
            ],
        )

        # 2 of 3 samples inside 24 h, 3 of 4 inside 7 d.
        self.assertEqual(entry["uptime_24h"], 66.7)
        self.assertEqual(entry["uptime_7d"], 75.0)

    def test_last_change_is_when_the_current_run_began(self):
        entry = self.payload(
            {"id": "web", "url": "https://web.example/"},
            [
                (self.sample("web", True), NOW - 4 * 3600),
                (self.sample("web", True), NOW - 3 * 3600),
                (self.sample("web", False), NOW - 2 * 3600),
                (self.sample("web", True), NOW - 3600),
            ],
        )

        change = int(NOW - 3600)
        change -= change % app.CHECK_BUCKET_SECONDS
        self.assertEqual(entry["last_change"], change)

    def test_a_check_with_no_samples_is_unknown(self):
        entry = self.payload(
            {"id": "new", "url": "https://new.example/"}, []
        )

        self.assertIsNone(entry["ok"])
        self.assertIsNone(entry["status"])
        self.assertIsNone(entry["latency_ms"])
        self.assertIsNone(entry["cert_days"])
        self.assertIsNone(entry["error"])
        self.assertIsNone(entry["last_change"])
        self.assertIsNone(entry["uptime_24h"])
        self.assertIsNone(entry["uptime_7d"])
        self.assertEqual(entry["verdict"], "unknown")

    def test_an_offline_host_turns_the_tied_check_host_down(self):
        app.AGENTS_FILE.write_text(json.dumps({
            "off": {"name": "off", "kind": "host", "last_seen": NOW - 1000},
        }))
        entry = self.payload(
            {"id": "web", "url": "https://web.example/", "device_id": "agent:off"},
            [(self.sample("web", True), NOW)],
        )

        self.assertEqual(entry["verdict"], "host_down")

    def test_an_external_failure_is_path_down_and_keeps_the_error(self):
        entry = self.payload(
            {"id": "wan", "url": "https://wan.example/", "external": True},
            [(self.sample("wan", False, status=None,
                          error="TimeoutError: timed out"), NOW)],
        )

        self.assertEqual(entry["verdict"], "path_down")
        self.assertEqual(entry["error"], "TimeoutError: timed out")

    def test_a_tied_online_host_failing_is_service_down(self):
        entry = self.payload(
            {"id": "web", "url": "https://web.example/",
             "device_id": "docker01"},
            [(self.sample("web", False, status=503,
                          error="HTTP 503"), NOW)],
        )

        self.assertEqual(entry["verdict"], "service_down")
        self.assertEqual(entry["status"], 503)


class ServiceCheckWarningsTest(ApiTestCase):
    def run_check(self, check, transport):
        self.write_checks([check])
        app.run_checks_once(now=time.time(), transport=transport)
        app._cache.update(at=0.0, body=None)

    def test_a_failing_check_appears_in_stats_warnings(self):
        self.run_check(
            {"id": "web", "name": "Web", "url": "https://web.example/"},
            FakeProbeTransport((503, b"down", None)),
        )

        warnings = app.stats()["warnings"]

        self.assertEqual(len(warnings), 1)
        warning = warnings[0]
        self.assertEqual(
            set(warning), {"code", "level", "value", "threshold", "text"}
        )
        self.assertEqual(warning["code"], "service")
        self.assertEqual(warning["level"], "critical")
        self.assertEqual(warning["value"], 503)
        self.assertIn("Web", warning["text"])
        self.assertIn("HTTP 503", warning["text"])

    def test_a_certificate_inside_fourteen_days_is_a_warning(self):
        now = time.time()
        self.run_check(
            {"id": "web", "name": "Web", "url": "https://web.example/"},
            FakeProbeTransport((200, b"", now + 10 * 86400)),
        )

        warnings = app.stats()["warnings"]

        self.assertEqual(len(warnings), 1)
        warning = warnings[0]
        self.assertEqual(warning["code"], "cert")
        self.assertEqual(warning["level"], "warning")
        self.assertEqual(warning["value"], 10.0)
        self.assertEqual(warning["threshold"], app.WARN_CERT_DAYS)
        self.assertIn("Web", warning["text"])
        self.assertIn("10d", warning["text"])

    def test_a_certificate_inside_half_the_window_is_critical(self):
        now = time.time()
        self.run_check(
            {"id": "web", "name": "Web", "url": "https://web.example/"},
            FakeProbeTransport((200, b"", now + 2 * 86400)),
        )

        warning = app.stats()["warnings"][0]

        self.assertEqual(warning["code"], "cert")
        self.assertEqual(warning["level"], "critical")

    def test_a_healthy_certificate_and_check_produce_no_warning(self):
        now = time.time()
        self.run_check(
            {"id": "web", "name": "Web", "url": "https://web.example/"},
            FakeProbeTransport((200, b"", now + 60 * 86400)),
        )

        self.assertEqual(app.stats()["warnings"], [])

    def test_a_host_down_check_is_a_warning_naming_the_host(self):
        app.AGENTS_FILE.write_text(json.dumps({
            "pve": {"name": "pve", "kind": "host", "last_seen": 0},
        }))
        self.run_check(
            {"id": "web", "name": "Web", "url": "https://web.example/",
             "device_id": "agent:pve"},
            FakeProbeTransport((200, b"", None)),
        )

        warning = app.stats()["warnings"][0]

        self.assertIn("host offline", warning["text"])

    def test_the_stats_endpoint_carries_the_warning_list(self):
        self.run_check(
            {"id": "web", "name": "Web", "url": "https://web.example/"},
            FakeProbeTransport((500, b"", None)),
        )

        status, data = self.get_json("/api/stats")

        self.assertEqual(status, 200)
        self.assertEqual(data["warnings"][0]["code"], "service")

    def test_no_checks_means_an_empty_warning_list(self):
        self.assertEqual(app.stats()["warnings"], [])

    def test_stats_reads_the_live_check_state_not_the_history(self):
        # The rail polls every 5 s; a check-history scan there would be a cost
        # the device warnings deliberately avoid.
        source = Path(app.__file__).read_text()
        start = source.index("def check_warnings(")
        end = source.index("\ndef ", start + 1)
        body = source[start:end]

        self.assertIn("_check_state", body)
        self.assertNotIn("check_samples", body)
        self.assertNotIn("_connect_metrics", body)


class BookmarkCheckReferenceTest(ApiTestCase):
    STORE = {
        "work": [{
            "category": "Services",
            "items": [
                {"title": "Gitea", "url": "https://git.example/",
                 "check": "gitea"},
                {"title": "Docs", "url": "https://docs.example.com"},
            ],
        }]
    }

    def test_api_passes_the_check_reference_through_unchanged(self):
        self.write_bookmarks(self.STORE)

        status, data = self.get_json("/api/bookmarks")

        self.assertEqual(status, 200)
        items = data["work"][0]["items"]
        self.assertEqual(items[0]["check"], "gitea")
        self.assertNotIn("check", items[1])

    def test_the_injected_first_paint_payload_keeps_the_reference(self):
        self.write_bookmarks(self.STORE)

        with urllib.request.urlopen(self.base + "/", timeout=5) as resp:
            html = resp.read().decode()

        self.assertIn('"check": "gitea"', html)
        # The injected copy is the same store the API serves, not a stripped
        # projection that would need a second request to learn the check id.
        self.assertNotIn("__BOOKMARKS_JSON__", html)


class ServiceChecksUiTest(FrontEndSourceTest):
    """The dashboard is health-aware: rows, tiles and the Services rail card.

    No JS runtime here, so these pin the seams: one check lookup, one badge
    renderer, one Services renderer and the poll that feeds them.
    """

    def test_all_tools_rows_and_launchpad_tiles_render_a_check_badge(self):
        self.assertIn("checkBadge(", function_body(self.js, "toolRow"))
        self.assertIn("checkBadge(", function_body(self.js, "launchpadTile"))

    def test_the_badge_is_not_colour_only(self):
        badge = function_body(self.js, "checkBadge")
        dot = function_body(self.js, "statusDotHTML")
        defs = self.js[self.js.index("const VERDICT_DEFS"):]
        defs = defs[:defs.index("\n};") + 3]

        # Every verdict names a distinct-ish glyph; unknown must not borrow the
        # failure mark, and path_down must differ from service_down on a tile.
        self.assertRegex(defs, r"unknown:\s*\{\s*label:\s*'no data',\s*mark:\s*'\?'")
        self.assertRegex(defs, r"service_down:\s*\{[^}]*mark:\s*'!'")
        self.assertRegex(defs, r"path_down:\s*\{[^}]*mark:\s*'~'")
        self.assertIn("aria-hidden", dot)
        self.assertIn(".mark", dot)

        # Screen-reader text plus a hover title name the state.
        self.assertIn("visually-hidden", badge)
        self.assertIn("title=", badge)
        self.assertIn("verdictLabel(", badge)

    def test_the_check_payload_is_fetched_by_the_existing_poller(self):
        refresh = function_body(self.js, "refreshDevices")

        self.assertIn("dashData.read('/api/checks'", refresh)
        self.assertIn("applyChecks(", refresh)
        self.assertIn("checkMap(", function_body(self.js, "applyChecks"))
        # Rendering is refreshed once the check state arrives, because the
        # first paint only has the bookmark store.
        apply = function_body(self.js, "applyChecks")
        self.assertIn("renderAllTools(", apply)
        self.assertIn("renderLaunchpad(", apply)
        # Latency and uptime change every poll; the All tools list must not be
        # rebuilt (and its scroll reset) unless a verdict changed.
        self.assertIn("checkSignatureOf(", apply)
        self.assertIn("signature !== checkSignature", apply)

    def test_the_services_card_shows_latency_uptime_cert_and_verdict(self):
        meta = function_body(self.js, "serviceMeta")
        self.assertIn("latency_ms", meta)
        self.assertIn("uptime_24h", meta)
        self.assertIn("cert_days", meta)

        row = function_body(self.js, "serviceRow")
        self.assertIn("service-verdict", row)
        self.assertIn("verdictLabel(", row)
        self.assertIn("serviceMeta(", row)

        render = function_body(self.js, "renderServices")
        self.assertIn("servicesListEl", render)
        self.assertIn("servicesSummary(", render)
        self.assertIn("services-empty", render)

    def test_every_verdict_has_a_label(self):
        labels = function_body(self.js, "verdictLabel")
        source = self.js[self.js.index("VERDICT_DEFS"):]
        for verdict in ("ok", "service_down", "host_down", "path_down", "unknown"):
            with self.subTest(verdict=verdict):
                self.assertIn(verdict + ":", source)
        self.assertIn("VERDICT_DEFS[", labels)

    def test_the_services_card_sits_below_network_in_the_rail(self):
        aside = self.html.index('<aside class="rail">')
        network = self.html.index('id="network-title"', aside)
        services = self.html.index('class="services-panel card"', aside)

        self.assertGreater(services, network)
        panel = self.html[services:self.html.index("</section>", services)]
        self.assertIn('id="services-title"', panel)
        self.assertIn('id="services-summary"', panel)
        self.assertIn('id="services-list"', panel)
        self.assertIn('aria-labelledby="services-title"', panel)

    def test_service_css_is_compact_presentational_and_shape_coded(self):
        self.assertIn(".services-panel {", self.css)
        self.assertIn(".service-row {", self.css)
        self.assertIn(".service-meta {", self.css)
        self.assertIn(".status-dot {", self.css)
        self.assertIn(".visually-hidden {", self.css)

        # host_down is a square, path_down a ring, and the unknown dot is
        # hollow: the state is readable without relying on hue.
        self.assertRegex(
            self.css,
            r"\.status-dot\.is-host-down\s*\{[^}]*border-radius:\s*2px",
        )
        self.assertRegex(
            self.css,
            r"\.status-dot\.is-path-down\s*\{[^}]*border:\s*2px solid",
        )
        self.assertRegex(
            self.css,
            r"\.status-dot\.is-unknown\s*\{[^}]*border:\s*1px dashed",
        )
        # The rail stays a scroll container, so the page itself never scrolls.
        self.assertRegex(self.css, r"\.rail\s*\{[^}]*overflow-y:\s*auto")

    def test_no_new_icon_or_font_request_was_added(self):
        for source in (self.html, self.js):
            self.assertNotIn("fonts.googleapis.com", source)
            self.assertNotIn("<img", source)
            self.assertNotIn("@font-face", source)


class HealthPayloadCleaningTest(unittest.TestCase):
    """The agent wire blocks are untrusted input: clean, clamp, drop junk."""

    def test_smart_cleans_valid_fields_and_drops_malformed_entries(self):
        cleaned = app.clean_smart([
            {"device": "/dev/sda", "health": "PASSED", "temperature_c": 34,
             "reallocated": 0, "pending": 0, "power_on_hours": 12000},
            {"device": "  ", "health": "FAILED"},
            {"health": "FAILED"},
            "not a dict",
            {"device": "/dev/sdb", "health": 7, "temperature_c": -5,
             "reallocated": -2, "pending": True, "power_on_hours": "9"},
        ])
        self.assertEqual(cleaned, [
            {"device": "/dev/sda", "health": "PASSED", "temperature_c": 34.0,
             "reallocated": 0, "pending": 0, "power_on_hours": 12000},
            # A negative temperature is not a reading; the row keeps only the
            # identifier rather than fabricating 0 °C.
            {"device": "/dev/sdb"},
        ])

    def test_smart_clamps_and_caps(self):
        raw = [{"device": "/dev/sd%d" % i, "temperature_c": 999999,
                "reallocated": 10 ** 15, "pending": 10 ** 15,
                "power_on_hours": 10 ** 15}
               for i in range(20)]
        cleaned = app.clean_smart(raw)
        self.assertEqual(len(cleaned), app.MAX_HEALTH_ITEMS)
        self.assertEqual(cleaned[0]["temperature_c"], 200.0)
        self.assertEqual(cleaned[0]["reallocated"], app.MAX_HEALTH_COUNT)
        self.assertEqual(cleaned[0]["pending"], app.MAX_HEALTH_COUNT)
        self.assertEqual(cleaned[0]["power_on_hours"], app.MAX_HEALTH_COUNT)

    def test_zfs_cleans_clamps_and_requires_a_pool(self):
        cleaned = app.clean_zfs([
            {"pool": "tank", "state": "ONLINE", "capacity_pct": 42.5,
             "scrub_age_s": 3600, "errors": 0},
            {"pool": "", "state": "ONLINE"},
            {"state": "ONLINE"},
            {"pool": "doomed", "state": 5, "capacity_pct": 140,
             "scrub_age_s": -1, "errors": 10 ** 15},
        ])
        self.assertEqual(cleaned, [
            {"pool": "tank", "state": "ONLINE", "capacity_pct": 42.5,
             "scrub_age_s": 3600, "errors": 0},
            {"pool": "doomed", "capacity_pct": 100.0,
             "errors": app.MAX_HEALTH_COUNT},
        ])
        self.assertEqual(len(app.clean_zfs(
            [{"pool": "p%d" % i} for i in range(20)])), app.MAX_HEALTH_ITEMS)

    def test_ups_is_a_single_clamped_object_or_none(self):
        self.assertEqual(app.clean_ups({
            "status": "OL", "charge_pct": 120, "runtime_s": -1,
            "load_pct": -5, "battery_low": True,
        }), {"status": "OL", "charge_pct": 100.0, "battery_low": True})
        self.assertIsNone(app.clean_ups({}))
        self.assertIsNone(app.clean_ups({"charge_pct": "90"}))
        self.assertIsNone(app.clean_ups(["not", "an", "object"]))

    def test_backup_requires_a_positive_absolute_timestamp(self):
        self.assertEqual(
            app.clean_backup({"last_success_ts": 1700000000, "tool": "restic"}),
            {"last_success_ts": 1700000000, "tool": "restic"},
        )
        for raw in ({}, {"last_success_ts": 0}, {"last_success_ts": -5},
                    {"last_success_ts": "yesterday"}, {"last_success_ts": True},
                    None):
            self.assertIsNone(app.clean_backup(raw), raw)

    def test_updates_are_clamped_counters_and_a_real_bool(self):
        self.assertEqual(
            app.clean_updates({"pending": 5, "security": 2,
                               "reboot_required": True}),
            {"pending": 5, "security": 2, "reboot_required": True},
        )
        self.assertEqual(
            app.clean_updates({"pending": 10 ** 15, "security": -1,
                               "reboot_required": "yes"}),
            {"pending": app.MAX_HEALTH_COUNT},
        )
        self.assertIsNone(app.clean_updates({}))
        self.assertIsNone(app.clean_updates("pending"))

    def test_clean_health_keeps_only_present_nonempty_blocks(self):
        cleaned = app.clean_health({
            "smart": [{"device": "/dev/sda", "health": "PASSED"}],
            "zfs": [],
            "ups": {"status": "OL", "charge_pct": 100},
            "backup": {"last_success_ts": 1700000000},
            "updates": {"pending": 3},
        })
        self.assertEqual(set(cleaned), {"smart", "ups", "backup", "updates"})
        self.assertEqual(cleaned["smart"][0]["device"], "/dev/sda")
        self.assertEqual(app.clean_health({"smart": "nope", "ups": []}), {})
        self.assertEqual(app.clean_health(None), {})


class HealthStorageTest(ApiTestCase):
    """device_health keeps one row per device/kind per bucket, like volumes."""

    BASE = int(NOW) - (int(NOW) % 3600)

    def device(self, health=None, device_id="agent:healthhost", **extra):
        record = {
            "id": device_id, "name": "healthhost", "kind": "pc",
            "online": True, "cpu_pct": 1.0, "mem_pct": 2.0,
            "mem_used": 1, "mem_total": 2, "disk_pct": 3.0,
            "uptime_s": 4, "last_seen": self.BASE,
        }
        if health is not None:
            record["health"] = health
        record.update(extra)
        return record

    def sample(self, health, now):
        app.record_samples([self.device(health)], now=now)

    def health_rows(self, device_id="agent:healthhost"):
        con = app._connect_metrics()
        try:
            return con.execute(
                "SELECT ts, kind, payload_json FROM device_health "
                "WHERE device_id = ? ORDER BY ts, kind", (device_id,)
            ).fetchall()
        finally:
            con.close()

    def test_each_cleaned_block_is_one_row_in_the_sample_bucket(self):
        self.sample({
            "smart": [{"device": "/dev/sda", "health": "PASSED", "pending": 0}],
            "zfs": [{"pool": "tank", "state": "ONLINE", "errors": 0}],
            "ups": {"status": "OL", "charge_pct": 100},
            "backup": {"last_success_ts": self.BASE - 3600, "tool": "restic"},
            "updates": {"pending": 2, "reboot_required": False},
        }, now=self.BASE + 10)

        rows = self.health_rows()
        self.assertEqual([row[1] for row in rows],
                         ["backup", "smart", "updates", "ups", "zfs"])
        self.assertTrue(all(row[0] == self.BASE for row in rows))
        payloads = {kind: json.loads(blob) for _ts, kind, blob in rows}
        self.assertEqual(payloads["smart"][0]["device"], "/dev/sda")
        self.assertEqual(payloads["backup"]["last_success_ts"],
                         self.BASE - 3600)

    def test_a_repeat_inside_the_bucket_replaces_the_health_rows(self):
        self.sample({"smart": [{"device": "/dev/sda", "health": "PASSED"}],
                     "zfs": [{"pool": "tank", "state": "ONLINE"}]}, now=self.BASE)
        self.sample({"smart": [{"device": "/dev/sda", "health": "FAILED"}]},
                    now=self.BASE + 30)

        rows = self.health_rows()
        self.assertEqual([row[1] for row in rows], ["smart"])
        self.assertEqual(json.loads(rows[0][2])[0]["health"], "FAILED")

    def test_latest_health_is_kept_per_kind_and_history_is_bounded(self):
        self.sample({"smart": [{"device": "/dev/sda", "health": "PASSED"}],
                     "zfs": [{"pool": "tank", "state": "ONLINE"}]}, now=self.BASE)
        self.sample({"smart": [{"device": "/dev/sda", "health": "FAILED"}]},
                    now=self.BASE + 60)
        self.sample({"smart": [{"device": "/dev/sda", "health": "PASSED"}]},
                    now=self.BASE + 120)

        con = app._connect_metrics()
        try:
            latest = app._latest_health(con, "agent:healthhost")
            history = app._health_history(
                con, "agent:healthhost", self.BASE - 60, self.BASE + 180, limit=2
            )
        finally:
            con.close()

        self.assertEqual(latest["ts"], self.BASE + 120)
        self.assertEqual(latest["smart"][0]["health"], "PASSED")
        self.assertEqual(latest["zfs"][0]["pool"], "tank")  # last known
        self.assertEqual([entry["ts"] for entry in history],
                         [self.BASE + 60, self.BASE + 120])
        self.assertEqual(history[0]["smart"][0]["health"], "FAILED")

    def test_retention_prunes_health_rows_with_the_other_raw_tables(self):
        self.sample({"smart": [{"device": "/dev/sda", "health": "PASSED"}]},
                    now=self.BASE - app.HISTORY_SECONDS - 600)
        self.assertEqual(len(self.health_rows()), 1)

        app._last_prune["at"] = 0.0
        self.sample({"smart": [{"device": "/dev/sda", "health": "PASSED"}]},
                    now=self.BASE)

        rows = self.health_rows()
        self.assertEqual(len(rows), 1)
        self.assertGreaterEqual(rows[0][0], self.BASE - 60)

    def test_a_push_keeps_the_last_reading_for_a_kind_it_did_not_report(self):
        first = {"name": "healthhost", "kind": "pc", "cpu_pct": 1.0,
                 "smart": [{"device": "/dev/sda", "health": "FAILED"}],
                 "backup": {"last_success_ts": self.BASE - 3600}}
        second = {"name": "healthhost", "kind": "pc", "cpu_pct": 2.0,
                  "smart": [{"device": "/dev/sda", "health": "PASSED"}]}
        third = {"name": "healthhost", "kind": "pc", "cpu_pct": 3.0,
                 "backup": []}

        self.post(json.dumps(first).encode(), "/api/push/healthhost")
        self.post(json.dumps(second).encode(), "/api/push/healthhost")
        health = app.load_agents()["healthhost"]["health"]
        self.assertEqual(health["smart"][0]["health"], "PASSED")
        self.assertEqual(health["backup"]["last_success_ts"], self.BASE - 3600)

        self.post(json.dumps(third).encode(), "/api/push/healthhost")
        health = app.load_agents()["healthhost"]["health"]
        self.assertNotIn("backup", health)
        self.assertIn("smart", health)

    def test_an_old_agent_with_no_health_blocks_still_pushes(self):
        body = {"name": "plain", "kind": "pc", "cpu_pct": 1.0, "mem_pct": 2.0,
                "disk_pct": 3.0, "uptime_s": 4}
        status, _ = self.post(json.dumps(body).encode(), "/api/push/plain")
        self.assertEqual(status, 200)
        self.assertNotIn("health", app.load_agents()["plain"])


class HealthWarningTest(unittest.TestCase):
    """Curated failure-mode warnings, in the existing evaluate_warnings shape."""

    NOW = int(NOW)

    def codes(self, health, now=None):
        return {warning["code"]: warning
                for warning in app.health_warnings(health, now or self.NOW)}

    def test_healthy_smart_and_zfs_produce_no_warning(self):
        self.assertEqual(self.codes({
            "smart": [{"device": "/dev/sda", "health": "PASSED", "pending": 0}],
            "zfs": [{"pool": "tank", "state": "ONLINE", "errors": 0,
                     "scrub_age_s": 30 * 86400}],
        }), {})

    def test_smart_failure_or_pending_sectors_is_critical(self):
        failed = self.codes({"smart": [
            {"device": "/dev/sda", "health": "FAILED", "pending": 0}]})["smart"]
        self.assertEqual(failed["level"], "critical")
        self.assertIn("/dev/sda", failed["text"])

        pending = self.codes({"smart": [
            {"device": "/dev/sdb", "health": "PASSED", "pending": 2}]})["smart"]
        self.assertEqual(pending["level"], "critical")
        self.assertIn("2 pending", pending["text"])

    def test_zfs_state_and_errors_are_critical_and_an_old_scrub_warns(self):
        degraded = self.codes({"zfs": [
            {"pool": "tank", "state": "DEGRADED"}]})["zfs"]
        self.assertEqual(degraded["level"], "critical")

        errors = self.codes({"zfs": [
            {"pool": "tank", "state": "ONLINE", "errors": 3}]})["zfs"]
        self.assertEqual(errors["level"], "critical")
        self.assertIn("3 errors", errors["text"])

        scrub = self.codes({"zfs": [
            {"pool": "tank", "state": "ONLINE", "errors": 0,
             "scrub_age_s": 31 * 86400}]})["zfs_scrub"]
        self.assertEqual(scrub["level"], "warning")
        self.assertIn("31d", scrub["text"])

    def test_ups_on_battery_low_charge_or_short_runtime_is_critical(self):
        on_battery = self.codes({"ups": {"status": "OB DISCHRG",
                                        "charge_pct": 80}})["ups"]
        self.assertEqual(on_battery["level"], "critical")
        self.assertIn("battery", on_battery["text"])

        self.assertEqual(self.codes(
            {"ups": {"status": "OL", "charge_pct": 20}})["ups"]["level"],
            "critical")
        self.assertEqual(self.codes(
            {"ups": {"status": "OL", "runtime_s": 120}})["ups"]["level"],
            "critical")
        self.assertEqual(self.codes(
            {"ups": {"status": "OL", "battery_low": True}})["ups"]["level"],
            "critical")
        self.assertEqual(self.codes(
            {"ups": {"status": "OL", "charge_pct": 100,
                     "runtime_s": 1200}}), {})

    def test_a_stale_backup_warns_and_a_fresh_or_future_one_does_not(self):
        stale = self.codes({"backup": {
            "last_success_ts": self.NOW - 40 * 3600, "tool": "restic"}})["backup"]
        self.assertEqual(stale["level"], "warning")
        self.assertIn("40h", stale["text"])
        self.assertIn("restic", stale["text"])

        self.assertEqual(self.codes({"backup": {
            "last_success_ts": self.NOW - 30 * 3600}}), {})
        self.assertEqual(self.codes({"backup": {
            "last_success_ts": self.NOW + 3600}}), {})

    def test_pending_updates_are_info_and_a_required_reboot_warns(self):
        pending = self.codes({"updates": {"pending": 3, "security": 2}})
        self.assertEqual(pending["updates"]["level"], "info")
        self.assertIn("2 security", pending["updates"]["text"])

        reboot = self.codes({"updates": {"reboot_required": True}})
        self.assertEqual(reboot["reboot"]["level"], "warning")

        both = self.codes({"updates": {"pending": 1, "reboot_required": True}})
        self.assertEqual(set(both), {"updates", "reboot"})

    def test_evaluate_warnings_appends_health_warnings(self):
        warnings = app.evaluate_warnings({
            "online": True,
            "health": {"smart": [{"device": "/dev/sda", "health": "FAILED"}]},
        }, self.NOW)
        self.assertEqual([warning["code"] for warning in warnings], ["smart"])
        for key in ("code", "level", "value", "threshold", "text"):
            self.assertIn(key, warnings[0])

    def test_health_summary_is_compact_and_status_ignores_info(self):
        healthy = app._health_summary({
            "ts": self.NOW,
            "smart": [{"device": "/dev/sda", "health": "PASSED", "pending": 0}],
        }, self.NOW)
        self.assertEqual(healthy["status"], "ok")
        self.assertEqual(healthy["smart_failing"], 0)
        self.assertEqual(healthy["ts"], self.NOW)

        info = app._health_summary({"updates": {"pending": 4}}, self.NOW)
        self.assertEqual(info["status"], "ok")
        self.assertEqual(info["updates_pending"], 4)
        self.assertEqual(info["issues"], 0)

        critical = app._health_summary({
            "smart": [{"device": "/dev/sda", "health": "FAILED"}],
            "zfs": [{"pool": "tank", "state": "ONLINE", "errors": 1}],
            "ups": {"status": "OB", "charge_pct": 10},
            "backup": {"last_success_ts": self.NOW - 48 * 3600},
        }, self.NOW)
        self.assertEqual(critical["status"], "critical")
        self.assertEqual(critical["smart_failing"], 1)
        self.assertEqual(critical["zfs_unhealthy"], 1)
        self.assertTrue(critical["ups"]["on_battery"])
        self.assertEqual(critical["backup_age_s"], 48 * 3600)
        self.assertEqual(critical["issues"], 4)

        self.assertEqual(app._health_summary({"ts": None}, self.NOW)["status"],
                         "unknown")


class HealthApiTest(ApiTestCase):
    """The health blocks are additive: latest block, compact summary, chips."""

    BASE = int(NOW) - (int(NOW) % 3600)

    def device(self, health=None, device_id="agent:healthapi", **extra):
        record = {
            "id": device_id, "name": "healthapi", "kind": "pc",
            "online": True, "cpu_pct": 1.0, "mem_pct": 2.0,
            "mem_used": 1, "mem_total": 2, "disk_pct": 3.0,
            "uptime_s": 4, "last_seen": self.BASE,
        }
        if health is not None:
            record["health"] = health
        record.update(extra)
        return record

    def failing_health(self):
        return {
            "smart": [{"device": "/dev/sda", "health": "FAILED", "pending": 2}],
            "zfs": [{"pool": "tank", "state": "DEGRADED", "errors": 1,
                     "scrub_age_s": 40 * 86400}],
            "ups": {"status": "OB", "charge_pct": 10, "runtime_s": 120},
            "backup": {"last_success_ts": self.BASE - 48 * 3600,
                       "tool": "restic"},
            "updates": {"pending": 3, "security": 1, "reboot_required": True},
        }

    def test_metrics_series_carries_the_latest_health_and_a_short_history(self):
        app.record_samples([self.device(self.failing_health())], now=self.BASE)
        app.record_samples([self.device(self.failing_health())],
                           now=self.BASE + 60)

        payload = app.metrics_series("agent:healthapi", "1h", 60,
                                     now=self.BASE + 60)
        health = payload["health"]
        self.assertEqual(health["ts"], self.BASE + 60)
        self.assertEqual(health["smart"][0]["device"], "/dev/sda")
        self.assertEqual(health["zfs"][0]["pool"], "tank")
        self.assertEqual(payload["health_history"][-1]["ts"], self.BASE + 60)
        codes = {warning["code"] for warning in payload["warnings"]}
        for code in ("smart", "zfs", "zfs_scrub", "ups", "backup", "reboot",
                     "updates"):
            self.assertIn(code, codes)

    def test_an_old_device_has_an_empty_health_block_and_no_fabricated_rows(self):
        app.record_samples([self.device()], now=self.BASE)
        payload = app.metrics_series("agent:healthapi", "1h", 60,
                                     now=self.BASE + 60)
        self.assertEqual(payload["health"], {"ts": None})
        self.assertEqual(payload["health_history"], [])
        self.assertFalse(any(warning["code"] in app.HEALTH_KINDS
                             for warning in payload["warnings"]))

    def test_summary_carries_a_compact_health_status_and_alert_chip(self):
        app.record_samples([self.device(self.failing_health())], now=self.BASE)

        entry = app.metrics_summary("1h", now=self.BASE + 60)["devices"][0]
        health = entry["health"]
        self.assertEqual(health["status"], "critical")
        self.assertEqual(health["smart_failing"], 1)
        self.assertEqual(health["zfs_unhealthy"], 1)
        self.assertTrue(health["ups"]["on_battery"])
        self.assertEqual(health["updates_pending"], 3)
        self.assertTrue(health["reboot_required"])
        self.assertEqual(health["issues"], 6)
        self.assertEqual(entry["warning_count"], 6)
        self.assertEqual(entry["warning_level"], "critical")
        # The full blocks stay out of the fleet payload.
        self.assertNotIn("smart", health)

    def test_info_only_updates_do_not_turn_the_warning_chip_on(self):
        app.record_samples([self.device({"updates": {"pending": 4}})],
                           now=self.BASE)
        entry = app.metrics_summary("1h", now=self.BASE + 60)["devices"][0]
        self.assertEqual(entry["health"]["status"], "ok")
        self.assertEqual(entry["warning_count"], 0)
        self.assertIsNone(entry["warning_level"])
        self.assertEqual([w["code"] for w in entry["warnings"]], ["updates"])
        self.assertEqual(entry["warnings"][0]["level"], "info")

    def test_stats_sees_live_health_warnings_without_a_history_scan(self):
        self.post(json.dumps({
            "name": "livehealth", "kind": "pc", "cpu_pct": 1.0,
            "mem_pct": 2.0, "disk_pct": 3.0, "uptime_s": 4,
            "smart": [{"device": "/dev/sda", "health": "FAILED"}],
        }).encode(), "/api/push/livehealth")
        app._cache.update(at=0.0, body=None)

        device = next(d for d in app.stats()["devices"]
                      if d["id"] == "agent:livehealth")
        self.assertEqual(device["warning_level"], "critical")
        self.assertGreaterEqual(device["warning_count"], 1)
        self.assertEqual(device["health"]["smart"][0]["health"], "FAILED")

    def test_stats_keeps_info_only_updates_out_of_the_alert_count(self):
        self.post(json.dumps({
            "name": "liveinfo", "kind": "pc", "cpu_pct": 1.0,
            "mem_pct": 2.0, "disk_pct": 3.0, "uptime_s": 4,
            "updates": {"pending": 9},
        }).encode(), "/api/push/liveinfo")
        app._cache.update(at=0.0, body=None)

        device = next(d for d in app.stats()["devices"]
                      if d["id"] == "agent:liveinfo")
        self.assertEqual(device["warning_count"], 0)
        self.assertIsNone(device["warning_level"])


class MetricsHealthSourceTest(FrontEndSourceTest):
    """The selected device's failure-mode health, asserted at the source."""

    def test_metrics_page_has_a_health_section_for_the_selected_device(self):
        self.assertIn('id="health-section"', self.metrics_html)
        self.assertIn('id="health-body"', self.metrics_html)
        self.assertIn('id="health-sub"', self.metrics_html)
        self.assertIn('class="card-heading">Health<', self.metrics_html)
        self.assertIn("renderHealthSection()", function_body(self.metrics_js, "load"))

    def test_health_section_shows_smart_zfs_ups_backup_and_updates(self):
        body = function_body(self.metrics_js, "renderHealthSection")
        for block in ("health.smart", "health.zfs", "health.ups",
                      "health.backup", "health.updates"):
            self.assertIn(block, body, block)

        smart = function_body(self.metrics_js, "smartRow")
        for heading in ("Temperature", "Reallocated", "Pending", "Power-on"):
            self.assertIn(heading, smart + self.metrics_js, heading)
        self.assertIn("disk.health", smart)
        self.assertIn("disk.pending", smart)

        zfs = function_body(self.metrics_js, "zfsRow")
        for field in ("state", "capacity_pct", "scrub_age_s", "errors"):
            self.assertIn(field, zfs)

        ups = function_body(self.metrics_js, "upsTile")
        for field in ("status", "charge_pct", "runtime_s", "load_pct",
                      "battery_low"):
            self.assertIn(field, ups)

        self.assertIn("last_success_ts", self.metrics_js)
        self.assertIn("reboot_required", self.metrics_js)
        self.assertIn("updates.pending", self.metrics_js)

    def test_health_state_is_text_not_colour_alone(self):
        # The badge carries the state in words; the CSS only reinforces it.
        self.assertIn("health-badge", self.metrics_js)
        self.assertIn("disk.health", self.metrics_js)
        self.assertRegex(self.metrics_css, r"\.health-badge\s*\{")
        self.assertRegex(self.metrics_css, r"\.health-badge\.is-critical\s*\{")

    def test_device_rail_reuses_the_warning_chip_with_a_critical_state(self):
        body = function_body(self.metrics_js, "deviceItem")
        self.assertIn("warning_count", body)
        self.assertIn("warning_level", body)
        self.assertIn("device-item-warn", body)
        self.assertIn("is-critical", body)
        self.assertRegex(self.metrics_css, r"\.device-item-warn\.is-critical\s*\{")

    def test_health_section_hides_when_the_device_reports_no_health(self):
        body = function_body(self.metrics_js, "renderHealthSection")
        self.assertIn("healthSectionEl.hidden", body)
        self.assertIn("healthBodyEl.innerHTML = ''", body)

    def test_health_tiles_do_not_invent_missing_counts_or_states(self):
        updates = function_body(self.metrics_js, "updatesTile")
        self.assertIn("pending === null", updates)
        self.assertNotIn("? updates.pending : 0", updates)
        ups = function_body(self.metrics_js, "upsTile")
        self.assertIn("known", ups)
        self.assertIn("battery_low", ups)
        # The small history the API carries is used, not just fetched.
        self.assertIn("health_history",
                      function_body(self.metrics_js, "renderHealthSection"))
