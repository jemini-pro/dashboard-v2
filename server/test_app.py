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

        self.assertIn("No usage yet", render)
        self.assertIn("hidden = false", render)
        self.assertNotIn("hidden = true", render)

    def test_only_the_scored_api_payload_populates_the_card(self):
        load = function_body(self.js, "loadBookmarks")

        self.assertIn("renderMostUsed(data.work)", load)

        # The scoreless embedded copy paints the grids for first paint; it must
        # never populate the usage card.
        self.assertNotIn("renderMostUsed", function_body(self.js, "renderBookmarks"))
        self.assertNotIn("renderMostUsed", function_body(self.js, "updateDashboard"))

        embedded = self.js[self.js.index("const bookmarks = {"):self.js.index("const clockEl")]
        self.assertNotIn("score", embedded)

    def test_category_order_and_contents_are_never_sorted_by_usage(self):
        body = function_body(self.js, "renderBookmarks")

        self.assertNotIn(".sort(", body)
        self.assertRegex(
            body,
            r"categories\.map\(\s*cat\s*=>\s*categorySection\(cat\.category,\s*cat\.items\)",
        )

    def test_click_and_auxclick_send_a_beacon_delegated_from_the_card(self):
        hit = function_body(self.js, "recordBookmarkHit")

        self.assertIn("mostUsedEl.addEventListener('click', recordBookmarkHit)", self.source)
        self.assertIn("mostUsedEl.addEventListener('auxclick', recordBookmarkHit)", self.source)
        self.assertIn(".usage-row", hit)
        self.assertIn("closest(", hit)
        self.assertIn("event.type === 'auxclick' && event.button !== 1", hit)
        self.assertIn("navigator.sendBeacon('/api/hit', JSON.stringify({ url }))", hit)

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
        network = self.html.index('class="network-panel"')

        self.assertLess(rail, network)
        self.assertLess(network, self.html.index("</aside>"))

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

    def test_bookmarks_use_the_monogram_helper_not_emoji(self):
        card = function_body(self.js, "bookmarkCard")
        self.assertIn("monogram(item)", card)
        self.assertNotIn("item.icon", card)

        embedded = self.js[
            self.js.index("const bookmarks = {"):self.js.index("const clockEl")
        ]
        self.assertNotRegex(embedded, r"\bicon\s*:")
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

    def test_result_count_lives_in_the_all_tools_card_head(self):
        card = self.html[self.html.index('class="card all-tools-card"'):]
        self.assertIn('id="result-count"', card[:card.index("</section>")])
        self.assertIn('id="bookmarks-grid"', card[:card.index("</section>")])

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
        handler = function_body(self.js, "recordToolHit")

        self.assertIn("event.target.closest('.tool-link')", handler)
        self.assertIn("event.button !== 1", handler)
        self.assertIn("getAttribute('href')", handler)
        self.assertIn(
            "navigator.sendBeacon('/api/hit', JSON.stringify({ url }))", handler
        )
        self.assertIn(
            "bookmarksGridEl.addEventListener('click', recordToolHit)", self.js
        )
        self.assertIn(
            "bookmarksGridEl.addEventListener('auxclick', recordToolHit)", self.js
        )
        self.assertNotIn("fetch('/api/hit'", handler)
