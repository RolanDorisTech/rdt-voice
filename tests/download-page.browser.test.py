#!/usr/bin/env python3
"""Offline Chromium integration checks for download.html / download.js.

Uses mocked GitHub API and mocked attachment response. No network download occurs.
"""
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from threading import Thread
from functools import partial
try:
    from playwright.sync_api import sync_playwright
except ModuleNotFoundError as error:
    if error.name == "playwright" or (error.name or "").startswith("playwright."):
        print("SKIP: Playwright is not installed; browser integration was not run.")
        raise SystemExit(0)
    raise
import json
import os

ROOT = Path(__file__).resolve().parents[1]
TAG = "rdt-connect-v0.2.0-dev.10"
NAME = "RDT_Connect_0.2.0-dev.10_aarch64.dmg"
URL = f"https://github.com/RolanDorisTech/rdt-voice/releases/download/{TAG}/{NAME}"
release = {
    "tag_name": TAG,
    "name": "Display title is ignored",
    "draft": False,
    "prerelease": True,
    "published_at": "2026-10-17T00:00:00Z",
    "assets": [{"name": NAME, "state": "uploaded", "size": 371715568,
                "digest": "sha256:" + "a" * 64, "browser_download_url": URL}],
}

class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass

httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(ROOT)))
Thread(target=httpd.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{httpd.server_port}"
failures = []
checks = 0

def check(label, fn):
    global checks
    checks += 1
    try:
        assert fn(), label
        print("PASS", label)
    except Exception as e:
        failures.append(label)
        print("FAIL", label, str(e))

try:
    with sync_playwright() as p:
        launch_options = {
            "headless": True,
            "args": ["--no-sandbox", "--allow-file-access-from-files"],
        }
        chromium_path = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
        if chromium_path:
            launch_options["executable_path"] = chromium_path
        browser = p.chromium.launch(**launch_options)
        page = browser.new_page(accept_downloads=True)
        api_calls = {"n": 0, "mode": "good"}

        def route_api(route):
            api_calls["n"] += 1
            if api_calls["mode"] == "retry" and api_calls["n"] == 1:
                route.fulfill(status=503, headers={"Access-Control-Allow-Origin": "*"}, body="temporary test failure")
            else:
                route.fulfill(status=200, content_type="application/json", headers={"Access-Control-Allow-Origin": "*"}, body=json.dumps([release]))
        page.route("https://api.github.com/repos/RolanDorisTech/rdt-voice/releases?per_page=100", route_api)
        html = (ROOT / "download.html").read_text().replace('<script src="./download.js" defer></script>', "")
        page.set_content(html)
        page.add_script_tag(path=str(ROOT / "download.js"))
        page.wait_for_function("document.querySelector('#download-link').hidden === false || document.querySelector('#retry-button').hidden === false")
        link = page.locator("#download-link")
        check("resolves valid release without redirecting away", lambda: page.url == "about:blank")
        check("download link becomes visible after validation", lambda: link.is_visible())
        check("link points at exact expected asset", lambda: link.get_attribute("href") == URL)
        check("version and exact human-readable size shown", lambda: "0.2.0-dev.10" in page.locator("#release-details").inner_text() and "354.5 MiB (371,715,568 bytes)" in page.locator("#release-details").inner_text())
        check("retry hidden after success", lambda: page.locator("#retry-button").is_hidden())
        # The sandbox blocks browser navigation to external GitHub URLs, so prevent
        # the actual network handoff and verify only the click-state behavior here.
        link.evaluate("el => el.addEventListener('click', event => event.preventDefault(), { once: true })")
        link.click()
        check("download click triggers handoff messaging", lambda: "Starting the download" in page.locator("#status").inner_text())
        check("status explains browser Downloads progress", lambda: "Downloads panel" in page.locator("#status").inner_text())
        check("handoff progress is visible and indeterminate", lambda: page.locator("#download-progress").is_visible() and page.locator("#download-progress").get_attribute("aria-valuenow") is None)
        check("page remains usable after handoff", lambda: page.url == "about:blank")
        check("progress help explains byte-progress limitation", lambda: page.locator("#download-help").is_visible() and "does not report byte-by-byte progress" in page.locator("#download-help").inner_text())

        # Failure path: use a fresh page because download.js declares module-level consts.
        page2 = browser.new_page()
        retry_calls = {"n": 0}
        def fail_then_succeed(route):
            retry_calls["n"] += 1
            if retry_calls["n"] == 1:
                route.fulfill(status=503, headers={"Access-Control-Allow-Origin": "*"}, body="temporary test failure")
            else:
                route.fulfill(status=200, content_type="application/json", headers={"Access-Control-Allow-Origin": "*"}, body=json.dumps([release]))
        page2.route("https://api.github.com/repos/RolanDorisTech/rdt-voice/releases?per_page=100", fail_then_succeed)
        page2.set_content(html)
        page2.add_script_tag(path=str(ROOT / "download.js"))
        page2.wait_for_function("document.querySelector('#retry-button').hidden === false")
        check("failure hides download link and reveals retry", lambda: page2.locator("#download-link").is_hidden() and page2.locator("#retry-button").is_visible() and "retry" in page2.locator("#status").inner_text().lower())
        page2.locator("#retry-button").click()
        page2.wait_for_function("document.querySelector('#download-link').hidden === false")
        check("retry resolves after temporary API failure", lambda: page2.locator("#download-link").is_visible() and retry_calls["n"] == 2)
        browser.close()
finally:
    httpd.shutdown()

print(f"\n{checks} browser assertions, {checks-len(failures)} passed, {len(failures)} failed")
raise SystemExit(1 if failures else 0)
