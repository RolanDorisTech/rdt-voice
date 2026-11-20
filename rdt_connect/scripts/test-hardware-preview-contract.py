from pathlib import Path
import re

main = Path("src/main.ts").read_text()
css = Path("src/style.css").read_text()
build = Path("scripts/build-macos-with-marker.sh").read_text()

pattern = re.compile(r"([1-9]|1[0-9]|20) hardware")
for number in range(1, 21):
    assert pattern.fullmatch(f"{number} hardware")
for invalid in ("0 hardware", "21 hardware", "01 hardware", "2 hardware now", "hardware 2"):
    assert not pattern.fullmatch(invalid)

assert "const previewCountMatch = /^([1-9]|1[0-9]|20) hardware$/.exec(phrase);" in main
assert 'phrase === "hardware" || phrase === "native"' in main
assert "developerHardwarePreviewCount = null;" in main
assert main.count("renderDeveloperHardwarePreviewPanels();") >= 3

start = main.index("/* RDT_HARDWARE_PREVIEW_PANELS_1D40_BEGIN */")
end = main.index("/* RDT_HARDWARE_PREVIEW_PANELS_1D40_END */", start)
preview = main[start:end]
assert "checkbox.disabled = true;" in preview
assert 'checkbox.type = "checkbox";' in preview
assert "data-benchmark-device" not in preview
assert 'inferenceDeviceStatus.insertAdjacentElement(' in preview
assert 'benchmarkHardwareSelector.appendChild(' in preview

assert 'querySelectorAll<HTMLInputElement>("input[data-benchmark-device]")' in main
assert "selectedManualHardwareDevices()" in main
assert "RDT_HARDWARE_PREVIEW_PANELS_1D40_BEGIN" in css
assert ".rdt-hardware-preview-row" in css
assert 'test-hardware-preview-contract.py' in build

print("PASS: exactly 1–20 hardware commands; hardware/native reset.")
print("PASS: both preview panels render disabled simulated candidates only.")
print("PASS: preview controls cannot enter the real benchmark-device input list.")
print("PASS: hardware preview contract is wired into the Mac build gate.")
