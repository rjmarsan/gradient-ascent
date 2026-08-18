from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from gradient_ascent import plan_changes
from gradient_ascent.storage import write_json


WEEK = {
    "id": "2026-09-01",
    "start_date": "2026-09-01",
    "end_date": "2026-09-07",
    "phase": "Build",
    "days": {"Tue": "Old intervals", "Thu": "Long endurance", "Mon": "Rest"},
    "day_loads": {"Tue": {"hours_min": 2, "hours_max": 2, "tss_min": 100, "tss_max": 100}},
    "raw": {"Tue": "Old intervals", "Thu": "Long endurance", "Hours Target": "8-10h"},
    "actual": {"Tue": "Recorded effort"},
    "tss_target": {"min": 350, "max": 400},
}


def workout(identifier: str, day: str) -> dict:
    return {
        "id": identifier,
        "date": day,
        "name": "Synthetic prescribed ride",
        "sport": "cycling",
        "steps": [
            {
                "name": "Steady",
                "duration_s": 1800,
                "intensity": "active",
                "target": {"type": "power", "unit": "percent_ftp", "low": 65, "high": 70},
            }
        ],
    }


def fixture(root: Path) -> None:
    root.mkdir()
    write_json(root / "plan/weeks.json", [WEEK])
    write_json(
        root / "plan/workouts.json", {"version": 1, "workouts": [workout("keep", "2026-09-03")]}
    )
    write_json(root / "plan/tss_budgets.json", {"version": 1, "budgets": []})
    write_json(root / "derived/activities.json", [{"id": "recorded", "estimated_tss": 75.25}])


def draft(root: Path, path: Path, **updates: object) -> Path:
    value = {
        "version": 1,
        "change": {
            "idempotency_key": "synthetic-plan-change",
            "title": "Adjust the session",
            "rationale": "A reviewed recovery decision.",
        },
        "expected_files": plan_changes.plan_file_fingerprints(root),
        **updates,
    }
    write_json(path, value)
    return path


