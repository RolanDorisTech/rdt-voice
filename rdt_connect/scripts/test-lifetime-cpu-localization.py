from pathlib import Path
import json

source = Path("src/main.ts").read_text()
assert 'const cpuLabel = translateKey("msg.cpu.1d22");' in source
assert 'return model ? `${model[1]} ${cpuLabel}` : cpuLabel;' in source
assert 'return model ? `${model[1]} CPU` : "CPU";' not in source

files = sorted(Path("src/locales").glob("*.json"))
assert len(files) == 26, f"Expected 26 locales, found {len(files)}"
for path in files:
    data = json.loads(path.read_text())
    messages = data.get("messages", {})
    value = messages.get("msg.cpu.1d22") if isinstance(messages, dict) else None
    assert isinstance(value, str) and value.strip(), f"Missing msg.cpu.1d22: {path.name}"

print(f"PASS: Lifetime Statistics CPU label uses msg.cpu.1d22 in all {len(files)} locales.")
