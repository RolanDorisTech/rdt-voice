from pathlib import Path
import re

css = Path("src/style.css").read_text()
marker = "/* 1D39: equal spacing for direct controls in General settings. */"

assert css.count(marker) == 1, "Expected exactly one 1D39 spacing block."
block = css.split(marker, 1)[1]

assert re.search(
    r"(?m)^#settings-general\s*\{\s*gap:\s*10px\s*!important;\s*\}",
    block,
), "General parent gap is missing."

assert re.search(
    r"#settings-general\s*>\s*\.card-heading,\s*"
    r"#settings-general\s*>\s*\.settings-language-row,\s*"
    r"#settings-general\s*>\s*\.menu-bar-option,\s*"
    r"#settings-general\s*>\s*#offline-only-mode-setting\s*\{\s*"
    r"margin-block:\s*0\s*!important;",
    block,
), "Scoped direct-child margin reset is missing."

# Reject only unscoped selectors; the correctly scoped child selector is valid.
assert not re.search(r"(?m)^\.settings-general-card\s*\{", block)
assert not re.search(r"(?m)^\.menu-bar-option\s*\{", block)
assert not re.search(r"(?m)^#offline-only-mode-setting\s*\{", block)

print("PASS: General settings share one 10px parent gap.")
print("PASS: direct-child margins are reset with scoped selectors.")
print("PASS: no broad or unscoped selectors were added.")