class PlanChangesTest(unittest.TestCase):
    def test_day_edit_records_exact_change_and_clears_stale_source_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            fixture(root)
            before_recorded = (root / "derived/activities.json").read_bytes()
            before_budgets = (root / "plan/tss_budgets.json").read_bytes()
            source = draft(
                root,
                Path(tmp) / "draft.json",
                days=[{"date": "2026-09-01", "workout": "Recovery spin"}],
            )
            result = plan_changes.update_plan_from_draft(root, source)
            changed = json.loads((root / "plan/weeks.json").read_text())[0]
            self.assertEqual(result["status"], "applied")
            self.assertEqual(changed["days"]["Tue"], "Recovery spin")
            self.assertEqual(changed["raw"]["Tue"], "Recovery spin")
            self.assertNotIn("Tue", changed["day_loads"])
            self.assertEqual(changed["actual"], WEEK["actual"])
            self.assertEqual(changed["tss_target"], WEEK["tss_target"])
            self.assertEqual((root / "derived/activities.json").read_bytes(), before_recorded)
            self.assertEqual((root / "plan/tss_budgets.json").read_bytes(), before_budgets)
            repeated = plan_changes.update_plan_from_draft(root, source)
            self.assertEqual(repeated["id"], result["id"])
            self.assertFalse(repeated["created"])

    def test_stale_or_ambiguous_day_edit_never_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            fixture(root)
            source = draft(
                root, Path(tmp) / "draft.json", days=[{"date": "2026-09-01", "workout": "New"}]
            )
            changed = copy.deepcopy(WEEK)
            changed["days"]["Tue"] = "Someone else's newer plan"
            write_json(root / "plan/weeks.json", [changed])
            before = (root / "plan/weeks.json").read_bytes()
            with self.assertRaises((ValueError, RuntimeError)):
                plan_changes.update_plan_from_draft(root, source)
            self.assertEqual((root / "plan/weeks.json").read_bytes(), before)
            write_json(root / "plan/weeks.json", [WEEK, WEEK])
            source = draft(root, source, days=[{"date": "2026-09-01", "workout": "New"}])
            before = (root / "plan/weeks.json").read_bytes()
            with self.assertRaisesRegex(ValueError, "exactly one"):
                plan_changes.update_plan_from_draft(root, source)
            self.assertEqual((root / "plan/weeks.json").read_bytes(), before)

    def test_explicit_zero_and_fractional_source_load_survive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            fixture(root)
            load = {"hours_min": 0, "hours_max": 1.5, "tss_min": 0, "tss_max": 75.25}
            source = draft(
                root,
                Path(tmp) / "draft.json",
                days=[{"date": "2026-09-07", "workout": "Optional easy ride", "load": load}],
            )
            plan_changes.update_plan_from_draft(root, source)
            self.assertEqual(
                json.loads((root / "plan/weeks.json").read_text())[0]["day_loads"]["Mon"], load
            )

    def test_independent_structured_workouts_use_existing_strict_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            fixture(root)
            original_weeks = (root / "plan/weeks.json").read_bytes()
            source = draft(
                root,
                Path(tmp) / "draft.json",
                workouts={"upsert": [workout("new", "2026-09-04")], "remove": ["keep"]},
            )
            plan_changes.update_plan_from_draft(root, source)
            document = json.loads((root / "plan/workouts.json").read_text())
            self.assertEqual([item["id"] for item in document["workouts"]], ["new"])
            self.assertEqual((root / "plan/weeks.json").read_bytes(), original_weeks)
            bad = workout("invalid", "2026-09-05")
            bad["steps"][0]["duration_s"] = -1
            source = draft(root, source, workouts={"upsert": [bad], "remove": []})
            before = (root / "plan/workouts.json").read_bytes()
            with self.assertRaises(ValueError):
                plan_changes.update_plan_from_draft(root, source)
            self.assertEqual((root / "plan/workouts.json").read_bytes(), before)

    def test_structured_delete_and_move_retries_keep_original_scopes(self) -> None:
        for edit in (
            {"remove": ["keep"]},
            {"upsert": [workout("keep", "2026-09-06")]},
        ):
            with self.subTest(edit=edit), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "workspace"
                fixture(root)
                source = draft(root, Path(tmp) / "draft.json", workouts=edit)
                first = plan_changes.update_plan_from_draft(root, source)
                second = plan_changes.update_plan_from_draft(root, source)
                self.assertEqual(second["id"], first["id"])
                self.assertFalse(second["created"])

    def test_editor_rejects_unknown_fields_and_missing_expected_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            fixture(root)
            source = draft(
                root,
                Path(tmp) / "draft.json",
                days=[{"date": "2026-09-01", "workout": "New", "ftp_w": 500}],
            )
            before = (root / "plan/weeks.json").read_bytes()
            with self.assertRaises(ValueError):
                plan_changes.update_plan_from_draft(root, source)
            document = json.loads(source.read_text())
            document["days"][0].pop("ftp_w")
            document["expected_files"].pop("plan/weeks.json")
            write_json(source, document)
            with self.assertRaisesRegex(ValueError, "expected"):
                plan_changes.update_plan_from_draft(root, source)
            self.assertEqual((root / "plan/weeks.json").read_bytes(), before)

    def test_editor_rejects_cross_source_workout_ids_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            fixture(root)
            source = draft(
                root,
                Path(tmp) / "draft.json",
                workouts={"upsert": [workout("week-2026-09-01-tue", "2026-09-01")]},
            )
            before = (root / "plan/workouts.json").read_bytes()
            with self.assertRaisesRegex(ValueError, "source"):
                plan_changes.update_plan_from_draft(root, source)
            self.assertEqual((root / "plan/workouts.json").read_bytes(), before)
            write_json(
                root / "plan/workouts.json",
                {"version": 1, "workouts": [workout("week-2026-09-01-wed", "2026-09-02")]},
            )
            source = draft(
                root, source, days=[{"date": "2026-09-02", "workout": "New prose prescription"}]
            )
            before = (root / "plan/weeks.json").read_bytes()
            with self.assertRaisesRegex(ValueError, "source"):
                plan_changes.update_plan_from_draft(root, source)
            self.assertEqual((root / "plan/weeks.json").read_bytes(), before)

    def test_unrelated_malformed_legacy_row_does_not_gate_structured_edit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            fixture(root)
            write_json(root / "plan/weeks.json", [WEEK, {}])
            write_json(root / "plan/events.json", [{}])
            source = draft(
                root, Path(tmp) / "draft.json", workouts={"upsert": [workout("new", "2026-09-06")]}
            )
            self.assertEqual(plan_changes.update_plan_from_draft(root, source)["status"], "applied")


if __name__ == "__main__":
    unittest.main()
