import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class WorkspaceGuidanceTest(unittest.TestCase):
    def test_appends_reviewed_section_preserves_bytes_and_is_idempotent(self):
        from gradient_ascent import workspace_guidance as guidance

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            root.mkdir(mode=0o700)
            original = b"# Custom instructions\r\n\r\nKeep this exact text."
            (root / "AGENTS.md").write_bytes(original)
            (root / ".gitignore").write_bytes(b"custom-entry")
            first = guidance.install_coaching_history_guidance(root)
            self.assertEqual(first, {"installed": True, "ignore_updated": True})
            body = (root / "AGENTS.md").read_bytes()
            self.assertTrue(body.startswith(original))
            self.assertEqual(body.count(guidance.MANAGED_START), 1)
            self.assertIn(b"add-coaching-context --file DRAFT", body)
            self.assertEqual((root / ".gitignore").read_bytes(), b"custom-entry\nplan/.history/\n")
            self.assertEqual((root / "AGENTS.md").stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                guidance.install_coaching_history_guidance(root),
                {"installed": False, "ignore_updated": False},
            )
            self.assertEqual((root / "AGENTS.md").read_bytes(), body)
            self.assertFalse((root / "plan").exists())

    def test_fresh_template_marker_avoids_duplicate_section(self):
        from gradient_ascent import workspace_guidance as guidance

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            body = (Path(guidance.__file__).parent / "workspace_templates/AGENTS.md").read_bytes()
            (root / "AGENTS.md").write_bytes(body)
            self.assertFalse(guidance.install_coaching_history_guidance(root)["installed"])
            self.assertEqual((root / "AGENTS.md").read_bytes(), body)

    def test_rejects_unsafe_files_and_oversize_without_partial_changes(self):
        from gradient_ascent import workspace_guidance as guidance

        for name in ("AGENTS.md", ".gitignore"):
            for unsafe in ("symlink", "hardlink", "writable", "oversize"):
                with self.subTest(name=name, unsafe=unsafe), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp) / "workspace"
                    root.mkdir(mode=0o700)
                    (root / "AGENTS.md").write_bytes(b"Custom")
                    (root / ".gitignore").write_bytes(b"Custom ignore")
                    target = root / name
                    target.unlink()
                    outside = Path(tmp) / "outside"
                    outside.write_bytes(b"Protected")
                    if unsafe == "symlink":
                        target.symlink_to(outside)
                    elif unsafe == "hardlink":
                        os.link(outside, target)
                    elif unsafe == "writable":
                        target.write_bytes(b"Unsafe")
                        target.chmod(0o666)
                    else:
                        target.write_bytes(b"x" * (guidance.MAX_GUIDANCE_BYTES + 1))
                    before = {
                        file: (root / file).read_bytes() for file in ("AGENTS.md", ".gitignore")
                    }
                    with self.assertRaises((OSError, ValueError)):
                        guidance.install_coaching_history_guidance(root)
                    self.assertEqual({file: (root / file).read_bytes() for file in before}, before)
                    self.assertEqual(outside.read_bytes(), b"Protected")

    def test_replacement_root_is_not_written(self):
        from gradient_ascent import workspace_guidance as guidance

        with tempfile.TemporaryDirectory() as tmp:
            root, old = Path(tmp) / "workspace", Path(tmp) / "old"
            root.mkdir(mode=0o700)
            (root / "AGENTS.md").write_bytes(b"Original")
            read = guidance._files._read
            replaced = False

            def replace(directory, name, limit):
                nonlocal replaced
                body = read(directory, name, limit)
                if name == ".gitignore" and not replaced:
                    replaced = True
                    root.rename(old)
                    root.mkdir(mode=0o700)
                    (root / "AGENTS.md").write_bytes(b"Replacement")
                return body

            with (
                mock.patch.object(guidance._files, "_read", side_effect=replace),
                self.assertRaises(RuntimeError),
            ):
                guidance.install_coaching_history_guidance(root)
            self.assertEqual((root / "AGENTS.md").read_bytes(), b"Replacement")
            self.assertFalse((root / ".gitignore").exists())
            self.assertEqual((old / "AGENTS.md").read_bytes(), b"Original")

    def test_partial_managed_marker_fails_without_overwriting_instructions(self):
        from gradient_ascent import workspace_guidance as guidance

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            body = b"Custom\n" + guidance.MANAGED_START
            (root / "AGENTS.md").write_bytes(body)
            with self.assertRaises(ValueError):
                guidance.install_coaching_history_guidance(root)
            self.assertEqual((root / "AGENTS.md").read_bytes(), body)
            self.assertFalse((root / ".gitignore").exists())
