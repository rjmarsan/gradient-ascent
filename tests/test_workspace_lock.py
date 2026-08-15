import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import patch

from gradient_ascent import workspace as workspace_module
from gradient_ascent.cli import _init_workspace
from gradient_ascent.workspace import purge_workspace_data
from gradient_ascent.workspace_lock import (
    cross_process_locking_available,
    workspace_identity,
    workspace_lock,
)


class WorkspaceLockTest(unittest.TestCase):
    def test_workspace_generation_changes_when_recreated_inode_is_reused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            _init_workspace(workspace, force=False)
            resolved_workspace = workspace.resolve()
            original_inode = workspace.stat().st_ino
            original_stat = Path.stat

            def reused_inode(path: Path, *args, **kwargs):
                result = original_stat(path, *args, **kwargs)
                if path in (workspace, resolved_workspace):
                    values = list(result)
                    values[stat.ST_INO] = original_inode
                    return os.stat_result(values)
                return result

            with patch.object(Path, "stat", reused_inode):
                old_identity = workspace_identity(workspace)
                purge_workspace_data(workspace, confirmation=str(workspace.resolve()))
                _init_workspace(workspace, force=False)

                self.assertNotEqual(workspace_identity(workspace), old_identity)
                with self.assertRaisesRegex(RuntimeError, "generation changed"):
                    with workspace_lock(workspace, expected_identity=old_identity):
                        self.fail("A stale writer must not enter the recreated workspace")

    def test_workspace_generation_marker_is_private_and_survives_force_init(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            _init_workspace(workspace, force=False)
            marker = workspace / ".codex" / "cache" / "workspace-generation"
            original_identity = workspace_identity(workspace)
            original_marker = marker.read_text(encoding="ascii")

            self.assertEqual(stat.S_IMODE(marker.stat().st_mode), 0o600)
            self.assertEqual(len(original_marker), 64)
            _init_workspace(workspace, force=True)

            self.assertEqual(marker.read_text(encoding="ascii"), original_marker)
            self.assertEqual(workspace_identity(workspace), original_identity)

    def test_legacy_workspace_gets_a_stable_generation_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            marker = workspace / ".codex" / "cache" / "workspace-generation"

            first_identity = workspace_identity(workspace)

            self.assertTrue(marker.is_file())
            self.assertEqual(workspace_identity(workspace), first_identity)

    def test_workspace_generation_rejects_symlink_marker_or_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index, relative in enumerate(
                (".codex", ".codex/cache", ".codex/cache/workspace-generation")
            ):
                with self.subTest(relative=relative):
                    workspace = root / f"workspace-{index}"
                    workspace.mkdir()
                    target = root / f"target-{index}"
                    if relative.endswith("workspace-generation"):
                        target.write_text("0" * 64, encoding="ascii")
                    else:
                        target.mkdir()
                    link = workspace / relative
                    link.parent.mkdir(parents=True, exist_ok=True)
                    link.symlink_to(target, target_is_directory=target.is_dir())

                    with self.assertRaisesRegex(OSError, "symlink"):
                        workspace_identity(workspace)

                    shutil.rmtree(workspace)
                    if target.is_dir():
                        target.rmdir()
                    else:
                        target.unlink()

    def test_removed_or_replaced_generation_marker_invalidates_stale_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            _init_workspace(workspace, force=False)
            marker = workspace / ".codex" / "cache" / "workspace-generation"
            original_identity = workspace_identity(workspace)

            marker.unlink()
            replacement_identity = workspace_identity(workspace)
            self.assertNotEqual(replacement_identity, original_identity)
            with self.assertRaisesRegex(RuntimeError, "generation changed"):
                with workspace_lock(workspace, expected_identity=original_identity):
                    self.fail("A missing marker must invalidate the original identity")

            marker.write_text("0" * 64, encoding="ascii")
            with self.assertRaisesRegex(RuntimeError, "generation changed"):
                with workspace_lock(workspace, expected_identity=replacement_identity):
                    self.fail("A replaced marker must invalidate the previous identity")

    def test_refresh_subprocess_rejects_a_stale_workspace_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            _init_workspace(workspace, force=False)
            stale_identity = workspace_identity(workspace)
            marker = workspace / ".codex" / "cache" / "workspace-generation"
            marker.unlink()
            current_identity = workspace_identity(workspace)
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "gradient_ascent.refresh",
                    "--data-dir",
                    str(workspace),
                    "--expected-workspace-device",
                    str(stale_identity[0]),
                    "--expected-workspace-inode",
                    str(stale_identity[1]),
                ],
                cwd=Path(__file__).resolve().parents[1],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("generation changed", result.stderr)
            self.assertEqual(workspace_identity(workspace), current_identity)

    def test_invalid_or_public_generation_marker_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            _init_workspace(workspace, force=False)
            marker = workspace / ".codex" / "cache" / "workspace-generation"

            marker.write_text("not-a-generation", encoding="ascii")
            with self.assertRaisesRegex(OSError, "invalid"):
                workspace_identity(workspace)

            if hasattr(os, "getuid"):
                marker.write_text("0" * 64, encoding="ascii")
                marker.chmod(0o644)
                with self.assertRaisesRegex(PermissionError, "not private"):
                    workspace_identity(workspace)

    def test_exec_process_waits_for_workspace_lock(self) -> None:
        if not cross_process_locking_available():
            self.skipTest("Cross-process file locking is unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            marker = Path(tmp) / "child-acquired"
            _init_workspace(workspace, force=False)
            script = "\n".join(
                [
                    "import sys",
                    "from pathlib import Path",
                    "from gradient_ascent.workspace_lock import workspace_lock",
                    "with workspace_lock(Path(sys.argv[1])):",
                    "    Path(sys.argv[2]).write_text('acquired', encoding='utf-8')",
                ]
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])

            with workspace_lock(workspace):
                process = subprocess.Popen(
                    [sys.executable, "-c", script, str(workspace), str(marker)],
                    cwd=Path(__file__).resolve().parents[1],
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                time.sleep(0.2)
                self.assertIsNone(process.poll())
                self.assertFalse(marker.exists())

            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, f"{stdout}\n{stderr}")
            self.assertEqual(marker.read_text(encoding="utf-8"), "acquired")

    def test_unicode_equivalent_workspace_names_share_one_lock(self) -> None:
        if not cross_process_locking_available():
            self.skipTest("Cross-process file locking is unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nfc_workspace = root / unicodedata.normalize("NFC", "Cafe\u0301")
            nfd_workspace = root / unicodedata.normalize("NFD", "Café")
            _init_workspace(nfc_workspace, force=False)
            try:
                same_workspace = nfd_workspace.exists() and nfd_workspace.samefile(nfc_workspace)
            except OSError:
                same_workspace = False
            if not same_workspace or nfc_workspace == nfd_workspace:
                self.skipTest("Filesystem does not alias NFC and NFD names")

            marker = root / "unicode-child-acquired"
            script = "\n".join(
                [
                    "import sys",
                    "from pathlib import Path",
                    "from gradient_ascent.workspace_lock import workspace_lock",
                    "with workspace_lock(Path(sys.argv[1])):",
                    "    Path(sys.argv[2]).write_text('acquired', encoding='utf-8')",
                ]
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
            with workspace_lock(nfc_workspace):
                process = subprocess.Popen(
                    [sys.executable, "-c", script, str(nfd_workspace), str(marker)],
                    cwd=Path(__file__).resolve().parents[1],
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                time.sleep(0.2)
                self.assertIsNone(process.poll())
                self.assertFalse(marker.exists())
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, f"{stdout}\n{stderr}")
            self.assertEqual(marker.read_text(encoding="utf-8"), "acquired")

    def test_purge_waits_for_writer_and_deleted_workspace_cannot_resurrect(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            _init_workspace(workspace, force=False)
            writer_acquired = Event()
            release_writer = Event()

            def held_writer() -> None:
                with workspace_lock(workspace):
                    writer_acquired.set()
                    self.assertTrue(release_writer.wait(timeout=5))
                    (workspace / "derived" / "writer-finished").write_text(
                        "finished\n",
                        encoding="utf-8",
                    )

            with ThreadPoolExecutor(max_workers=2) as executor:
                writer = executor.submit(held_writer)
                self.assertTrue(writer_acquired.wait(timeout=2))
                purge = executor.submit(
                    purge_workspace_data,
                    workspace,
                    confirmation=str(workspace.resolve()),
                )
                time.sleep(0.1)
                self.assertFalse(purge.done())
                release_writer.set()
                writer.result(timeout=5)
                result = purge.result(timeout=5)

            self.assertTrue(result["deleted"])
            self.assertFalse(workspace.exists())

    def test_purge_holds_lock_before_validating_workspace_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            _init_workspace(workspace, force=False)
            validation_started = Event()
            continue_validation = Event()
            original_validate = workspace_module._validated_workspace

            def paused_validation(path: Path) -> Path:
                validation_started.set()
                self.assertTrue(continue_validation.wait(timeout=5))
                return original_validate(path)

            def competing_writer() -> None:
                with workspace_lock(workspace):
                    self.fail("Competing writer must not enter before purge validation")

            with (
                patch.object(
                    workspace_module,
                    "_validated_workspace",
                    side_effect=paused_validation,
                ),
                ThreadPoolExecutor(max_workers=2) as executor,
            ):
                purge = executor.submit(
                    purge_workspace_data,
                    workspace,
                    confirmation=str(workspace.resolve()),
                )
                self.assertTrue(validation_started.wait(timeout=2))
                writer = executor.submit(competing_writer)
                time.sleep(0.1)
                self.assertFalse(writer.done())
                continue_validation.set()
                self.assertTrue(purge.result(timeout=5)["deleted"])
                with self.assertRaises(FileNotFoundError):
                    writer.result(timeout=5)

            self.assertFalse(workspace.exists())
            with self.assertRaises(FileNotFoundError):
                with workspace_lock(workspace):
                    self.fail("A deleted workspace must not be lockable")
            self.assertFalse(workspace.exists())

    def test_force_init_waits_for_existing_workspace_writer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            _init_workspace(workspace, force=False)
            with ThreadPoolExecutor(max_workers=1) as executor:
                with workspace_lock(workspace):
                    future = executor.submit(_init_workspace, workspace, force=True)
                    time.sleep(0.1)
                    self.assertFalse(future.done())
                result = future.result(timeout=5)

            self.assertEqual(result["workspace_dir"], str(workspace))
            self.assertTrue((workspace / "plan" / "goals.md").is_file())


if __name__ == "__main__":
    unittest.main()
