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


def main() -> int:
    ARTIFACTS.mkdir(exist_ok=True)
    fight_id = int(
        django_shell(
            "from fightlog.models import Fight; print(max(Fight.objects.all(), key=lambda f: f.engagements.count()).pk)"
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
        page.screenshot(path=str(ARTIFACTS / "fight.png"), full_page=True)

        # Log toggles change the view and the URL
        boxes = page.locator(".log-toggle")
        first_id = boxes.first.get_attribute("value")
        boxes.first.uncheck()
        page.wait_for_function(f"!new URLSearchParams(location.search).get('logs').split(',').includes('{first_id}')")
        page.wait_for_function("document.getElementById('analysis-status').textContent.includes('4 logs')")
        check("W7 unticking a log updates the URL and the analysis", True, page.url.split("?", 1)[1][:60])
        boxes.first.check()
        page.wait_for_function("document.getElementById('analysis-status').textContent.includes('5 logs')")

        # Perspective
        page.select_option("#perspective", index=1)
        page.wait_for_function("new URLSearchParams(location.search).get('perspective').startsWith('side:')")
        check("W8 perspective switch", True, page.input_value("#perspective"))
        page.select_option("#perspective", index=0)

        # Pilot drill-down modal
        page.locator("a.fl-pilot").first.click()
        page.wait_for_selector("#drill-modal.show", timeout=30000)
        check("W9 pilot drill-down", "Weapons" in page.inner_text("#drill-body"), page.inner_text("#drill-title"))
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
