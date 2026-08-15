import tempfile
import unittest
from pathlib import Path

from gradient_ascent.coach_notes import add_coach_note


class CoachNotesTest(unittest.TestCase):
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
