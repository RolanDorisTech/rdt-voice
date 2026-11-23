from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[3]
COLLECTOR_PATH = REPO / "backend/linux/scripts/collect_third_party_notices.py"
SPEC = importlib.util.spec_from_file_location("linux_notice_collector", COLLECTOR_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("could not load Linux notice collector")
COLLECTOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(COLLECTOR)


class FakeDistribution:
    def __init__(
        self,
        name: str,
        version: str,
        package_root: Path,
        *,
        requires: tuple[str, ...] = (),
        has_license: bool = True,
    ) -> None:
        self.metadata = {
            "Name": name,
            "License": "MIT",
            "License-Expression": "MIT",
        }
        self.version = version
        self.requires = list(requires)
        self.files = ["licenses/LICENSE"] if has_license else []
        self.package_root = package_root

    def locate_file(self, item: str) -> Path:
        return self.package_root / item


class LinuxThirdPartyNoticeCollectionTests(unittest.TestCase):
    def make_repo(self, root: Path) -> Path:
        repo = root / "repo"
        repo.mkdir()
        (repo / "LICENSE").write_text(
            "Repository license fixture — unchanged\n", encoding="utf-8"
        )
        (repo / "LICENSE_HISTORY.md").write_text(
            "License history fixture\n", encoding="utf-8"
        )
        opencc = repo / "licenses/third-party/opencc-js"
        (opencc / "LICENSES").mkdir(parents=True)
        (opencc / "LICENSE").write_text("OpenCC MIT fixture\n", encoding="utf-8")
        (opencc / "NOTICE.md").write_text("OpenCC notice fixture\n", encoding="utf-8")
        (opencc / "THIRD_PARTY_LICENSES.md").write_text(
            "OpenCC third-party fixture\n", encoding="utf-8"
        )
        (opencc / "LICENSES/Apache-2.0.txt").write_text(
            "Apache fixture\n", encoding="utf-8"
        )
        return repo

    def make_whisper_source(
        self,
        root: Path,
        *,
        include_ggml_license: bool = True,
        unrelated_symlink: bool = False,
    ) -> tuple[Path, str]:
        source = root / "whisper.cpp"
        source.mkdir()
        (source / "LICENSE").write_text("whisper.cpp MIT fixture\n", encoding="utf-8")
        (source / "ggml").mkdir()
        if include_ggml_license:
            (source / "ggml/LICENSE").write_text(
                "GGML MIT fixture\n", encoding="utf-8"
            )
        (source / "ggml/COPYING").write_text(
            "GGML copyright fixture\n", encoding="utf-8"
        )
        if unrelated_symlink:
            (source / "unrelated-link").symlink_to(source / "LICENSE")

        subprocess.run(["git", "-C", str(source), "init", "-q"], check=True)
        subprocess.run(
            ["git", "-C", str(source), "config", "user.name", "Linux notice test"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(source), "config", "user.email", "notice-test@example.invalid"],
            check=True,
        )
        subprocess.run(["git", "-C", str(source), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(source), "commit", "-qm", "license fixture"],
            check=True,
        )
        commit = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        return source, commit

    def write_whisper_pin(self, repo: Path, commit: str) -> None:
        pins = repo / "backend/whisper_cpp"
        pins.mkdir(parents=True, exist_ok=True)
        (pins / "engine-pins.json").write_text(
            json.dumps({"source_commit": commit}) + "\n", encoding="utf-8"
        )

    @staticmethod
    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def make_ffmpeg_notices(self, destination: Path) -> Path:
        root = destination / "ffmpeg"
        files = {
            "source/ffmpeg-9.1.tar.xz": b"verified FFmpeg source archive fixture",
            "source/ffmpeg-9.1.tar.xz.asc": b"detached signature fixture",
            "source/ffmpeg-devel.asc": b"FFmpeg signing key fixture",
            "COPYING.LGPLv2.1": b"LGPL-2.1 license text fixture\n",
            "FFMPEG-LICENSE-INFO.txt": b"FFmpeg license: LGPL version 2.1 or later\n",
            "FFMPEG-VERSION.txt": b"ffmpeg version 9.1\n",
            "FFMPEG-BUILD-CONFIGURATION.txt": b"--disable-gpl --disable-nonfree\n",
            "FFMPEG-CONFIGURE-ARGS.txt": b"--enable-shared\n",
        }
        for relative, content in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

        archive = root / "source/ffmpeg-9.1.tar.xz"
        signature = root / "source/ffmpeg-9.1.tar.xz.asc"
        records = [
            {
                "path": relative,
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            for relative, content in sorted(files.items())
        ]
        manifest = {
            "schema": "rdt-linux-ffmpeg-bundle-v1",
            "version": "9.1",
            "platform": "linux",
            "architecture": "x86_64",
            "license": "LGPL-2.1-or-later",
            "gpl_enabled": False,
            "nonfree_enabled": False,
            "dynamic_linking": True,
            "source": {
                "source_archive": "source/ffmpeg-9.1.tar.xz",
                "sha256": self.digest(archive),
                "signature_sha256": self.digest(signature),
            },
            "notice_files": records,
            "notice_size_bytes": sum(item["size_bytes"] for item in records),
            "runtime_size_bytes": 0,
        }
        (root / "FFMPEG-BUILD-MANIFEST.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return root

    def make_python_distributions(
        self,
        site: Path,
        *,
        fastapi_has_license: bool = True,
        fastapi_requires: tuple[str, ...] = (
            "colorama>=1",
            'win-runtime>=1; sys_platform == "win32"',
        ),
        include_transitive: bool = True,
    ) -> dict:
        site.mkdir(parents=True, exist_ok=True)

        def create(
            name: str,
            *,
            requires: tuple[str, ...] = (),
            has_license: bool = True,
        ) -> FakeDistribution:
            package_root = site / f"{COLLECTOR.normalize_name(name)}-1.0"
            if has_license:
                license_path = package_root / "licenses/LICENSE"
                license_path.parent.mkdir(parents=True, exist_ok=True)
                license_path.write_text(
                    f"{name} license fixture\n", encoding="utf-8"
                )
            return FakeDistribution(
                name,
                "1.0",
                package_root,
                requires=requires,
                has_license=has_license,
            )

        result = {
            "fastapi": create(
                "fastapi",
                requires=fastapi_requires,
                has_license=fastapi_has_license,
            ),
            "pyinstaller": create("PyInstaller"),
        }
        if include_transitive:
            result["colorama"] = create("colorama")
        return result

    def test_base_collection_copies_repository_and_opencc_with_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self.make_repo(root)
            destination = root / "linux-notices"
            manifest = COLLECTOR.collect(repo, destination)

            self.assertEqual(
                (destination / "Repository-LICENSE.txt").read_text(encoding="utf-8"),
                (repo / "LICENSE").read_text(encoding="utf-8"),
            )
            self.assertFalse((destination / "RDT-Connect-LICENSE.txt").exists())
            self.assertTrue((destination / "LICENSE-HISTORY.txt").is_file())
            self.assertTrue(
                (destination / "opencc-js/LICENSES/Apache-2.0.txt").is_file()
            )
            self.assertTrue((destination / "BUILD-NOTICES.md").is_file())
            self.assertTrue((destination / "NOTICE-MANIFEST.json").is_file())
            self.assertIn(
                "licenses/third-party/opencc-js",
                manifest["base_notice_sources"],
            )
            self.assertGreaterEqual(manifest["notice_file_count"], 6)

    def test_collection_preserves_cuda_manifest_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self.make_repo(root)
            destination = root / "linux-notices"
            COLLECTOR.collect(repo, destination)
            manifest_path = destination / "NOTICE-MANIFEST.json"
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            data["cuda_runtime"] = {
                "driver_libraries_redistributed": False,
                "test_marker": "preserve",
            }
            manifest_path.write_text(json.dumps(data), encoding="utf-8")
            second = COLLECTOR.collect(repo, destination)
            self.assertEqual(
                second["cuda_runtime"]["test_marker"], "preserve"
            )

    def test_conflicting_existing_license_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self.make_repo(root)
            destination = root / "linux-notices"
            destination.mkdir()
            (destination / "Repository-LICENSE.txt").write_text(
                "unexpected user content\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(
                RuntimeError, "conflicting existing notice file"
            ):
                COLLECTOR.collect(repo, destination)

    def test_pinned_whisper_and_ggml_license_material_are_collected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self.make_repo(root)
            source, commit = self.make_whisper_source(
                root, unrelated_symlink=True
            )
            self.write_whisper_pin(repo, commit)
            destination = root / "linux-notices"
            COLLECTOR.collect(
                repo,
                destination,
                whisper_source=source,
            )
            manifest = json.loads(
                (destination / "NOTICE-MANIFEST.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["whisper_cpp"]["source_commit"], commit)
            self.assertTrue((destination / "whisper.cpp/LICENSE").is_file())
            self.assertTrue((destination / "whisper.cpp/ggml/LICENSE").is_file())
            self.assertTrue((destination / "whisper.cpp/ggml/COPYING").is_file())
            self.assertIn("## whisper.cpp and GGML", (
                destination / "BUILD-NOTICES.md"
            ).read_text(encoding="utf-8"))

    def test_wrong_whisper_commit_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self.make_repo(root)
            source, _ = self.make_whisper_source(root)
            self.write_whisper_pin(repo, "0" * 40)
            with self.assertRaisesRegex(RuntimeError, "source mismatch"):
                COLLECTOR.collect(
                    repo,
                    root / "linux-notices",
                    whisper_source=source,
                )

    def test_missing_ggml_license_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self.make_repo(root)
            source, commit = self.make_whisper_source(
                root, include_ggml_license=False
            )
            self.write_whisper_pin(repo, commit)
            with self.assertRaisesRegex(RuntimeError, "GGML license files are missing"):
                COLLECTOR.collect(
                    repo,
                    root / "linux-notices",
                    whisper_source=source,
                )

    def test_ffmpeg_source_license_and_manifest_are_validated(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self.make_repo(root)
            destination = root / "linux-notices"
            COLLECTOR.collect(repo, destination)
            self.make_ffmpeg_notices(destination)
            data = json.loads(
                (destination / "NOTICE-MANIFEST.json").read_text(encoding="utf-8")
            )
            data["cuda_runtime"] = {
                "driver_libraries_redistributed": False,
                "test_marker": "cuda-metadata-survives",
            }
            (destination / "NOTICE-MANIFEST.json").write_text(
                json.dumps(data), encoding="utf-8"
            )

            result = COLLECTOR.collect(
                repo,
                destination,
                ffmpeg_notices=destination / "ffmpeg",
            )
            self.assertEqual(result["ffmpeg"]["version"], "9.1")
            self.assertFalse(result["ffmpeg"]["gpl_enabled"])
            self.assertFalse(result["ffmpeg"]["nonfree_enabled"])
            self.assertEqual(
                result["cuda_runtime"]["test_marker"],
                "cuda-metadata-survives",
            )
            self.assertTrue(
                (destination / "ffmpeg/COPYING.LGPLv2.1").is_file()
            )
            self.assertTrue(
                (destination / "ffmpeg/FFMPEG-LICENSE-INFO.txt").is_file()
            )
            self.assertIn("## FFmpeg — Linux bundled runtime", (
                destination / "BUILD-NOTICES.md"
            ).read_text(encoding="utf-8"))

    def test_modified_ffmpeg_license_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self.make_repo(root)
            destination = root / "linux-notices"
            COLLECTOR.collect(repo, destination)
            ffmpeg = self.make_ffmpeg_notices(destination)
            (ffmpeg / "COPYING.LGPLv2.1").write_text(
                "tampered license text\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(RuntimeError, "hash/size mismatch"):
                COLLECTOR.collect(
                    repo,
                    destination,
                    ffmpeg_notices=ffmpeg,
                )

    @unittest.skipUnless(
        importlib.util.find_spec("packaging"),
        "packaging is required to exercise dependency marker evaluation",
    )
    def test_python_runtime_transitive_licenses_and_linux_markers_are_collected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self.make_repo(root)
            destination = root / "linux-notices"
            site = root / "site-packages"
            roots = root / "runtime-roots.txt"
            roots.write_text("fastapi\n", encoding="utf-8")
            distributions = self.make_python_distributions(site)
            distributions["win-runtime"] = self.make_python_distributions(
                site, include_transitive=False
            )["pyinstaller"]
            distributions["win-runtime"] = FakeDistribution(
                "win-runtime",
                "1.0",
                site / "win-runtime-1.0",
            )
            win_license = site / "win-runtime-1.0/licenses/LICENSE"
            win_license.parent.mkdir(parents=True)
            win_license.write_text("Windows-only license\n", encoding="utf-8")

            with patch.object(
                COLLECTOR,
                "_distribution_index",
                return_value=distributions,
            ):
                manifest = COLLECTOR.collect(
                    repo,
                    destination,
                    include_python_runtime=True,
                    runtime_roots_file=roots,
                    site_packages=site,
                    marker_environment={
                        "sys_platform": "linux",
                        "platform_system": "Linux",
                        "platform_machine": "x86_64",
                    },
                )

            self.assertEqual(
                manifest["python_runtime_distribution_count"], 2
            )
            self.assertTrue(
                (destination / "python/runtime/fastapi-1.0/licenses/LICENSE").is_file()
            )
            self.assertTrue(
                (destination / "python/runtime/colorama-1.0/licenses/LICENSE").is_file()
            )
            self.assertFalse(
                (destination / "python/runtime/win-runtime-1.0").exists()
            )
            self.assertTrue(
                (destination / "python/embedded-build-components/pyinstaller-1.0/licenses/LICENSE").is_file()
            )
            self.assertNotIn(str(site), json.dumps(manifest))

    @unittest.skipUnless(
        importlib.util.find_spec("packaging"),
        "packaging is required to exercise dependency closure",
    )
    def test_missing_python_transitive_dependency_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self.make_repo(root)
            destination = root / "linux-notices"
            site = root / "site-packages"
            roots = root / "runtime-roots.txt"
            roots.write_text("fastapi\n", encoding="utf-8")
            distributions = self.make_python_distributions(
                site,
                fastapi_requires=("missing-runtime>=1",),
                include_transitive=False,
            )
            with patch.object(
                COLLECTOR,
                "_distribution_index",
                return_value=distributions,
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "not installed.*missing-runtime"
                ):
                    COLLECTOR.collect(
                        repo,
                        destination,
                        include_python_runtime=True,
                        runtime_roots_file=roots,
                        site_packages=site,
                    )

    @unittest.skipUnless(
        importlib.util.find_spec("packaging"),
        "packaging is required to exercise license closure",
    )
    def test_python_distribution_without_license_text_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self.make_repo(root)
            destination = root / "linux-notices"
            site = root / "site-packages"
            roots = root / "runtime-roots.txt"
            roots.write_text("fastapi\n", encoding="utf-8")
            distributions = self.make_python_distributions(
                site,
                fastapi_has_license=False,
                fastapi_requires=(),
                include_transitive=False,
            )
            with patch.object(
                COLLECTOR,
                "_distribution_index",
                return_value=distributions,
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "no license/notice text found"
                ):
                    COLLECTOR.collect(
                        repo,
                        destination,
                        include_python_runtime=True,
                        runtime_roots_file=roots,
                        site_packages=site,
                    )

    @unittest.skipUnless(
        importlib.util.find_spec("packaging"),
        "packaging is required to exercise Python license closure",
    )
    def test_cpython_license_is_copied_and_hashed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self.make_repo(root)
            destination = root / "linux-notices"
            site = root / "site-packages"
            roots = root / "runtime-roots.txt"
            roots.write_text("fastapi\n", encoding="utf-8")
            base_prefix = root / "python-prefix"
            base_prefix.mkdir()
            (base_prefix / "LICENSE").write_text(
                "Python Software Foundation license fixture\\n",
                encoding="utf-8",
            )
            distributions = self.make_python_distributions(
                site,
                fastapi_requires=(),
                include_transitive=False,
            )
            with patch.object(
                COLLECTOR,
                "_distribution_index",
                return_value=distributions,
            ):
                manifest = COLLECTOR.collect(
                    repo,
                    destination,
                    include_python_runtime=True,
                    runtime_roots_file=roots,
                    site_packages=site,
                    python_base_prefix=base_prefix,
                )
            copied = destination / "python/cpython/LICENSE"
            self.assertEqual(
                copied.read_text(encoding="utf-8"),
                "Python Software Foundation license fixture\\n",
            )
            self.assertEqual(
                manifest["cpython_runtime"]["license_file"],
                "python/cpython/LICENSE",
            )
            self.assertEqual(
                manifest["cpython_runtime"]["sha256"],
                self.digest(copied),
            )
            self.assertNotIn(str(base_prefix), json.dumps(manifest))
            self.assertIn(
                "## CPython interpreter",
                (destination / "BUILD-NOTICES.md").read_text(encoding="utf-8"),
            )

    @unittest.skipUnless(
        importlib.util.find_spec("packaging"),
        "packaging is required to exercise Python license closure",
    )
    def test_missing_cpython_license_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self.make_repo(root)
            destination = root / "linux-notices"
            site = root / "site-packages"
            roots = root / "runtime-roots.txt"
            roots.write_text("fastapi\n", encoding="utf-8")
            base_prefix = root / "python-prefix"
            base_prefix.mkdir()
            distributions = self.make_python_distributions(
                site,
                fastapi_requires=(),
                include_transitive=False,
            )
            with patch.object(
                COLLECTOR,
                "_distribution_index",
                return_value=distributions,
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "CPython license text was not found"
                ):
                    COLLECTOR.collect(
                        repo,
                        destination,
                        include_python_runtime=True,
                        runtime_roots_file=roots,
                        site_packages=site,
                        python_base_prefix=base_prefix,
                    )

    def test_packager_finalizes_notices_after_ffmpeg_and_cuda_staging(self) -> None:
        script = (REPO / "rdt_connect/scripts/prepare-linux-bundle.sh").read_text(
            encoding="utf-8"
        )
        cuda = script.index("attach-notices")
        final_collection = script.index("--whisper-source")
        dependency_check = script.index(
            "# Refuse bundles with unresolved shared-library dependencies."
        )
        self.assertLess(cuda, final_collection)
        self.assertLess(final_collection, dependency_check)
        self.assertIn("--ffmpeg-notices", script)
        self.assertIn("--include-python-runtime", script)
        self.assertIn("--runtime-roots-file", script)

        workflow = (REPO / ".github/workflows/linux-native-qualification.yml").read_text(
            encoding="utf-8"
        )
        bundle_step = workflow.index("- name: Build one-folder Linux backend bundle")
        self.assertIn(
            'WHISPER_CPP_SOURCE="$RUNNER_TEMP/whisper.cpp"',
            workflow[bundle_step:],
        )

    def test_linux_runtime_roots_match_packager_direct_dependencies(self) -> None:
        roots = (
            REPO / "backend/linux/runtime-package-roots.txt"
        ).read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            [line.strip() for line in roots if line.strip() and not line.lstrip().startswith("#")],
            [
                "fastapi",
                "httpx",
                "numpy",
                "python-multipart",
                "qrcode",
                "uvicorn",
                "backports.tarfile",
            ],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
