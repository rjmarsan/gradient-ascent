from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gradient_ascent import coaching_history
from gradient_ascent.cli import _install_goal_files
from gradient_ascent.calendar import ingest_calendar
from gradient_ascent.plan import build_plan_from_csv
from gradient_ascent.storage import write_json


def source(path: Path, *, phase: str = "Base", workout: str = "Easy ride") -> Path:
    path.write_text(
        f"Week,Phase,Mon\n2026-09-07 – 2026-09-13,{phase},{workout}\n", encoding="utf-8"
    )
    return path


class PlanWriterHistoryTest(unittest.TestCase):
    def test_goal_file_changes_retain_reason_and_retry_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            root.mkdir()
            metadata = {
                "idempotency_key": "goals-review",
                "rationale": "Revised the goal after review.",
            }
            first = _install_goal_files(
                root, goals="# New goal\n", measurement=None, history_request=metadata
            )
            second = _install_goal_files(
                root, goals="# New goal\n", measurement=None, history_request=metadata
            )
            self.assertEqual(first["history"]["id"], second["history"]["id"])
            detail = coaching_history.plan_change_details(root, first["history"]["id"])
            self.assertEqual(detail["request"]["rationale"], metadata["rationale"])
            self.assertEqual(detail["files"]["plan/goals.md"]["after_content"], "# New goal\n")

    def test_explicit_change_key_replays_csv_and_calendar_imports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            root.mkdir()
            csv = source(Path(tmp) / "source.csv")
            metadata = {
                "idempotency_key": "reviewed-import",
                "rationale": "An approved source revision.",
            }
            first = build_plan_from_csv(csv, root / "plan", history_request=metadata)
            second = build_plan_from_csv(csv, root / "plan", history_request=metadata)
            self.assertEqual(second["history"]["id"], first["history"]["id"])
            self.assertFalse(second["history"]["created"])
            metadata = {**metadata, "idempotency_key": "reviewed-calendar"}
            first = ingest_calendar(csv, root / "calendar.json", history_request=metadata)
            second = ingest_calendar(csv, root / "calendar.json", history_request=metadata)
            self.assertEqual(second["history"]["id"], first["history"]["id"])

    def test_plan_import_cannot_introduce_a_structured_calendar_id_collision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            root.mkdir()
            csv = source(Path(tmp) / "source.csv", workout="")
            build_plan_from_csv(csv, root / "plan")
            write_json(
                root / "plan/workouts.json",
                {
                    "version": 1,
                    "workouts": [
                        {
                            "id": "week-2026-09-07-mon",
                            "date": "2026-09-07",
                            "name": "Independent",
                            "sport": "cycling",
                            "steps": [
                                {
                                    "name": "Easy",
                                    "duration_s": 60,
                                    "intensity": "active",
                                    "target": {"type": "open"},
                                }
                            ],
                        }
                    ],
                },
            )
            before = (root / "plan/weeks.json").read_bytes()
            source(csv, workout="New prose")
            with self.assertRaisesRegex(ValueError, "source"):
                build_plan_from_csv(csv, root / "plan")
            self.assertEqual((root / "plan/weeks.json").read_bytes(), before)

    def test_canonical_plan_import_tracks_all_five_files_and_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            root.mkdir()
            csv = source(Path(tmp) / "source.csv")
            first = build_plan_from_csv(csv, root / "plan")
            self.assertEqual(first["history"]["status"], "applied")
            transaction = coaching_history.plan_change_details(root, first["history"]["id"])
            self.assertEqual(
                set(transaction["files"]),
                {
                    f"plan/{name}.json"
                    for name in ("athlete", "events", "weeks", "phases", "legend")
                },
            )
            self.assertIsNone(transaction["files"]["plan/weeks.json"]["before_content"])
            self.assertIn("Easy ride", transaction["files"]["plan/weeks.json"]["after_content"])
            again = build_plan_from_csv(csv, root / "plan")
            self.assertEqual(again["history"]["status"], "unchanged")
            self.assertEqual(len(coaching_history.plan_history(root)), 1)

    def test_interrupted_import_keeps_complete_intent_for_explicit_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            root.mkdir()
            csv = source(Path(tmp) / "source.csv")
            build_plan_from_csv(csv, root / "plan")
            before = {path.name: path.read_bytes() for path in (root / "plan").glob("*.json")}
            source(csv, phase="Recovery", workout="Rest")
            write_target = coaching_history._write_target

            def fail_last(descriptor, name, body):
                if name == "plan/weeks.json":
                    raise OSError("synthetic interruption")
                return write_target(descriptor, name, body)

            with patch.object(coaching_history, "_write_target", side_effect=fail_last):
                with self.assertRaises(RuntimeError):
                    build_plan_from_csv(csv, root / "plan")
            transaction = coaching_history.plan_history(root)[-1]
            self.assertEqual(transaction["status"], "recovery_required")
            self.assertEqual(len(transaction["files"]), 5)
            restored = coaching_history.recover_plan_change(
                root, transaction["id"], action="restore"
            )
            self.assertEqual(restored["status"], "restored")
            self.assertEqual(
                {path.name: path.read_bytes() for path in (root / "plan").glob("*.json")}, before
            )

    def test_calendar_import_is_official_but_external_output_is_only_an_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            root.mkdir()
            csv = source(Path(tmp) / "source.csv")
            result = ingest_calendar(csv, root / "calendar.json")
            self.assertEqual(result["history"]["status"], "applied")
            artifact = Path(tmp) / "artifact"
            build_plan_from_csv(csv, artifact, record_history=False)
            ingest_calendar(csv, artifact / "calendar.json", record_history=False)
            self.assertFalse((artifact / "plan/.history").exists())
            self.assertFalse((Path(tmp) / "plan/.history").exists())
            self.assertEqual(len(coaching_history.plan_history(root)), 1)


if __name__ == "__main__":
    unittest.main()
