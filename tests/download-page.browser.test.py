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
        page.on("pageerror", lambda error: print("PAGE ERROR:", error))
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
        # Install mocks after set_content so they exist in the actual test document.
        page.evaluate("""() => {
          window.__writtenBytes = 0;
          Object.defineProperty(window, "showSaveFilePicker", {
            configurable: true,
            writable: true,
            value: async () => ({
              createWritable: async () => ({
                write: async chunk => { window.__writtenBytes += chunk.byteLength; },
                close: async () => {},
                abort: async () => {}
              })
            })
          });
          const nativeFetch = window.fetch.bind(window);
          window.fetch = async (input, init) => {
            const href = typeof input === "string" ? input : input.url;
            if (href.startsWith("https://rdt-connect-download-proxy.rolandoristech.workers.dev/download/")) {
              return new Response(new ReadableStream({
                start(controller) {
                  controller.enqueue(new Uint8Array([1, 2, 3, 4]));
                  setTimeout(() => {
                    controller.enqueue(new Uint8Array([5, 6, 7, 8]));
                    controller.close();
                  }, 350);
                }
              }), { status: 200, headers: {
                "Content-Length": "8",
                "Content-Type": "application/octet-stream"
              }});
            }
            return nativeFetch(input, init);
          };
        }""")
        page.add_script_tag(path=str(ROOT / "download.js"))
        page.wait_for_function("document.querySelector('#download-link').hidden === false || document.querySelector('#retry-button').hidden === false")
        link = page.locator("#download-link")
        check("resolves valid release without redirecting away", lambda: page.url == "about:blank")
        check("download link becomes visible after validation", lambda: link.is_visible())
        check("link points at exact expected asset", lambda: link.get_attribute("href") == URL)
        check("version and exact human-readable size shown", lambda: "0.2.0-dev.10" in page.locator("#release-details").inner_text() and "354.5 MiB (371,715,568 bytes)" in page.locator("#release-details").inner_text())
        check("retry hidden after success", lambda: page.locator("#retry-button").is_hidden())
        cancel_button = page.locator("#cancel-download-button")
        link.click()
        page.wait_for_function(
            "!document.querySelector('#cancel-download-button').hidden",
            timeout=3000)
        check("cancel button is visible during active download",
              lambda: cancel_button.is_visible())
        try:
            page.wait_for_function("document.querySelector('#status').innerText.includes('Download complete')", timeout=10000)
        except Exception:
            print("DEBUG status:", page.locator("#status").inner_text())
            print("DEBUG save API:", page.evaluate("typeof window.showSaveFilePicker"))
            print("DEBUG proxy URL:", link.get_attribute("data-proxy-url"))
            raise
        check("download completion is reported after stream finishes", lambda: "Download complete" in page.locator("#status").inner_text())
        check("byte counter reflects bytes written to file", lambda: page.evaluate("window.__writtenBytes") == 8)
        check("in-page progress reaches 100 percent", lambda: page.locator("#download-progress").is_visible() and page.locator("#download-progress").get_attribute("aria-valuenow") == "100")
        check("progress fill reaches full width", lambda: page.locator("#download-progress-fill").evaluate("el => el.style.width") == "100%")
        check("progress describes bytes rather than animation", lambda: "written" in page.locator("#download-progress").get_attribute("aria-valuetext"))
        check("progress help stays hidden during measured download", lambda: page.locator("#download-help").is_hidden())
        check("cancel button hides after successful completion", lambda: cancel_button.is_hidden())
        check("page remains usable after handoff", lambda: page.url == "about:blank")

        # Cancellation path: hold the response stream open, cancel it, and
        # verify the partial writer is aborted instead of reporting success.
        page3 = browser.new_page()
        page3.route(
            "https://api.github.com/repos/RolanDorisTech/rdt-voice/releases?per_page=100",
            lambda route: route.fulfill(
                status=200, content_type="application/json",
                headers={"Access-Control-Allow-Origin": "*"},
                body=json.dumps([release])))
        page3.set_content(html)
        page3.evaluate("""() => {
          window.__abortedPartialFile = false;
          window.showSaveFilePicker = async () => ({
            createWritable: async () => ({
              write: async () => {},
              close: async () => {},
              abort: async () => { window.__abortedPartialFile = true; }
            })
          });
          const nativeFetch = window.fetch.bind(window);
          window.fetch = async (input, init) => {
            const href = typeof input === "string" ? input : input.url;
            if (href.startsWith("https://rdt-connect-download-proxy.rolandoristech.workers.dev/download/")) {
              return new Response(new ReadableStream({
                start(controller) {
                  controller.enqueue(new Uint8Array([1, 2, 3, 4]));
                }
              }), {status: 200, headers: {
                "Content-Length": "1000",
                "Content-Type": "application/octet-stream"
              }});
            }
            return nativeFetch(input, init);
          };
        }""")
        page3.add_script_tag(path=str(ROOT / "download.js"))
        page3.wait_for_function(
            "document.querySelector('#download-link').hidden === false")
        page3.locator("#download-link").click()
        page3.wait_for_function(
            "!document.querySelector('#cancel-download-button').hidden")
        page3.locator("#cancel-download-button").click()
        page3.wait_for_function(
            "document.querySelector('#status').innerText.includes('Download canceled')",
            timeout=5000)
        check("cancel stops active download with explicit status",
              lambda: "Download canceled" in page3.locator("#status").inner_text())
        check("partial file writer is aborted",
              lambda: page3.evaluate("window.__abortedPartialFile") is True)
        check("progress is hidden after cancellation",
              lambda: page3.locator("#download-progress").is_hidden())
        check("cancel button hides after cancellation",
              lambda: page3.locator("#cancel-download-button").is_hidden())

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
