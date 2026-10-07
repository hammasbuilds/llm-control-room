"""Drive the real UI in a headless browser, exercise every page, and take the README screenshots.

    uv run llm-control-room --no-browser --port 8799 --db <tmp.sqlite3>        # in one shell
    uv run --with playwright python scripts/ui_tour.py http://127.0.0.1:8799 docs/screenshots

Fails (non-zero exit) if the page logs a console error, a request fails, or an expected element
never appears.
"""

from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE, OUT = sys.argv[1], Path(sys.argv[2])
OUT.mkdir(parents=True, exist_ok=True)
problems: list[str] = []


def main() -> None:
    with sync_playwright() as p:
        # prefer a full Chromium from the Playwright cache when the headless shell is missing
        found = sorted(
            Path.home().glob("AppData/Local/ms-playwright/chromium-*/chrome-win64/chrome.exe")
        )
        b = p.chromium.launch(executable_path=str(found[-1])) if found else p.chromium.launch()
        ctx = b.new_context(viewport={"width": 1360, "height": 900}, color_scheme="light")
        page = ctx.new_page()
        page.on(
            "console",
            lambda m: (
                problems.append(f"console {m.type}: {m.text}")
                if m.type == "error" and "status of 400" not in m.text
                else None
            ),
        )  # 400 = the injection refusal we provoke
        page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
        page.on("requestfailed", lambda r: problems.append(f"request failed: {r.url}"))
        page.on(
            "response",
            lambda r: problems.append(f"HTTP {r.status} {r.url}") if r.status >= 500 else None,
        )

        def go(name: str) -> None:
            page.goto(f"{BASE}/#/{name}")
            page.wait_for_selector("main .card, main .tiles", timeout=15000)
            page.wait_for_timeout(400)

        def shot(name: str, full: bool = True) -> None:
            page.screenshot(path=str(OUT / f"{name}.png"), full_page=full)

        # --- overview (seeded first-run traffic)
        go("overview")
        assert page.locator(".tile").count() >= 6, "overview tiles missing"
        shot("01-overview")

        # --- playground: PII + routing explanation, then an injection refusal
        go("playground")
        page.click("[data-p='3']")  # with PII
        page.click("#p-send")
        page.wait_for_selector("#p-out .answer", timeout=10000)
        assert "redacted:email" in page.inner_text("#p-out"), "PII redaction not shown"
        assert "routing decision" in page.inner_text("#p-out").lower()
        shot("02-playground")
        page.click("[data-p='5']")  # injection
        page.click("#p-send")
        page.wait_for_selector("#p-out >> text=Refused", timeout=10000)
        page.click("[data-p='6']")  # RAG with grounding
        page.select_option("#p-model", "titan-mock")
        page.click("#p-send")
        page.wait_for_selector("#p-out >> text=grounding", timeout=10000)
        page.click("#p-dry")
        page.wait_for_selector("#p-out >> text=Routing decision", timeout=10000)

        # --- router
        go("routing")
        assert "Estimate against the simulator" in page.inner_text("main")
        shot("03-router")

        # --- simulator: run scenarios from the UI
        go("simulator")
        for scenario in ("drift", "provider-outage", "canary-good", "ab-test"):
            page.fill("#sm-n", "1500" if scenario == "ab-test" else "600")
            page.click(f"[data-run='{scenario}']")
            page.wait_for_selector("#sm-out >> text=served", timeout=60000)
        page.fill("#sm-n", "600")
        page.click("[data-run='canary-bad']")
        page.wait_for_selector("#sm-out >> text=auto rollback", timeout=60000)
        shot("04-simulator")

        # --- observability: drift, alerts, fault injection
        go("observability")
        assert "Prompt drift" in page.inner_text("main")
        page.select_option("#fi-m", "nano-mock")
        page.click("#fi-set")
        page.wait_for_selector("text=nano-mock: 50% errors", timeout=10000)
        page.click("#fi-clear")
        page.wait_for_selector("text=Active: none", timeout=10000)
        page.goto(f"{BASE}/#/observability?hours=24")
        page.wait_for_selector(".card")
        page.wait_for_timeout(500)
        shot("05-observability")

        # --- releases: the bad canary, then the outage hold
        page.goto(f"{BASE}/#/simulator")
        page.wait_for_selector("[data-run='canary-outage']")
        page.click("[data-run='canary-outage']")
        page.wait_for_selector("#sm-out >> text=rollback held", timeout=60000)
        go("releases?r=support-bot")
        page.wait_for_selector("text=Upstream outage: rollback held", timeout=10000)
        shot("06-releases-outage-held")
        page.goto(f"{BASE}/#/simulator")
        page.click("[data-run='canary-bad']")
        page.wait_for_selector("#sm-out >> text=auto rollback", timeout=60000)
        go("releases?r=support-bot")
        page.wait_for_selector("text=Rolled back", timeout=10000)
        shot("07-releases-rollback")
        page.goto(f"{BASE}/#/releases?r=summariser")
        page.wait_for_selector("text=Version comparison", timeout=10000)
        shot("08-releases-ab")

        # --- agents: approval gate, then limits
        go("agents")
        page.click("[data-s='email']")
        page.click("#a-go")
        page.wait_for_selector("#ap-y", timeout=15000)
        shot("09-agent-approval")
        page.click("#ap-y")
        page.wait_for_selector("#r-status >> text=completed", timeout=15000)
        for scen, text in (
            ("loop", "LIMIT HIT: loop"),
            ("spendthrift", "LIMIT HIT: cost"),
            ("code-escape", "PermissionError"),
        ):
            page.click(f"[data-s='{scen}']")
            page.click("#a-go")
            page.wait_for_selector(f"#r-log >> text={text}", timeout=20000)
            page.wait_for_timeout(1200)
        shot("10-agent-runs")

        # --- sandbox
        go("sandbox")
        page.click("#sb-go")
        page.wait_for_selector("#sb-out >> text=exit 0", timeout=15000)
        page.click("#pb-go")
        page.wait_for_selector("#pb-out table", timeout=90000)
        assert "got through" in page.inner_text("#pb-out")
        shot("11-sandbox")

        # --- tenants
        go("tenants")
        page.fill("#nt-name", "tour-co")
        page.click("#nt-go")
        page.wait_for_selector("#nt-out >> text=Key (shown only once)", timeout=10000)
        shot("12-tenants")

        # --- dark mode and a phone-sized screen
        ctx2 = b.new_context(viewport={"width": 1360, "height": 900}, color_scheme="dark")
        d = ctx2.new_page()
        d.goto(f"{BASE}/#/overview")
        d.wait_for_selector(".tile")
        d.wait_for_timeout(400)
        d.screenshot(path=str(OUT / "13-overview-dark.png"), full_page=True)
        ctx3 = b.new_context(viewport={"width": 390, "height": 844})
        m = ctx3.new_page()
        m.goto(f"{BASE}/#/overview")
        m.wait_for_selector(".tile")
        m.wait_for_timeout(400)
        overflow = m.evaluate(
            "document.documentElement.scrollWidth > document.documentElement.clientWidth + 2"
        )
        m.screenshot(path=str(OUT / "14-overview-phone.png"), full_page=False)
        if overflow:
            problems.append("phone layout scrolls horizontally")
        b.close()


try:
    main()
except Exception:  # report, then fail
    import traceback

    problems.append("tour aborted: " + traceback.format_exc(limit=-2))

if problems:
    print("PROBLEMS:")
    for line in problems:
        print(" -", line)
    sys.exit(1)
print("UI tour passed; screenshots in", OUT)
