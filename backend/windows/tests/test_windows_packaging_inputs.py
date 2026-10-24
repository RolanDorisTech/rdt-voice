from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "backend" / "windows" / "scripts"


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lock_validator = load_module(SCRIPTS / "validate_windows_requirements_lock.py", "windows_lock_validator")
notice_collector = load_module(SCRIPTS / "collect_third_party_notices.py", "windows_notice_collector")


class WindowsDependencyLockTests(unittest.TestCase):
    def test_every_package_must_be_exactly_pinned_and_hashed(self) -> None:
        digest_a = "a" * 64
        digest_b = "b" * 64
        parsed = lock_validator.parse_lock(
            f"""
            # resolver output
            fastapi==0.115.14 \\
                --hash=sha256:{digest_a} \\
                --hash=sha256:{digest_b}
            numpy==2.3.5 \\
                --hash=sha256:{'c' * 64}
            """
        )
        self.assertEqual(parsed["fastapi"]["version"], "0.115.14")
        self.assertEqual(len(parsed["fastapi"]["hashes"]), 2)
        self.assertIn("numpy", parsed)

    def test_unhashed_or_unpinned_entries_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least one SHA-256 hash"):
            lock_validator.parse_lock("fastapi==0.115.14\n")
        with self.assertRaisesRegex(ValueError, "exact name==version pin"):
            lock_validator.parse_lock("fastapi>=0.115\n    --hash=sha256:" + "a" * 64 + "\n")

    def test_direct_pin_mismatch_marks_a_lock_stale(self) -> None:
        parsed = lock_validator.parse_lock("fastapi==0.115.14\n    --hash=sha256:" + "a" * 64 + "\n")
        with self.assertRaisesRegex(ValueError, "lock is stale"):
            lock_validator.check_direct_pins(parsed, "fastapi==0.115.15\n")
        with self.assertRaisesRegex(ValueError, "missing from lock"):
            lock_validator.check_direct_pins(parsed, "fastapi==0.115.14\nnumpy==2.3.5\n")

    def test_direct_urls_includes_and_untrusted_options_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "not allowed"):
            lock_validator.parse_lock("-r shared-requirements.txt\n")
        with self.assertRaisesRegex(ValueError, "exact name==version pin"):
            lock_validator.parse_lock("fastapi @ https://example.invalid/file.whl\n")

    def test_native_architecture_is_checked(self) -> None:
        lock_validator.check_process_architecture("ARM64", "ARM64")
        lock_validator.check_process_architecture("x64", "AMD64")
        with self.assertRaisesRegex(ValueError, "native architecture mismatch"):
            lock_validator.check_process_architecture("ARM64", "AMD64")
        with self.assertRaisesRegex(ValueError, "native architecture mismatch"):
            lock_validator.check_process_architecture("x64", "ARM64")


