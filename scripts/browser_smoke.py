"""Real-browser HTMX smoke check against a seeded development server.

Uses installed Chrome by default; set BROWSER_CHANNEL=chromium after
`uv run playwright install chromium` on a host without Chrome.
"""

import argparse
import os
from pathlib import Path

from playwright.sync_api import sync_playwright


def run(base_url):
    if os.getenv("APP_ENV", "development") != "development":
        raise SystemExit("Browser demo is restricted to development accounts.")
    output = Path(".runtime")
    output.mkdir(exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            channel=os.getenv("BROWSER_CHANNEL", "chrome"), headless=True
        )
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        page = context.new_page()
        errors, remote_assets = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on(
            "request",
            lambda request: (
                remote_assets.append(request.url) if not request.url.startswith(base_url) else None
            ),
        )
        page.goto(base_url + "/", wait_until="networkidle")
        page.screenshot(path=str(output / "landing-desktop.png"), full_page=True)
        page.goto(base_url + "/login")
        page.get_by_label("Email address").fill("a@slotrescue.test")
        page.get_by_label("Password", exact=True).fill(
            os.getenv("DEMO_PASSWORD", "DemoPassphrase!2026")
        )
        page.get_by_role("button", name="Sign in", exact=True).click()
        page.wait_for_url(base_url + "/dashboard")
        page.goto(base_url + "/slots")
        page.get_by_role("button", name="Find appointments", exact=False).click()
        page.wait_for_load_state("networkidle")
        if page.get_by_role("button", name="Book this time", exact=False).count():
            page.get_by_role("button", name="Book this time", exact=False).first.click()
            page.wait_for_url(base_url + "/bookings/*")
            page.get_by_text("Cancel this appointment", exact=True).click()
            page.get_by_role("button", name="Confirm cancellation", exact=True).click()
            page.wait_for_url(base_url + "/dashboard")
        else:
            raise AssertionError(
                "Seed at least one available appointment for the browser booking check"
            )
        page.get_by_role("button", name="Sign out", exact=True).click()
        page.wait_for_url(base_url + "/")
        page.goto(base_url + "/login")
        page.get_by_label("Email address").fill("staff@slotrescue.test")
        page.get_by_label("Password", exact=True).fill(
            os.getenv("DEMO_PASSWORD", "DemoPassphrase!2026")
        )
        page.get_by_role("button", name="Sign in", exact=True).click()
        page.wait_for_url(base_url + "/staff")
        page.screenshot(path=str(output / "staff-desktop.png"), full_page=True)
        page.goto(base_url + "/docs", wait_until="networkidle")
        page.locator(".swagger-ui .opblock").first.wait_for()
        assert page.locator(".swagger-ui .opblock").count() >= 30
        mobile = browser.new_context(
            viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True
        )
        mobile_page = mobile.new_page()
        for path, filename in [("/", "landing-mobile.png"), ("/slots", "slots-mobile.png")]:
            mobile_page.goto(base_url + path, wait_until="networkidle")
            assert mobile_page.evaluate("document.documentElement.scrollWidth <= 390"), (
                f"Horizontal overflow on {path}"
            )
            mobile_page.screenshot(path=str(output / filename), full_page=True)
        mobile.add_cookies(context.cookies())
        for path in ["/staff", "/staff/services", "/staff/slots", "/staff/bookings"]:
            mobile_page.goto(base_url + path, wait_until="networkidle")
            assert mobile_page.evaluate("document.documentElement.scrollWidth <= 390"), (
                f"Horizontal overflow on {path}"
            )
        assert not errors, errors
        assert not remote_assets, remote_assets
        browser.close()
        print(
            "Browser checks passed: HTMX login/filter/book/cancel, staff dashboard, local Swagger, mobile overflow, no remote assets or JavaScript errors."
        )
        print(f"Screenshots: {output.resolve()}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    run(parser.parse_args().base_url.rstrip("/"))
