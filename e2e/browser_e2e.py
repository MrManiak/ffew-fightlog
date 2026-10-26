"""Browser end-to-end tests (Playwright, headless Chromium) against the dev stack.

    python e2e/browser_e2e.py [Gamelogs folder]

Run after e2e/run_e2e.py so the dev database holds the real 2026-10-25 fight.
"""

import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from client import BASE, django_shell, session_key  # noqa: E402

GAMELOGS = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / "Documents" / "EVE" / "logs" / "Gamelogs"
ARTIFACTS = Path(__file__).resolve().parent / "artifacts"
results: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""), flush=True)


def close_modal(page):
    # Bootstrap ignores hide() while the fade-in transition is still running.
    page.wait_for_timeout(600)
    page.keyboard.press("Escape")
    page.wait_for_selector("#drill-modal", state="hidden", timeout=10000)
    # ...and removes the backdrop only after its own fade-out.
    page.wait_for_selector(".modal-backdrop", state="detached", timeout=10000)


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


def main() -> int:
    ARTIFACTS.mkdir(exist_ok=True)
    # The real 2026-10-25 fight from run_e2e.py, not perf_e2e.py's synthetic one.
    fight_id = int(
        django_shell(
            "from fightlog.models import Fight; "
            "real = Fight.objects.exclude(participants__key__startswith='p:Synthpilot '); "
            "print(max(real, key=lambda f: f.engagements.count()).pk)"
        )
    )
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(viewport={"width": 1400, "height": 1000})
        context.add_cookies([{"name": "sessionid", "value": session_key("admin"), "url": BASE}])
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
        page.on("console", lambda msg: errors.append(f"console: {msg.text}") if msg.type == "error" else None)

        # Fight list
        page.goto(f"{BASE}/fightlog/")
        rows = page.locator("#fight-table tbody tr").count()
        check("W1 fight list shows fights", rows > 0, f"{rows} rows")
        page.fill("#f-who", "IMBCN")
        page.click("#fight-search button[type=submit]")
        page.wait_for_load_state("networkidle")
        check("W2 search by corp ticker", page.locator("#fight-table tbody tr").count() >= 1)

        # Fight page
        page.goto(f"{BASE}/fightlog/fight/{fight_id}/")
        page.wait_for_function(
            "document.getElementById('analysis-status').textContent.includes('pilots')", timeout=60000
        )
        check("W3 analysis loads", True, page.inner_text("#analysis-status"))
        has_chart = page.evaluate("() => !!(window.Chart && Chart.getChart(document.getElementById('timeline')))")
        datasets = page.evaluate("() => Chart.getChart(document.getElementById('timeline')).data.datasets.length")
        check("W4 timeline chart drawn", has_chart and datasets >= 3, f"{datasets} series")
        tables = page.locator("table.fl-pilots").count()
        pilot_rows = page.locator("table.fl-pilots tbody tr").count()
        check("W5 per-side tables", tables >= 2 and pilot_rows > 50, f"{tables} tables, {pilot_rows} rows")
        check("W6 targets table", page.locator("#targets-table tbody tr").count() > 0)
        totals = page.evaluate(TOTALS_JS)
        check(
            "W19 the totals strip matches the chart",
            totals["tiles"] >= 6
            and totals["title"].endswith(totals["expected"])
            and "whole fight" in totals["caption"],
            f"{totals['tiles']} tiles; {totals['title']}; {totals['caption']}",
        )
        page.screenshot(path=str(ARTIFACTS / "fight.png"), full_page=True)

        # Log toggles change the view and the URL
        boxes = page.locator(".log-toggle")
        n_logs = boxes.count()
        first_id = boxes.first.get_attribute("value")
        boxes.first.uncheck()
        page.wait_for_function(f"!new URLSearchParams(location.search).get('logs').split(',').includes('{first_id}')")
        page.wait_for_function(f"document.getElementById('analysis-status').textContent.includes('{n_logs - 1} logs')")
        check("W7 unticking a log updates the URL and the analysis", True, page.url.split("?", 1)[1][:60])
        boxes.first.check()
        page.wait_for_function(f"document.getElementById('analysis-status').textContent.includes('{n_logs} logs')")

        # Perspective
        page.select_option("#perspective", index=1)
        page.wait_for_function("new URLSearchParams(location.search).get('perspective').startsWith('side:')")
        check("W8 perspective switch", True, page.input_value("#perspective"))
        page.select_option("#perspective", index=0)

        # Pilot drill-down modal: its weapons add up to the pilot's "Dmg out" in the table
        first_row = page.locator("table.fl-pilots tbody tr").first
        table_damage = first_row.locator("td").nth(3).inner_text()
        first_row.locator("a.fl-pilot").click()
        page.wait_for_selector("#drill-modal.show", timeout=30000)
        page.wait_for_selector("#weapons-table tfoot", timeout=30000)
        popup_damage = page.locator("#weapons-table tfoot td").nth(1).inner_text()
        check(
            "W9 pilot drill-down adds up to the table",
            popup_damage == table_damage,
            f"{page.inner_text('#drill-title')}: table {table_damage}, pop-up {popup_damage}",
        )
        close_modal(page)

        # Clicking the chart opens the events in that bucket
        box = page.locator("#timeline").bounding_box()
        busiest_x = page.evaluate(
            "() => { const c = Chart.getChart(document.getElementById('timeline'));"
            " const sums = c.data.labels.map((_, i) => c.data.datasets.reduce((s, d) => s + (d.data[i] || 0), 0));"
            " return c.scales.x.getPixelForValue(sums.indexOf(Math.max(...sums))); }"
        )
        page.mouse.click(box["x"] + busiest_x, box["y"] + box["height"] * 0.5)
        page.wait_for_function("document.getElementById('drill-title').textContent.startsWith('Events')", timeout=30000)
        check(
            "W10 chart click shows events",
            page.locator("#events-table tbody tr").count() > 0,
            page.inner_text("#drill-title"),
        )
        close_modal(page)

        # Deaths: a marker per killmail in the chart's time range
        deaths = page.evaluate(DEATHS_JS)
        check(
            "W21 a death marker per killmail, no ship names in the whole-fight view",
            deaths["markers"] == deaths["expected"] > 10
            and deaths["labelled"] == 0
            and page.is_visible("#death-legend"),
            f"{deaths['markers']} markers for {deaths['expected']} killmails in range; {page.inner_text('#death-legend')[:40]}",
        )
        box = page.locator("#timeline").bounding_box()
        spot = deaths["hits"][len(deaths["hits"]) // 2]
        page.mouse.move(box["x"] + spot["x"], box["y"] + spot["y"])
        page.wait_for_selector(".fightlog-death-tip", state="visible", timeout=10000)
        tip = page.inner_text(".fightlog-death-tip")
        hovered = page.evaluate(DEATHS_JS)["hovered"]
        tooltip_quiet = page.evaluate(
            "() => { const t = window.fightlog.chart().tooltip; return t.opacity === 0 || (!t.title.length && !t.body.length); }"
        )
        check(
            "W22 hovering a death shows who lost what",
            " lost " in tip and "zKillboard" in tip and hovered is not None and tooltip_quiet,
            tip.replace("\n", " | "),
        )
        page.evaluate(
            "() => { window.__opened = []; window.open = (url) => { window.__opened.push(url); return null; }; }"
        )
        page.mouse.click(box["x"] + spot["x"], box["y"] + spot["y"])
        page.wait_for_function("() => window.__opened.length > 0", timeout=10000)
        opened = page.evaluate("() => window.__opened[0]")
        page.wait_for_timeout(600)
        check(
            "W23 clicking a death opens its killmail on zKillboard",
            opened.startswith("https://zkillboard.com/kill/") and page.locator("#drill-modal.show").count() == 0,
            opened,
        )
        page.mouse.move(box["x"] + 5, box["y"] + 5)

        # Dragging across the chart zooms the whole page into that time window
        js_sum = "(s) => s.reduce((a, b) => a + b, 0)"
        full = page.evaluate(
            f"() => {{ const r = window.fightlog.lastResult(); return {{labels: r.labels.length, damage: ({js_sum})(r.series.damage_out)}}; }}"
        )
        area = page.evaluate(
            "() => { const a = window.fightlog.chart().chartArea; return {left: a.left, right: a.right, top: a.top, bottom: a.bottom}; }"
        )
        box = page.locator("#timeline").bounding_box()
        y = box["y"] + (area["top"] + area["bottom"]) / 2
        x0 = box["x"] + area["left"] + (area["right"] - area["left"]) * 0.55
        x1 = box["x"] + area["left"] + (area["right"] - area["left"]) * 0.8
        page.mouse.move(x0, y)
        page.mouse.down()
        page.mouse.move(x1, y, steps=8)
        band = page.locator(".fightlog-zoom-box")
        band_label = band.inner_text() if band.is_visible() else ""
        band_box = band.bounding_box() or {"width": 0}
        # Chart.js draws nothing when the tooltip has no title or body left after filtering.
        tooltip_hidden = page.evaluate(
            "() => { const t = window.fightlog.chart().tooltip; return t.opacity === 0 || (!t.title.length && !t.body.length); }"
        )
        page.mouse.up()
        check(
            "W15 the selection is drawn while dragging",
            "–" in band_label and band_box["width"] > 100 and tooltip_hidden,
            f"band {band_box['width']:.0f}px wide, label '{band_label}', tooltip hidden: {tooltip_hidden}",
        )
        page.wait_for_function("new URLSearchParams(location.search).has('from')")
        page.wait_for_function("document.getElementById('analysis-status').textContent.includes('pilots')")
        zoomed = page.evaluate(
            f"() => {{ const r = window.fightlog.lastResult(); return {{window: r.window, labels: r.labels.length, damage: ({js_sum})(r.series.damage_out)}}; }}"
        )
        page.wait_for_timeout(600)
        check(
            "W16 dragging across the chart zooms the page into that window",
            zoomed["window"]
            and zoomed["labels"] < full["labels"]
            and 0 < zoomed["damage"] < full["damage"]
            and page.is_visible("#zoom-bar")
            and page.locator("#drill-modal.show").count() == 0,
            f"{zoomed['labels']} of {full['labels']} buckets, damage {zoomed['damage']:,} of {full['damage']:,}; "
            + page.inner_text("#zoom-text")[:30],
        )
        totals = page.evaluate(TOTALS_JS)
        check(
            "W20 the totals strip follows the window",
            totals["title"].endswith(totals["expected"]) and "whole fight" not in totals["caption"],
            f"{totals['title']}; {totals['caption']}",
        )
        deaths = page.evaluate(DEATHS_JS)
        check(
            "W24 zoomed in, the deaths in the window get ship names",
            0 < deaths["markers"] == deaths["expected"] and deaths["labelled"] > 0,
            f"{deaths['markers']} markers, {deaths['labelled']} named",
        )
        first_row = page.locator("table.fl-pilots tbody tr").first
        table_damage = first_row.locator("td").nth(3).inner_text()
        first_row.locator("a.fl-pilot").click()
        page.wait_for_selector("#weapons-table tfoot", timeout=30000)
        popup_damage = page.locator("#weapons-table tfoot td").nth(1).inner_text()
        check(
            "W17 the pilot drill-down follows the window",
            popup_damage == table_damage,
            f"{table_damage} / {popup_damage}",
        )
        close_modal(page)
        page.click("#zoom-reset")
        page.wait_for_function("!new URLSearchParams(location.search).has('from')")
        page.wait_for_function(
            f"() => window.fightlog.lastResult() && window.fightlog.lastResult().labels.length === {full['labels']}"
        )
        check("W18 'Show the whole fight' resets the zoom", page.is_hidden("#zoom-bar"))

        # Upload page: the browser pre-filter
        page.goto(f"{BASE}/fightlog/upload/")
        candidates = sorted(GAMELOGS.glob("2026102[456]_*.txt"))
        picked = [p for p in candidates if p.stat().st_size < 3_000_000][:8]
        old = sorted(GAMELOGS.glob("2026100*_*_*.txt"))[:2]
        page.set_input_files("#file-input", [str(p) for p in picked + old])
        total = len(picked) + len(old)
        page.wait_for_function(
            f"() => {{ const c = window.fightlogUpload.counts; return c.seen === {total} && c.sent + c.skipped === {total}; }}",
            timeout=120000,
        )
        final = ("Parsed", "No PvP found", "Failed", "Rejected", "Skipped")
        deadline = time.time() + 300
        while True:
            statuses = [r.inner_text() for r in page.locator("#upload-table tbody tr td:nth-child(3)").all()]
            building = "building…" in page.inner_text("#upload-table")
            if (all(s.startswith(final) for s in statuses) and not building) or time.time() > deadline:
                break
            time.sleep(2)
        summary = page.inner_text("#upload-summary")
        check(
            "W11 upload pre-filter skips empty and out-of-range files", "skipped" in summary and len(old) > 0, summary
        )
        check(
            "W12 uploaded files reach a final status",
            statuses and all(s.startswith(final) for s in statuses),
            "; ".join(sorted(set(s.split(":")[0] for s in statuses))),
        )
        page.screenshot(path=str(ARTIFACTS / "upload.png"), full_page=True)

        for path, needle in (
            ("/fightlog/my-logs/", "My logs"),
            ("/fightlog/help/", "overview ship label"),
            ("/fightlog/friendlies/", "Allies"),
        ):
            page.goto(BASE + path)
            check(f"W13 {path} renders", needle in page.content())

        check("W14 no JavaScript errors", not errors, "; ".join(errors[:5]))
        browser.close()
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} browser checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
