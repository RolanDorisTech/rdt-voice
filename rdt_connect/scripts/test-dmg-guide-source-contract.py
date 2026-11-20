from pathlib import Path

root = Path(__file__).resolve().parents[1]
guide = root / "assets/macos-dmg-template/Installation_Guide.html"
builder = (root / "scripts/build-custom-dmg.py").read_text(errors="replace")

assert guide.is_file(), "repository-owned DMG guide missing"
html = guide.read_text(errors="replace")
assert html.count("data:image/png;base64") == 5, "embedded guide images changed"
assert 'source_guide = ROOT / "assets" / "macos-dmg-template" / "Installation_Guide.html"' in builder
assert 'TEMPLATE.parent.mkdir(parents=True, exist_ok=True)' in builder
assert '"/usr/bin/ditto", "--rsrc", "--extattr", "--acl",' in builder
assert 'str(source_guide), str(TEMPLATE),' in builder
assert 'rdt_connect_guide_icon_240.png' in builder
assert 'apply-macos-file-icon.swift' in builder
assert "Finder's valid custom ICNS resource" in builder
assert 'iconized staged guide' in builder
assert 'approved repository Installation_Guide.html is missing' in builder
assert 'TEMPLATE = BUILD / "custom-dmg-template" / "Installation_Guide.html"' in builder
print("PASS: durable DMG guide source, embedded images and generated staging path.")
