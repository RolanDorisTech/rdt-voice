#!/usr/bin/env python3
import json
import re
from pathlib import Path

root = Path(__file__).resolve().parents[1]
main = (root / "src/main.ts").read_text()
html = (root / "index.html").read_text()
css = (root / "src/style.css").read_text()
locales = sorted((root / "src/locales").glob("*.json"))
keys = [
    "msg.update_success_title.1d40",
    "msg.update_success_body.1d40",
    "msg.update_success_version.1d40",
    "msg.update_success_build.1d40",
    "msg.update_success_continue.1d40",
]
assert len(locales) == 26, f"Expected 26 locales, got {len(locales)}"
for path in locales:
    messages = json.loads(path.read_text()).get("messages", {})
    for key in keys:
        assert isinstance(messages.get(key), str) and messages[key].strip(), f"Missing {key} in {path.name}"

success = main.index("await update.install({ restartAfterInstall: false });")
armed = main.index("recordSuccessfulUpdateInstall(update.version);", success)
assert armed < main.index("pendingTauriUpdate = null;", success)
assert "consumePendingUpdateSuccess(info.version, info.buildId);" in main
assert "normalizeUpdateVersionForComparison(pendingVersion) ===" in main
assert "normalizeUpdateVersionForComparison(actualVersion)" in main
assert "pendingBuildId.toUpperCase() === actualBuildId.trim().toUpperCase()" in main
assert "if (!versionMatches || !buildMatches) return;" in main
assert "localStorage.removeItem(UPDATE_SUCCESS_STORAGE_KEY);" in main
assert "now - record.installedAt >= 0" in main
assert "now - record.installedAt <= 30 * 24 * 60 * 60 * 1000" in main
assert "Number.isFinite(record.installedAt)" in main
assert 'role="dialog"' in html and 'aria-modal="true"' in html
assert 'id="rdt-update-success-continue"' in html
assert ".rdt-update-success-overlay[hidden]" in css
assert "@media (prefers-reduced-motion: no-preference)" in css
print("PASS: update success arms only after install, normalized version/build-ID match, one-time consumption, expiry, accessible dialog, reduced motion, and all 26 localized catalogs.")
