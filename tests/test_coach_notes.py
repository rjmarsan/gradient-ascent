import json
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from gradient_ascent.coach_notes import add_coach_note, coach_notes_by_date, load_coach_notes
from gradient_ascent.storage import write_json


class CoachNotesTest(unittest.TestCase):
    def test_portable_windows_modes_allow_normal_files_but_reject_reparse_points(self) -> None:
        from gradient_ascent import coach_notes

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "plan/coach_notes.json"
            write_json(path, {"version": 1, "notes": []})
            original_lstat = Path.lstat

            def windows_stat(candidate, *, reparse=None):
                value = original_lstat(candidate)
                attributes = {
                    name: getattr(value, name)
                    for name in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_nlink", "st_uid")
                }
                attributes["st_mode"] = (
                    (stat.S_IFDIR | 0o777) if candidate.is_dir() else (stat.S_IFREG | 0o666)
                )
                attributes["st_file_attributes"] = 0x400 if candidate == reparse else 0
                return SimpleNamespace(**attributes)

            with mock.patch.object(coach_notes, "_POSIX_PERMISSIONS", False, create=True):
                with mock.patch.object(Path, "lstat", autospec=True, side_effect=windows_stat):
                    self.assertEqual(coach_notes._portable_read(path), path.read_bytes())
                for rejected in (root, root / "plan", path):
                    with (
                        self.subTest(reparse=rejected.name),
                        mock.patch.object(
                            Path,
                            "lstat",
                            autospec=True,
                            side_effect=lambda candidate: windows_stat(candidate, reparse=rejected),
                        ),
                    ):
                        with self.assertRaisesRegex(ValueError, "reparse|unsafe"):
                            coach_notes._portable_read(path)

    def test_portable_posix_permissions_still_reject_other_writers(self) -> None:
        from gradient_ascent import coach_notes

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "plan/coach_notes.json"
            write_json(path, {"version": 1, "notes": []})
            path.parent.chmod(0o777)
            with mock.patch.object(coach_notes, "_POSIX_PERMISSIONS", True, create=True):
                with self.assertRaises(ValueError):
                    coach_notes._portable_read(path)

    def test_activity_label_without_id_and_proposal_kind_remain_honest(self) -> None:
        from gradient_ascent.coaching_history import capture_coaching_entry

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            saved = add_coach_note(
                root, note_date="2026-04-08", note="Label only", activity_name="Synthetic ride"
            )
            self.assertEqual(saved["entry"]["activity_name"], "Synthetic ride")
            self.assertEqual(saved["entry"]["ride_id"], "")
            capture_coaching_entry(
                root,
                {
                    "kind": "proposal",
                    "title": "Consider recovery",
                    "body": "Not approved",
                    "rationale": "Open choice",
                    "idempotency_key": "proposal-day",
                    "scopes": [
                        {"kind": "day", "start_date": "2026-04-08", "end_date": "2026-04-08"}
                    ],
                },
            )
            proposal = next(row for row in load_coach_notes(root) if row.get("kind") == "proposal")
            self.assertEqual(proposal["title"], "Proposal: Consider recovery")
            self.assertEqual(proposal["note"], "Not approved")

    def test_new_notes_use_journal_once_and_preserve_legacy_bytes(self) -> None:
        from gradient_ascent.coaching_history import recall_coaching_history

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "plan/coach_notes.json"
            write_json(
                legacy,
                {
                    "version": 1,
                    "notes": [{"id": "old", "date": "2026-04-08", "note": "Old observation"}],
                },
            )
            before = legacy.read_bytes()
            kwargs = dict(
                note_date="2026-04-08",
                note="A concise new takeaway.",
                title="Takeaway",
                ride_id="strava:123",
                activity_name="Synthetic ride",
                tags="fueling,race",
                codex_thread_id="synthetic-thread",
            )
            first = add_coach_note(root, **kwargs)
            second = add_coach_note(root, **kwargs)
            self.assertEqual(first["entry"]["id"], second["entry"]["id"])
            self.assertEqual(first["entry"]["note"], kwargs["note"])
            self.assertEqual(first["entry"]["ride_id"], "strava:123")
            self.assertEqual(first["entry"]["activity_name"], "Synthetic ride")
            self.assertEqual(first["entry"]["tags"], ["fueling", "race"])
            self.assertEqual(first["entry"]["codex_url"], "codex://threads/synthetic-thread")
            self.assertEqual(first["history_status"], "available")
            self.assertTrue(first["created"])
            self.assertFalse(second["created"])
            self.assertEqual(first["count"], 2)
            self.assertEqual(legacy.read_bytes(), before)
            self.assertEqual(len(recall_coaching_history(root)), 1)
            self.assertEqual(len(coach_notes_by_date(root)["2026-04-08"]), 2)

    def test_explicit_retry_key_rejects_different_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = add_coach_note(
                root, note_date="2026-04-08", note="One", idempotency_key="same-note"
            )
            again = add_coach_note(
                root, note_date="2026-04-08", note="One", idempotency_key="same-note"
            )
            self.assertEqual(first["entry"]["id"], again["entry"]["id"])
            with self.assertRaisesRegex(ValueError, "different content"):
                add_coach_note(
                    root, note_date="2026-04-08", note="Different", idempotency_key="same-note"
                )

    def test_malformed_legacy_notes_are_never_reset_or_written_over(self) -> None:
        for value in (
            {"version": 1, "notes": {}},
            {"version": 1, "notes": ["bad"]},
            {"version": 2, "notes": []},
        ):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = root / "plan/coach_notes.json"
                write_json(path, value)
                before = path.read_bytes()
                with self.assertRaises(ValueError):
                    add_coach_note(root, note_date="2026-04-08", note="Do not write")
                self.assertEqual(path.read_bytes(), before)
                self.assertFalse((root / "plan/.history/journal.json").exists())

    def test_unsupported_empty_history_keeps_legacy_behavior_but_nonempty_fails(self) -> None:
        from gradient_ascent import coaching_history

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(coaching_history, "_secure_files_supported", return_value=False):
                first = add_coach_note(root, note_date="2026-04-08", note="Portable note")
                again = add_coach_note(root, note_date="2026-04-08", note="Portable note")
                self.assertEqual(first["history_status"], "unavailable")
                self.assertEqual(first["entry"]["id"], again["entry"]["id"])
            self.assertEqual(
                len(json.loads((root / "plan/coach_notes.json").read_text())["notes"]), 1
            )
            add_coach_note(root, note_date="2026-04-09", note="Journal note")
            before = (root / "plan/coach_notes.json").read_bytes()
            with mock.patch.object(coaching_history, "_secure_files_supported", return_value=False):
                with self.assertRaises(RuntimeError):
                    add_coach_note(root, note_date="2026-04-10", note="Must fail")
            self.assertEqual((root / "plan/coach_notes.json").read_bytes(), before)

    def test_legacy_links_are_sanitized_and_symlinks_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "plan/coach_notes.json"
            write_json(
                path,
                {
                    "version": 1,
                    "notes": [
                        {"date": "2026-04-08", "note": "Keep text", "codex_url": "javascript:bad"}
                    ],
                },
            )
            self.assertEqual(load_coach_notes(root)[0]["codex_url"], "")
            path.unlink()
            outside = root / "outside.json"
            write_json(outside, {"version": 1, "notes": []})
            path.symlink_to(outside)
            with self.assertRaises(ValueError):
                add_coach_note(root, note_date="2026-04-08", note="Do not follow")

    def test_coach_note_accepts_codex_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = add_coach_note(
                Path(tmp),
                note_date="2026-07-09",
                note="Keep the next endurance ride controlled.",
                codex_url="codex://threads/019ef732-753d-7163-8971-41cd5467a322",
            )

        self.assertEqual(
            result["entry"]["codex_url"],
            "codex://threads/019ef732-753d-7163-8971-41cd5467a322",
        )

    def test_coach_note_rejects_non_codex_links_before_writing(self) -> None:
        for unsafe_url in (
            "https://example.com/thread",
            "javascript:alert(1)",
            "file:///tmp/private",
            "/relative/path",
            "codex://threads/",
            "codex://other/thread-id",
            "codex://threads/thread-id?redirect=https://example.com",
        ):
            with self.subTest(url=unsafe_url), tempfile.TemporaryDirectory() as tmp:
                data_dir = Path(tmp)
                with self.assertRaisesRegex(ValueError, "codex://"):
                    add_coach_note(
                        data_dir,
                        note_date="2026-07-09",
                        note="Do not persist this note.",
                        codex_url=unsafe_url,
                    )
                self.assertFalse((data_dir / "plan" / "coach_notes.json").exists())

    def test_coach_note_rejects_malformed_thread_ids_before_writing(self) -> None:
        for unsafe_thread_id in ("thread/id", "thread-id?redirect=elsewhere", "../thread-id"):
            with self.subTest(thread_id=unsafe_thread_id), tempfile.TemporaryDirectory() as tmp:
                data_dir = Path(tmp)
                with self.assertRaisesRegex(ValueError, "thread id"):
                    add_coach_note(
                        data_dir,
                        note_date="2026-07-09",
                        note="Do not persist this note.",
                        codex_thread_id=unsafe_thread_id,
                    )
                self.assertFalse((data_dir / "plan" / "coach_notes.json").exists())


if __name__ == "__main__":
    unittest.main()
