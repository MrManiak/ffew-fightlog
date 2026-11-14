"""Browser tests: the fight page's JavaScript on the reference fight, in headless Chromium against a live server.

Skipped when Playwright isn't installed. To run them:

    pip install playwright && playwright install chromium
    python runtests.py fightlog.tests.test_browser

or `tox -e browser`, as CI does.

The pages load Bootstrap, Chart.js and DataTables from their CDNs, as Alliance Auth's bundles do.
"""

import os
from unittest import mock, skipUnless

from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import TestCase
from django.urls import reverse

from fightlog.services import zkb
from fightlog.services.rebuild import rebuild_fight

from .test_reference_fight import main_fight
from .utils import ALL_PERMS, FIGHT_DAY_AFTER, FIVE_LOGS, fleet_owner, ingest_fixture, offline

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None

READY_JS = "document.getElementById('analysis-status').textContent.includes('pilots')"

TOTALS_JS = """() => {
    const r = window.fightlog.lastResult();
    const tile = document.querySelector('#totals [data-total="Damage"] [title]');
    return {
        tiles: document.querySelectorAll('#totals [data-total]').length,
        caption: document.querySelector('#totals .w-100').textContent,
        title: tile ? tile.title : '',
        expected: r.series.damage_out.reduce((a, b) => a + b, 0).toLocaleString(),
    };
}"""

DEATHS_JS = """() => {
    const r = window.fightlog.lastResult();
    const d = window.fightlog.deaths();
    const last = r.labels[r.labels.length - 1] + r.bucket - 1;
    return {
        markers: d.hits.length,
        expected: r.kills.filter((k) => k.time >= r.labels[0] && k.time <= last).length,
        labelled: d.labelled,
        hovered: d.hovered,
        hits: d.hits,
    };
}"""

RESULT_JS = """() => {
    const r = window.fightlog.lastResult();
    return {window: r.window, labels: r.labels.length, damage: r.series.damage_out.reduce((a, b) => a + b, 0)};
}"""

# Chart.js draws nothing when the tooltip has no title or body left after filtering.
TOOLTIP_QUIET_JS = """() => {
    const t = window.fightlog.chart().tooltip;
    return t.opacity === 0 || (!t.title.length && !t.body.length);
}"""

SERIES_OFF_JS = """(label) => {
    const c = window.fightlog.chart();
    const i = c.data.datasets.findIndex((d) => d.label === label);
    return i >= 0 && !c.isDatasetVisible(i);
}"""


# TestCase first: the fight is built once per class, and SQLite's in-memory test database shares its
# connection with the live server's threads, so they see it inside the class's transaction.
@skipUnless(sync_playwright, "Playwright is not installed")
class BrowserTestCase(TestCase, StaticLiveServerTestCase):
    @classmethod
    def setUpClass(cls):
        # Playwright's sync API keeps an event loop running in this thread, which Django takes for async code.
        env = mock.patch.dict(os.environ, {"DJANGO_ALLOW_ASYNC_UNSAFE": "true"})
        env.start()
        cls.addClassCleanup(env.stop)
        cls.playwright = sync_playwright().start()
        cls.addClassCleanup(cls.playwright.stop)
        cls.browser = cls.playwright.chromium.launch()
        cls.addClassCleanup(cls.browser.close)
        super().setUpClass()

    @classmethod
    def setUpTestData(cls):
        cls.user = fleet_owner(permissions=ALL_PERMS)
        with offline(zkb=True):
            ingest_fixture(cls.user, FIVE_LOGS)
            fight = main_fight()
            zkb.link_fight(fight, now=FIGHT_DAY_AFTER)
            fight.data_version += 1
            fight.save()
            rebuild_fight(fight.pk)
        cls.fight_url = reverse("fightlog:fight", args=[fight.pk])

    def setUp(self):
        self.client.force_login(self.user)
        context = self.browser.new_context(viewport={"width": 1400, "height": 1000})
        self.addCleanup(context.close)
        context.add_cookies(
            [
                {
                    "name": settings.SESSION_COOKIE_NAME,
                    "value": self.client.cookies[settings.SESSION_COOKIE_NAME].value,
                    "url": self.live_server_url,
                }
            ]
        )
        self.page = context.new_page()
        self.page.set_default_timeout(30000)
        self.js_errors = []
        self.page.on("pageerror", lambda exc: self.js_errors.append(f"pageerror: {exc}"))
        self.page.on(
            "console", lambda msg: self.js_errors.append(f"console: {msg.text}") if msg.type == "error" else None
        )

    def tearDown(self):
        self.assertEqual(self.js_errors, [])

    def open_fight(self):
        self.page.goto(self.live_server_url + self.fight_url)
        self.page.wait_for_function(READY_JS)

    def close_modal(self):
        # Bootstrap ignores hide() while the fade-in transition is still running.
        self.page.wait_for_timeout(600)
        self.page.keyboard.press("Escape")
        self.page.wait_for_selector("#drill-modal", state="hidden")
        # ...and removes the backdrop only after its own fade-out.
        self.page.wait_for_selector(".modal-backdrop", state="detached")

    def chart_origin(self):
        box = self.page.locator("#timeline").bounding_box()
        return box["x"], box["y"]

    def series_off(self, label: str) -> bool:
        return self.page.evaluate(SERIES_OFF_JS, label)

    def switch_series(self, label: str, on: bool):
        """Click the series in the chart's legend and wait for it to switch."""
        self.assertEqual(self.series_off(label), on)
        spot = self.page.evaluate(
            "(label) => { const c = window.fightlog.chart();"
            " const b = c.legend.legendHitBoxes[c.data.datasets.findIndex((d) => d.label === label)];"
            " return {x: b.left + b.width / 2, y: b.top + b.height / 2}; }",
            label,
        )
        # The chart moves down the page when the zoom bar appears: where it is now.
        left, top = self.chart_origin()
        self.page.mouse.click(left + spot["x"], top + spot["y"])
        # Chart.js handles the click a frame later.
        self.page.wait_for_function(f"(label) => ({SERIES_OFF_JS})(label) === {str(not on).lower()}", arg=label)

    def drag_across_chart(self, start: float, end: float, release: bool = True):
        """Drag between two fractions of the plot's width, at mid-height."""
        area = self.page.evaluate(
            "() => { const a = window.fightlog.chart().chartArea;"
            " return {left: a.left, right: a.right, top: a.top, bottom: a.bottom}; }"
        )
        left, top = self.chart_origin()
        y = top + (area["top"] + area["bottom"]) / 2
        x = [left + area["left"] + (area["right"] - area["left"]) * f for f in (start, end)]
        self.page.mouse.move(x[0], y)
        self.page.mouse.down()
        self.page.mouse.move(x[1], y, steps=8)
        if release:
            self.release_drag()

    def release_drag(self):
        self.page.mouse.up()
        self.page.wait_for_function("new URLSearchParams(location.search).has('from')")
        self.page.wait_for_function("window.fightlog.lastResult().window !== null")
        self.page.wait_for_function(READY_JS)