class WindowsNoticeCollectionTests(unittest.TestCase):
    PIN = "d1be6fde11ac6e0407606b4e42fe72d34add8037"
    WIN_MARKERS = {
        "sys_platform": "win32",
        "platform_system": "Windows",
        "os_name": "nt",
        "platform_machine": "AMD64",
        "python_version": "3.13",
        "python_full_version": "3.13.16",
    }

    def write_distribution(self, site: Path, name: str, version: str = "1.0", requires: tuple[str, ...] = (), with_license: bool = True) -> None:
        normalized = notice_collector.normalize_name(name)
        dist_info_name = f"{name}-{version}.dist-info"
        dist_info = site / dist_info_name
        dist_info.mkdir(parents=True, exist_ok=True)
        metadata_lines = ["Metadata-Version: 2.3", f"Name: {name}", f"Version: {version}", "License: MIT"]
        metadata_lines.extend(f"Requires-Dist: {req}" for req in requires)
        metadata_path = dist_info / "METADATA"
        metadata_path.write_text("\n".join(metadata_lines) + "\n", encoding="utf-8")
        records = [f"{dist_info_name}/METADATA,,"]
        if with_license:
            package_dir = site / normalized
            package_dir.mkdir(parents=True, exist_ok=True)
            license_path = package_dir / "LICENSE"
            license_path.write_text(f"License text for {name} {version}\n", encoding="utf-8")
            records.append(f"{normalized}/LICENSE,,")
        records.append(f"{dist_info_name}/RECORD,,")
        (dist_info / "RECORD").write_text("\n".join(records) + "\n", encoding="utf-8")

    def make_fixture(self, base: Path, *, missing_python_license: str | None = None, missing_ffmpeg_license: bool = False):
        repo = base / "repo"
        repo.mkdir()
        (repo / "LICENSE").write_text("RDT license text\n", encoding="utf-8")
        opencc = repo / "licenses" / "third-party" / "opencc-js"
        (opencc / "LICENSES").mkdir(parents=True)
        (opencc / "LICENSE").write_text("OpenCC MIT license\n", encoding="utf-8")
        (opencc / "NOTICE.md").write_text("OpenCC notice\n", encoding="utf-8")
        (opencc / "THIRD_PARTY_LICENSES.md").write_text("OpenCC attribution\n", encoding="utf-8")
        (opencc / "LICENSES" / "Apache-2.0.txt").write_text("Apache license\n", encoding="utf-8")
        pins = repo / "backend" / "whisper_cpp"
        pins.mkdir(parents=True)
        (pins / "engine-pins.json").write_text(json.dumps({"source_commit": self.PIN}), encoding="utf-8")
        whisper = base / "whisper-source"
        (whisper / "ggml").mkdir(parents=True)
        (whisper / "LICENSE").write_text("whisper MIT license\n", encoding="utf-8")
        (whisper / "ggml" / "LICENSE").write_text("GGML license\n", encoding="utf-8")
        (whisper / "ggml" / "licenses").mkdir()
        (whisper / "ggml" / "licenses" / "Apache-2.0.txt").write_text("GGML Apache license\n", encoding="utf-8")
        ffmpeg = base / "ffmpeg"
        (ffmpeg / "licenses").mkdir(parents=True)
        (ffmpeg / "ffmpeg.exe").write_bytes(b"fake ffmpeg binary")
        (ffmpeg / "ffprobe.exe").write_bytes(b"fake ffprobe binary")
        if not missing_ffmpeg_license:
            (ffmpeg / "LICENSE.txt").write_text("FFmpeg LGPL distribution notice\n", encoding="utf-8")
            (ffmpeg / "COPYING.LGPLv2.1").write_text("LGPL license text\n", encoding="utf-8")
            (ffmpeg / "licenses" / "distribution-terms.txt").write_text("Additional distributor terms\n", encoding="utf-8")
        site = base / "site-packages"
        site.mkdir()
        self.write_distribution(site, "fastapi", requires=("starlette",))
        self.write_distribution(site, "starlette", requires=("idna; python_version >= '3.12'",), with_license=missing_python_license != "starlette")
        self.write_distribution(site, "idna")
        self.write_distribution(site, "qrcode", requires=("colorama; sys_platform == 'win32'",))
        self.write_distribution(site, "colorama")
        self.write_distribution(site, "PyInstaller")
        roots = repo / "backend" / "windows"
        roots.mkdir(parents=True)
        roots_file = roots / "runtime-package-roots.txt"
        roots_file.write_text("fastapi\nqrcode\n", encoding="utf-8")
        return repo, whisper, ffmpeg, site, roots_file

    def fake_process(self, args, **kwargs):
        if args and args[0] == "git":
            return subprocess.CompletedProcess(args, 0, stdout=self.PIN + "\n", stderr="")
        if args and str(args[0]).lower().endswith("ffmpeg.exe") and args[-1] == "-L":
            return subprocess.CompletedProcess(args, 0, stdout="ffmpeg version test\nLicensed under LGPLv2.1+\n", stderr="")
        raise AssertionError(f"unexpected subprocess call: {args}")

    def test_complete_notice_material_is_collected_into_the_staging_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, whisper, ffmpeg, site, roots_file = self.make_fixture(Path(tmp))
            destination = Path(tmp) / "stage" / "third_party_notices"
            with patch.object(notice_collector.subprocess, "run", side_effect=self.fake_process):
                manifest = notice_collector.collect_notices(
                    repo, whisper, ffmpeg, site, destination, roots_file, self.WIN_MARKERS
                )
            expected = (
                "RDT-Connect-LICENSE.txt",
                "opencc-js/NOTICE.md",
                "opencc-js/LICENSES/Apache-2.0.txt",
                "whisper.cpp/LICENSE",
                "whisper.cpp/ggml/LICENSE",
                "whisper.cpp/ggml/licenses/Apache-2.0.txt",
                "ffmpeg/LICENSE.txt",
                "ffmpeg/licenses/distribution-terms.txt",
                "ffmpeg/COPYING.LGPLv2.1",
                "ffmpeg/ffmpeg-license-info.txt",
                "python/runtime/fastapi-1.0/fastapi/LICENSE",
                "python/runtime/colorama-1.0/colorama/LICENSE",
                "python/embedded-build-components/pyinstaller-1.0/pyinstaller/LICENSE",
                "NOTICE-MANIFEST.json",
                "BUILD-NOTICES.md",
            )
            for relative in expected:
                self.assertTrue((destination / relative).is_file(), relative)
            self.assertEqual(manifest["whisper_cpp"]["source_commit"], self.PIN)
            self.assertEqual(manifest["ffmpeg"]["executables"]["ffmpeg.exe"], notice_collector.sha256_file(ffmpeg / "ffmpeg.exe"))
            self.assertIn("colorama", [item["name"].lower() for item in manifest["python_distributions"]])
            self.assertTrue(all(len(item["sha256"]) == 64 for item in manifest["source_notice_files"]))

    def test_python_dependency_missing_license_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, whisper, ffmpeg, site, roots_file = self.make_fixture(Path(tmp), missing_python_license="starlette")
            with patch.object(notice_collector.subprocess, "run", side_effect=self.fake_process):
                with self.assertRaisesRegex(RuntimeError, "no license/notice text found.*starlette"):
                    notice_collector.collect_notices(
                        repo, whisper, ffmpeg, site, Path(tmp) / "stage", roots_file, self.WIN_MARKERS
                    )

    def test_missing_ffmpeg_license_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, whisper, ffmpeg, site, roots_file = self.make_fixture(Path(tmp), missing_ffmpeg_license=True)
            with patch.object(notice_collector.subprocess, "run", side_effect=self.fake_process):
                with self.assertRaisesRegex(RuntimeError, "no license/notice files"):
                    notice_collector.collect_notices(
                        repo, whisper, ffmpeg, site, Path(tmp) / "stage", roots_file, self.WIN_MARKERS
                    )

    def test_wrong_whisper_commit_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, whisper, ffmpeg, site, roots_file = self.make_fixture(Path(tmp))
            def wrong_commit(args, **kwargs):
                if args and args[0] == "git":
                    return subprocess.CompletedProcess(args, 0, stdout="f" * 40 + "\n", stderr="")
                return self.fake_process(args, **kwargs)
            with patch.object(notice_collector.subprocess, "run", side_effect=wrong_commit):
                with self.assertRaisesRegex(RuntimeError, "whisper.cpp source mismatch"):
                    notice_collector.collect_notices(
                        repo, whisper, ffmpeg, site, Path(tmp) / "stage", roots_file, self.WIN_MARKERS
                    )


if __name__ == "__main__":
    unittest.main(verbosity=2)
