import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gradient_ascent.storage import write_json


def entry(key, start, end=None, *, kind="observation", scope="day"):
    return {
        "kind": kind,
        "idempotency_key": key,
        "title": "Synthetic context",
        "body": "Compact synthesis",
        "rationale": "Useful later",
        "scopes": [{"kind": scope, "start_date": start, "end_date": end or start}],
    }


class CoachingContextTest(unittest.TestCase):
    def test_manual_plan_drift_is_read_only_not_an_invented_change(self):
        from gradient_ascent import coaching_history as history
        from gradient_ascent.coaching_context import build_coaching_context

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_json(root / "plan/weeks.json", [])
            history.initialize_plan_history(root)
            journal = root / "plan/.history/journal.json"
            before = journal.read_bytes()
            write_json(root / "plan/weeks.json", [{"synthetic": "manual edit"}])
            with mock.patch.object(
                history, "_snapshot_body", side_effect=AssertionError("No snapshots")
            ):
                result = build_coaching_context(root)
            self.assertEqual(result["history"]["drift"]["drifted_files"], ["plan/weeks.json"])
            self.assertEqual(result["history"]["drift"]["drifted_count"], 1)
            self.assertEqual(result["plan_changes"], [])
            self.assertEqual(journal.read_bytes(), before)

    def test_recall_combines_only_relevant_bounded_compact_sources(self):
        from gradient_ascent import coaching_history as history
        from gradient_ascent.coaching_context import build_coaching_context

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_json(root / "plan/weeks.json", [])
            baseline = history.initialize_plan_history(root)
            write_json(
                root / "plan/coach_notes.json",
                {
                    "version": 1,
                    "notes": [
                        {"date": "2026-04-08", "note": "Old coach note"},
                        {"date": "2026-05-01", "note": "Outside range"},
                    ],
                },
            )
            write_json(
                root / "plan/daily_notes.json",
                {
                    "version": 1,
                    "notes": {
                        "2026-04-08": {
                            "date": "2026-04-08",
                            "note": "Rider day note",
                            "updated_at": "2026-04-08T12:00:00Z",
                        }
                    },
                },
            )
            history.capture_coaching_entry(
                root, entry("week", "2026-04-06", "2026-04-12", scope="week")
            )
            decision = history.capture_coaching_entry(
                root, entry("decision", "2026-04-08", kind="decision")
            )
            history.capture_coaching_entry(root, entry("outside", "2026-05-01"))
            changed = history.apply_plan_change(
                root,
                updates={"plan/goals.md": b"Private snapshot must not expand"},
                request={
                    "idempotency_key": "change",
                    "title": "Agreed change",
                    "rationale": "Why",
                    "scopes": [
                        {"kind": "day", "start_date": "2026-04-08", "end_date": "2026-04-08"}
                    ],
                    "thread_id": "synthetic-thread",
                    "decision_id": decision["id"],
                },
            )
            with mock.patch.object(
                history, "_snapshot_body", side_effect=AssertionError("No snapshot bodies")
            ):
                result = build_coaching_context(
                    root, start="2026-04-08", end="2026-04-08", limit=10
                )
            self.assertEqual(len(result["entries"]), 2)
            self.assertEqual(len(result["legacy_notes"]), 2)
            self.assertEqual([item["id"] for item in result["plan_changes"]], [changed["id"]])
            change = result["plan_changes"][0]
            self.assertEqual(change["codex_url"], "codex://threads/synthetic-thread")
            self.assertEqual(change["decision_revision"], 1)
            self.assertEqual(change["files"], ["plan/goals.md"])
            self.assertNotIn("Private snapshot", json.dumps(result))
            self.assertNotIn("before_content", json.dumps(result))
            self.assertFalse(result["external_access"])
            self.assertTrue(result["history"]["available"])
            self.assertEqual(result["history"]["baseline_id"], baseline["id"])
            self.assertTrue(result["history"]["baseline_created_at"])
            limited = build_coaching_context(root, start="2026-04-08", end="2026-04-08", limit=1)
            self.assertTrue(limited["summary"]["truncated"])
            self.assertEqual(limited["summary"]["truncated_entries"], 1)
            self.assertEqual(limited["summary"]["matching_plan_changes"], 1)
            self.assertLessEqual(len(limited["entries"]), 1)
            self.assertLessEqual(len(limited["legacy_notes"]), 1)
            only_decisions = build_coaching_context(root, kind="decision")
            self.assertEqual([item["kind"] for item in only_decisions["entries"]], ["decision"])
            self.assertEqual(only_decisions["legacy_notes"], [])

    def test_invalid_options_and_malformed_legacy_fail_without_mutation(self):
        from gradient_ascent.coaching_context import build_coaching_context

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for options in (
                {"limit": True},
                {"limit": 0},
                {"limit": 1001},
                {"start": "bad"},
                {"start": "2026-04-09", "end": "2026-04-08"},
                {"kind": "applied"},
            ):
                with self.subTest(options=options), self.assertRaises(ValueError):
                    build_coaching_context(root, **options)
            path = root / "plan/daily_notes.json"
            write_json(path, {"version": 1, "notes": []})
            before = path.read_bytes()
            with self.assertRaises(ValueError):
                build_coaching_context(root)
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse((root / "plan/.history").exists())


if __name__ == "__main__":
    unittest.main()
