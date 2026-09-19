import os
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:5000"
OUT = "/tmp/orkung_screens"
os.makedirs(OUT, exist_ok=True)

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path="/opt/pw-browsers/chromium/chrome-linux/chrome" if os.path.exists("/opt/pw-browsers/chromium/chrome-linux/chrome") else None)
    for label, viewport in [("desktop", {"width": 1360, "height": 900}), ("mobile", {"width": 390, "height": 844})]:
        ctx = browser.new_context(viewport=viewport)
        page = ctx.new_page()
        page.goto(f"{BASE}/login")
        page.fill("#username", "admin")
        page.fill("#password", "Admin#2026")
        page.click("button[type=submit]")
        page.wait_for_load_state("networkidle")
        page.screenshot(path=f"{OUT}/{label}_dashboard.png", full_page=True)

        page.goto(f"{BASE}/animals")
        page.wait_for_load_state("networkidle")
        page.screenshot(path=f"{OUT}/{label}_animals.png", full_page=True)

        # first animal profile
        link = page.query_selector("table.data tbody tr td a")
        if link:
            link.click()
            page.wait_for_load_state("networkidle")
            page.screenshot(path=f"{OUT}/{label}_profile.png", full_page=True)

        page.goto(f"{BASE}/animals/add")
        page.wait_for_load_state("networkidle")
        page.screenshot(path=f"{OUT}/{label}_add_animal.png", full_page=True)

        page.goto(f"{BASE}/reports")
        page.wait_for_load_state("networkidle")
        page.screenshot(path=f"{OUT}/{label}_reports.png", full_page=True)

        if label == "mobile":
            page.goto(f"{BASE}/")
            page.wait_for_load_state("networkidle")
            page.click("#menuBtn")
            page.wait_for_timeout(200)
            page.screenshot(path=f"{OUT}/{label}_drawer_open.png", full_page=False)

        ctx.close()
    browser.close()
print("Screenshots written to", OUT)
