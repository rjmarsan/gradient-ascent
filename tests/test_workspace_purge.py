import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gradient_ascent.cli import _init_workspace
from gradient_ascent.workspace import preview_workspace_purge, purge_workspace_data


class WorkspacePurgeTest(unittest.TestCase):
    def test_preview_is_non_destructive_and_lists_private_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            _init_workspace(workspace, force=False)
            private_note = workspace / "context" / "private.md"
            private_note.write_text("private athlete note\n", encoding="utf-8")

            preview = preview_workspace_purge(workspace)

            self.assertTrue(private_note.exists())
            self.assertEqual(preview["workspace"], str(workspace.resolve()))
            self.assertIn("context", preview["existing_paths"])
            self.assertIn("plan", preview["existing_paths"])
            self.assertIn(".env", preview["existing_paths"])

    def test_purge_requires_exact_resolved_path_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            _init_workspace(workspace, force=False)

            with self.assertRaisesRegex(ValueError, "confirmation"):
                purge_workspace_data(workspace, confirmation="workspace")

            self.assertTrue((workspace / "plan" / "athlete.json").exists())

    def test_purge_removes_the_entire_workspace_including_git_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            _init_workspace(workspace, force=False)
            (workspace / ".git").mkdir()
            (workspace / ".git" / "config").write_text("private git config\n", encoding="utf-8")
            (workspace / "context" / "private.md").write_text("private note\n", encoding="utf-8")
            (workspace / "imports" / "archive.zip").write_bytes(b"private archive")
            (workspace / "derived" / "training_center.html").write_text(
                "private dashboard\n", encoding="utf-8"
            )

            result = purge_workspace_data(workspace, confirmation=str(workspace.resolve()))

            self.assertEqual(result["workspace"], str(workspace.resolve()))
            self.assertFalse(workspace.exists())

    def test_refuses_non_workspace_and_symlink_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ordinary = root / "ordinary"
            ordinary.mkdir()
            with self.assertRaisesRegex(ValueError, "Gradient Ascent workspace"):
                preview_workspace_purge(ordinary)

            workspace = root / "workspace"
            _init_workspace(workspace, force=False)
            link = root / "workspace-link"
            link.symlink_to(workspace, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symbolic link"):
                preview_workspace_purge(link)

    def test_refuses_root_home_and_filesystem_mounts(self) -> None:
        for unsafe in (Path("/"), Path.home()):
            with self.subTest(unsafe=unsafe), self.assertRaisesRegex(ValueError, "unsafe path"):
                preview_workspace_purge(unsafe)

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            _init_workspace(workspace, force=False)
            with patch("gradient_ascent.workspace.Path.is_mount", return_value=True):
                with self.assertRaisesRegex(ValueError, "mount point"):
                    preview_workspace_purge(workspace)

            mounted_child = workspace / "imports" / "mounted-volume"
            with patch(
                "gradient_ascent.workspace._cross_device_descendants",
                return_value=[mounted_child],
            ):
                with self.assertRaisesRegex(ValueError, "another filesystem mount"):
                    preview_workspace_purge(workspace)

    def test_cli_previews_then_requires_exact_path_to_delete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            _init_workspace(workspace, force=False)
            env = {**os.environ, "COACH_WORKSPACE_DIR": str(workspace)}

            preview = subprocess.run(
                [sys.executable, "-m", "gradient_ascent.cli", "purge-workspace", str(workspace)],
                check=True,
                cwd=Path(__file__).resolve().parents[1],
                env=env,
                text=True,
                capture_output=True,
            )
            preview_payload = json.loads(preview.stdout)
            self.assertFalse(preview_payload["deleted"])
            self.assertTrue(workspace.exists())

            deleted = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "gradient_ascent.cli",
                    "purge-workspace",
                    str(workspace),
                    "--confirm",
                    str(workspace.resolve()),
                ],
                check=True,
                cwd=Path(__file__).resolve().parents[1],
                env=env,
                text=True,
                capture_output=True,
            )
            deleted_payload = json.loads(deleted.stdout)
            self.assertTrue(deleted_payload["deleted"])
            self.assertFalse(workspace.exists())


if __name__ == "__main__":
    unittest.main()
