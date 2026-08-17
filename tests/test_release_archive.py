import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class ReleaseArchiveTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "release-source"
        (self.root / "scripts").mkdir(parents=True)
        (self.root / ".codex-plugin").mkdir()
        shutil.copyfile(
            REPO_ROOT / "scripts" / "build-release-zip.sh",
            self.root / "scripts" / "build-release-zip.sh",
        )
        (self.root / ".gitignore").write_text("dist/\nprivate-data/\n", encoding="utf-8")
        self._versions("9.8.7", "9.8.7")
        self._git("init", "-q")
        self._commit()

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "git",
                "-C",
                str(self.root),
                "-c",
                "user.name=Release Tests",
                "-c",
                "user.email=release-tests@example.invalid",
                "-c",
                "commit.gpgSign=false",
                "-c",
                "core.hooksPath=/dev/null",
                *args,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def _versions(self, package: str, plugin: str) -> None:
        (self.root / "pyproject.toml").write_text(
            f'[project]\nname = "gradient-ascent"\nversion = {json.dumps(package)}\n',
            encoding="utf-8",
        )
        (self.root / ".codex-plugin" / "plugin.json").write_text(
            json.dumps({"name": "gradient-ascent", "version": plugin}),
            encoding="utf-8",
        )

    def _commit(self) -> None:
        self._git("add", "--all")
        self._git("commit", "-q", "-m", "Synthetic release snapshot")

    def _build(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(self.root / "scripts" / "build-release-zip.sh"), *args],
            cwd=self.root,
            env={**os.environ, "PYTHON": sys.executable},
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_archive_uses_committed_matching_version_and_tracked_files_only(self) -> None:
        (self.root / "private-data").mkdir()
        (self.root / "private-data" / "excluded.txt").write_text(
            "synthetic private fixture", encoding="utf-8"
        )

        result = self._build()

        self.assertEqual(result.returncode, 0, result.stderr)
        archive = self.root / "dist" / "gradient-ascent-9.8.7.zip"
        self.assertEqual(Path(result.stdout.strip()), archive)
        with zipfile.ZipFile(archive) as contents:
            names = contents.namelist()
            self.assertTrue(all(name.startswith("gradient-ascent-9.8.7/") for name in names))
            self.assertFalse(any("private-data" in name for name in names))
            self.assertEqual(
                contents.comment.decode("ascii"),
                self._git("rev-parse", "HEAD").stdout.strip(),
            )
            self.assertEqual(
                contents.read("gradient-ascent-9.8.7/pyproject.toml"),
                (self.root / "pyproject.toml").read_bytes(),
            )

    def test_custom_output_keeps_the_release_version_prefix(self) -> None:
        output = Path(self.temp.name) / "downloads" / "source.zip"

        result = self._build(str(output))

        self.assertEqual(result.returncode, 0, result.stderr)
        with zipfile.ZipFile(output) as contents:
            self.assertTrue(
                all(name.startswith("gradient-ascent-9.8.7/") for name in contents.namelist())
            )

    def test_mismatched_or_unsafe_versions_fail_before_creating_output(self) -> None:
        for package, plugin in (("9.8.7", "9.8.6"), ("../unsafe", "../unsafe")):
            with self.subTest(package=package, plugin=plugin):
                self._versions(package, plugin)
                self._commit()
                output = Path(self.temp.name) / "invalid" / "source.zip"

                result = self._build(str(output))

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("version", result.stderr.lower())
                self.assertFalse(output.parent.exists())

    def test_dirty_checkout_is_not_packaged(self) -> None:
        (self.root / "unreviewed.txt").write_text("uncommitted", encoding="utf-8")

        result = self._build()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("dirty checkout", result.stderr)
        self.assertFalse((self.root / "dist").exists())
