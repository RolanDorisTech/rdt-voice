#!/usr/bin/env python3
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KEY = "msg.offline_update_tooltip.1d40"
ENGLISH = "please turn off offline mode to use update features"

main = (ROOT / "src/main.ts").read_text()
css = (ROOT / "src/style.css").read_text()
assert KEY in main
assert 'setAttribute("role", "tooltip")' in main
assert "aria-describedby" in main
assert "offlineTooltipAddedTabindex" in main
assert "data-offline-update-tooltip" in css
assert ":hover .offline-update-tooltip" in css
assert ":focus-visible .offline-update-tooltip" in css
assert ":focus-within .offline-update-tooltip" in css

locales = sorted((ROOT / "src/locales").glob("*.json"))
assert len(locales) == 26, f"expected 26 locales, found {len(locales)}"
for path in locales:
    data = json.loads(path.read_text())
    value = data["messages"].get(KEY)
    assert isinstance(value, str) and value.strip(), f"missing {KEY}: {path.name}"
en = json.loads((ROOT / "src/locales/en.json").read_text())
assert en["messages"][KEY] == ENGLISH
print(f"PASS: Offline Mode update tooltip, accessibility hooks, and locale parity ({len(locales)} locales).")