class FightPageTest(BrowserTestCase):
    def test_page_shows_chart_tables_and_totals(self):
        self.open_fight()
        page = self.page
        datasets = page.evaluate("() => window.fightlog.chart().data.datasets.length")
        self.assertGreaterEqual(datasets, 3)
        self.assertGreaterEqual(page.locator("table.fl-pilots").count(), 2)
        self.assertGreater(page.locator("table.fl-pilots tbody tr").count(), 50)
        self.assertGreater(page.locator("#targets-table tbody tr").count(), 0)
        totals = page.evaluate(TOTALS_JS)
        self.assertGreaterEqual(totals["tiles"], 6)
        self.assertTrue(totals["title"].endswith(totals["expected"]), totals)
        self.assertIn("whole fight", totals["caption"])

    def test_unticking_a_log_updates_the_url_and_the_analysis(self):
        self.open_fight()
        page = self.page
        boxes = page.locator(".log-toggle")
        n_logs = boxes.count()
        first_id = boxes.first.get_attribute("value")
        boxes.first.uncheck()
        page.wait_for_function(f"!new URLSearchParams(location.search).get('logs').split(',').includes('{first_id}')")
        page.wait_for_function(f"document.getElementById('analysis-status').textContent.includes('{n_logs - 1} logs')")
        boxes.first.check()
        page.wait_for_function(f"document.getElementById('analysis-status').textContent.includes('{n_logs} logs')")

    def test_perspective_switch(self):
        self.open_fight()
        page = self.page
        before = page.evaluate(RESULT_JS)["damage"]
        page.select_option("#perspective", index=1)
        page.wait_for_function(f"() => ({RESULT_JS})().damage !== {before}")
        self.assertIn(f"perspective={page.input_value('#perspective')}".replace(":", "%3A"), page.url)

    def test_pilot_drill_down_adds_up_to_the_table(self):
        self.open_fight()
        page = self.page
        first_row = page.locator("table.fl-pilots tbody tr").first
        table_damage = first_row.locator("td").nth(3).inner_text()
        first_row.locator("a.fl-pilot").click()
        page.wait_for_selector("#drill-modal.show")
        page.wait_for_selector("#weapons-table tfoot")
        self.assertEqual(page.locator("#weapons-table tfoot td").nth(1).inner_text(), table_damage)

    def test_chart_click_shows_the_events_in_that_bucket(self):
        self.open_fight()
        page = self.page
        busiest_x = page.evaluate(
            "() => { const c = window.fightlog.chart();"
            " const sums = c.data.labels.map((_, i) => c.data.datasets.reduce((s, d) => s + (d.data[i] || 0), 0));"
            " return c.scales.x.getPixelForValue(sums.indexOf(Math.max(...sums))); }"
        )
        box = page.locator("#timeline").bounding_box()
        page.mouse.click(box["x"] + busiest_x, box["y"] + box["height"] * 0.5)
        page.wait_for_function("document.getElementById('drill-title').textContent.startsWith('Events')")
        self.assertGreater(page.locator("#events-table tbody tr").count(), 0)

    def test_death_markers(self):
        self.open_fight()
        page = self.page
        deaths = page.evaluate(DEATHS_JS)
        self.assertGreater(deaths["expected"], 10)
        self.assertEqual(deaths["markers"], deaths["expected"])
        # No ship names in the whole-fight view.
        self.assertEqual(deaths["labelled"], 0)
        self.assertTrue(page.is_visible("#death-legend"))

        # Hovering one shows who lost what, instead of the series tooltip.
        left, top = self.chart_origin()
        spot = deaths["hits"][len(deaths["hits"]) // 2]
        page.mouse.move(left + spot["x"], top + spot["y"])
        page.wait_for_selector(".fightlog-death-tip", state="visible")
        tip = page.inner_text(".fightlog-death-tip")
        self.assertIn(" lost ", tip)
        self.assertIn("zKillboard", tip)
        self.assertIsNotNone(page.evaluate(DEATHS_JS)["hovered"])
        page.wait_for_function(TOOLTIP_QUIET_JS)

        # Clicking it opens the killmail, not the bucket's events.
        page.evaluate(
            "() => { window.__opened = []; window.open = (url) => { window.__opened.push(url); return null; }; }"
        )
        page.mouse.click(left + spot["x"], top + spot["y"])
        page.wait_for_function("() => window.__opened.length > 0")
        self.assertTrue(page.evaluate("() => window.__opened[0]").startswith("https://zkillboard.com/kill/"))
        page.wait_for_timeout(600)
        self.assertEqual(page.locator("#drill-modal.show").count(), 0)

    def test_dragging_across_the_chart_zooms_the_page(self):
        self.open_fight()
        page = self.page
        full = page.evaluate(RESULT_JS)

        # The selection is drawn while dragging, without the series tooltip over it.
        self.drag_across_chart(0.55, 0.8, release=False)
        band = page.locator(".fightlog-zoom-box")
        self.assertTrue(band.is_visible())
        self.assertIn("–", band.inner_text())
        self.assertGreater(band.bounding_box()["width"], 100)
        page.wait_for_function(TOOLTIP_QUIET_JS)
        self.release_drag()

        zoomed = page.evaluate(RESULT_JS)
        self.assertTrue(zoomed["window"])
        self.assertLess(zoomed["labels"], full["labels"])
        self.assertTrue(0 < zoomed["damage"] < full["damage"], (zoomed, full))
        self.assertTrue(page.is_visible("#zoom-bar"))
        # The click that ends the drag doesn't open the bucket's events.
        page.wait_for_timeout(600)
        self.assertEqual(page.locator("#drill-modal.show").count(), 0)

        # The totals, the death markers and the pilot drill-down follow the window.
        totals = page.evaluate(TOTALS_JS)
        self.assertTrue(totals["title"].endswith(totals["expected"]), totals)
        self.assertNotIn("whole fight", totals["caption"])
        deaths = page.evaluate(DEATHS_JS)
        self.assertGreater(deaths["markers"], 0)
        self.assertEqual(deaths["markers"], deaths["expected"])
        self.assertGreater(deaths["labelled"], 0)
        first_row = page.locator("table.fl-pilots tbody tr").first
        table_damage = first_row.locator("td").nth(3).inner_text()
        first_row.locator("a.fl-pilot").click()
        page.wait_for_selector("#weapons-table tfoot")
        self.assertEqual(page.locator("#weapons-table tfoot td").nth(1).inner_text(), table_damage)
        self.close_modal()

        page.click("#zoom-reset")
        page.wait_for_function("!new URLSearchParams(location.search).has('from')")
        page.wait_for_function(f"() => window.fightlog.lastResult().labels.length === {full['labels']}")
        self.assertTrue(page.is_hidden("#zoom-bar"))

    def test_a_series_switched_off_in_the_legend_stays_off_through_a_zoom(self):
        self.open_fight()
        page = self.page
        label = page.evaluate("() => window.fightlog.chart().data.datasets[0].label")
        self.switch_series(label, on=False)

        self.drag_across_chart(0.55, 0.8)
        self.assertTrue(self.series_off(label))

        # Switched back on, it stays on through the next redraw.
        self.switch_series(label, on=True)
        page.click("#zoom-reset")
        page.wait_for_function("window.fightlog.lastResult().window === null")
        page.wait_for_function(READY_JS)
        self.assertFalse(self.series_off(label))
