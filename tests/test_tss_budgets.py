import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from gradient_ascent.storage import write_json


WEEK = {
    "start_date": "2026-09-01",
    "end_date": "2026-09-07",
    "days": {"Tue": "Prescribed ride"},
    "hours_target": {"min": 5, "max": 7},
    "tss_target": {"min": None, "max": None},
}
SECOND = {**WEEK, "start_date": "2026-09-08", "end_date": "2026-09-14"}


def draft_entry(week=WEEK, **changes):
    return {
        "start_date": week["start_date"],
        "end_date": week["end_date"],
        "target_tss": 300,
        "rationale": "Synthetic coaching decision.",
        **changes,
    }


def setup(root):
    root.mkdir(mode=0o700)
    write_json(root / "plan/weeks.json", [WEEK, SECOND])
    write_json(root / "plan/events.json", [])
    write_json(root / "plan/workouts.json", {"version": 1, "workouts": []})
    write_json(root / "plan/athlete.json", {"ftp_w": 250, "constraints": []})
    (root / "plan/goals.md").write_text("Synthetic goal\n")


def write_draft(path, entries):
    write_json(path, {"version": 1, "budgets": entries})
    return path


def allocation(week=WEEK, values=(0, 75, 50, 0, 75, 100, 0)):
    first = date.fromisoformat(week["start_date"])
    return [
        {"date": (first + timedelta(days=index)).isoformat(), "target_tss": value}
        for index, value in enumerate(values)
    ]


def workout(identifier, day, *, watts=250, unit="watts"):
    return {
        "id": identifier,
        "date": day,
        "name": "Synthetic prescribed session",
        "sport": "cycling",
        "steps": [
            {
                "name": "Work",
                "duration_s": 1800,
                "intensity": "active",
                "target": {"type": "power", "unit": unit, "low": watts, "high": watts},
            }
        ],
    }


class TSSBudgetsTest(unittest.TestCase):
    def test_daily_allocation_revision_preservation_clear_and_provenance(self):
        from gradient_ascent.tss_budgets import (
            coach_daily_tss,
            load_tss_budgets,
            update_tss_budgets,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            setup(root)
            draft = Path(tmp) / "draft.json"
            key = (WEEK["start_date"], WEEK["end_date"])
            days = allocation()
            days[0]["rationale"] = "Explicit rest or recovery branch."
            update_tss_budgets(
                root, write_draft(draft, [draft_entry(daily_tss=list(reversed(days)))])
            )
            loaded = load_tss_budgets(root)
            self.assertEqual(loaded[key]["daily_tss"], days)
            flattened = coach_daily_tss(loaded)
            self.assertEqual(len(flattened), 7)
            first = flattened[WEEK["start_date"]]
            self.assertEqual(
                (first["target_tss"], first["tss_source"], first["status"]),
                (0, "coach_budget_allocation", "provisional"),
            )
            self.assertEqual(first["rationale"], days[0]["rationale"])
            self.assertEqual(first["budget_revision"], 1)
            self.assertFalse(first["override_daily_source"])
            original = (root / "plan/tss_budgets.json").read_bytes()
            self.assertEqual(
                update_tss_budgets(root, write_draft(draft, [draft_entry()]))["unchanged"], 1
            )
            self.assertEqual((root / "plan/tss_budgets.json").read_bytes(), original)
            with self.assertRaisesRegex(ValueError, "daily_tss"):
                update_tss_budgets(root, write_draft(draft, [draft_entry(target_tss=310)]))
            self.assertEqual((root / "plan/tss_budgets.json").read_bytes(), original)
            update_tss_budgets(
                root, write_draft(draft, [draft_entry(target_tss=310, daily_tss=None)])
            )
            cleared = load_tss_budgets(root)[key]
            self.assertNotIn("daily_tss", cleared)
            self.assertEqual(cleared["revision"], 2)
            self.assertEqual(coach_daily_tss(load_tss_budgets(root)), {})

    def test_daily_allocation_requires_complete_exact_dates_and_central_sum(self):
        from gradient_ascent.tss_budgets import update_tss_budgets

        days = allocation()
        invalid = [
            [],
            days[:-1],
            days + [days[-1]],
            [days[0], days[0], *days[2:]],
            [{**days[0], "date": "2026-08-31"}, *days[1:]],
        ]
        invalid += [
            [{**days[0], "target_tss": value}, *days[1:]]
            for value in (True, -1, float("nan"), float("inf"), 10**400, 21601, 1)
        ]
        invalid += [
            [{**days[0], "unknown": 1}, *days[1:]],
            [{**days[0], "rationale": ""}, *days[1:]],
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            setup(root)
            draft = Path(tmp) / "draft.json"
            for value in invalid:
                with self.subTest(value_type=type(value).__name__):
                    with self.assertRaises(ValueError):
                        update_tss_budgets(root, write_draft(draft, [draft_entry(daily_tss=value)]))
                    self.assertFalse((root / "plan/tss_budgets.json").exists())
            with self.assertRaises(ValueError):
                update_tss_budgets(root, write_draft(draft, [draft_entry(override_daily_source=1)]))
            short = {**WEEK, "end_date": "2026-09-02"}
            write_json(root / "plan/weeks.json", [short])
            update_tss_budgets(
                root,
                write_draft(
                    draft,
                    [draft_entry(short, target_tss=0.3, daily_tss=allocation(short, (0.1, 0.2)))],
                ),
            )

    def test_daily_source_conflict_has_separate_explicit_override(self):
        from gradient_ascent.tss_budgets import (
            coach_daily_tss,
            load_tss_budgets,
            update_tss_budgets,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            setup(root)
            write_json(
                root / "plan/weeks.json",
                [{**WEEK, "day_loads": {"Tue": {"tss_min": 20, "tss_max": 40}}}, SECOND],
            )
            draft = Path(tmp) / "draft.json"
            for weekly_override in (False, True):
                with self.assertRaisesRegex(ValueError, "override_daily_source"):
                    update_tss_budgets(
                        root,
                        write_draft(
                            draft,
                            [draft_entry(daily_tss=allocation(), override_source=weekly_override)],
                        ),
                    )
            update_tss_budgets(
                root,
                write_draft(
                    draft, [draft_entry(daily_tss=allocation(), override_daily_source=True)]
                ),
            )
            self.assertTrue(
                coach_daily_tss(load_tss_budgets(root))[WEEK["start_date"]]["override_daily_source"]
            )

    def test_primary_structured_targets_are_checked_without_prose_doublecount(self):
        from gradient_ascent.tss_budgets import update_tss_budgets

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            setup(root)
            draft = Path(tmp) / "draft.json"
            write_json(root / "plan/weeks.json", [{**WEEK, "days": {}}, SECOND])
            write_json(
                root / "plan/workouts.json",
                {
                    "version": 1,
                    "workouts": [
                        workout("first", WEEK["start_date"]),
                        workout("second", WEEK["start_date"]),
                    ],
                },
            )
            with self.assertRaisesRegex(ValueError, "override_daily_source"):
                update_tss_budgets(root, write_draft(draft, [draft_entry(daily_tss=allocation())]))
            update_tss_budgets(
                root,
                write_draft(
                    draft, [draft_entry(daily_tss=allocation(values=(100, 0, 50, 0, 50, 100, 0)))]
                ),
            )
            write_json(root / "plan/tss_budgets.json", {"version": 1, "budgets": []})
            write_json(root / "plan/weeks.json", [WEEK, SECOND])
            update_tss_budgets(root, write_draft(draft, [draft_entry(daily_tss=allocation())]))

    def test_explicit_prose_and_rest_use_the_dashboard_prescription_rules(self):
        from gradient_ascent.tss_budgets import update_tss_budgets

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            setup(root)
            draft = Path(tmp) / "draft.json"
            days = allocation(values=(100, 0, 50, 0, 50, 100, 0))
            for text in ("Rest", "Ride 75 TSS"):
                with self.subTest(text=text):
                    write_json(root / "plan/tss_budgets.json", {"version": 1, "budgets": []})
                    before = (root / "plan/tss_budgets.json").read_bytes()
                    write_json(root / "plan/weeks.json", [{**WEEK, "days": {"Tue": text}}, SECOND])
                    with self.assertRaisesRegex(ValueError, "override_daily_source"):
                        update_tss_budgets(root, write_draft(draft, [draft_entry(daily_tss=days)]))
                    self.assertEqual((root / "plan/tss_budgets.json").read_bytes(), before)
            write_json(root / "plan/tss_budgets.json", {"version": 1, "budgets": []})
            update_tss_budgets(
                root, write_draft(draft, [draft_entry(daily_tss=days, override_daily_source=True)])
            )
            write_json(root / "plan/tss_budgets.json", {"version": 1, "budgets": []})
            for text in ("Cancelled ride 75 TSS", "Endurance ride 2 h"):
                with self.subTest(text=text):
                    write_json(root / "plan/weeks.json", [{**WEEK, "days": {"Tue": text}}, SECOND])
                    update_tss_budgets(root, write_draft(draft, [draft_entry(daily_tss=days)]))
                    write_json(root / "plan/tss_budgets.json", {"version": 1, "budgets": []})

    def test_exact_fractional_source_targets_need_no_daily_override(self):
        from gradient_ascent.tss_budgets import update_tss_budgets

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            setup(root)
            draft = Path(tmp) / "draft.json"
            days = allocation(values=(75.25, 0, 0, 0, 0, 0, 0))
            for source in (
                {"days": {"Tue": "Ride 75.25 TSS"}},
                {"day_loads": {"Tue": {"tss_min": 75.25, "tss_max": 75.25}}},
            ):
                with self.subTest(source=list(source)):
                    write_json(root / "plan/tss_budgets.json", {"version": 1, "budgets": []})
                    write_json(
                        root / "plan/weeks.json",
                        [{**WEEK, **source, "tss_target": {"min": 75.25, "max": 75.25}}, SECOND],
                    )
                    result = update_tss_budgets(
                        root, write_draft(draft, [draft_entry(target_tss=75.25, daily_tss=days)])
                    )
                    self.assertEqual(result["created"], 1)

    def test_stale_and_orphaned_allocations_are_never_returned(self):
        from gradient_ascent.tss_budgets import (
            coach_daily_tss,
            load_tss_budgets,
            update_tss_budgets,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            setup(root)
            update_tss_budgets(
                root, write_draft(Path(tmp) / "draft.json", [draft_entry(daily_tss=allocation())])
            )
            write_json(root / "plan/weeks.json", [{**WEEK, "notes": "Changed"}, SECOND])
            self.assertEqual(coach_daily_tss(load_tss_budgets(root)), {})
            write_json(root / "plan/weeks.json", [SECOND])
            self.assertEqual(coach_daily_tss(load_tss_budgets(root)), {})

    def test_author_merge_revision_replace_and_private_storage(self):
        from gradient_ascent.tss_budgets import (
            load_tss_budgets,
            update_tss_budgets,
            tss_budget_summary,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            setup(root)
            draft = Path(tmp) / "draft.json"
            before = (root / "plan/weeks.json").read_bytes()
            result = update_tss_budgets(root, write_draft(draft, [draft_entry()]))
            self.assertEqual((result["created"], result["current"]), (1, 1))
            path = root / "plan/tss_budgets.json"
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            entry = load_tss_budgets(root)[(WEEK["start_date"], WEEK["end_date"])]
            self.assertEqual(entry["range"], {"min": 300, "max": 300})
            self.assertEqual(
                (entry["revision"], entry["status"], entry["state"]), (1, "provisional", "current")
            )
            self.assertEqual(len(entry["plan_fingerprint"]), 64)
            original = path.read_bytes()
            self.assertEqual(update_tss_budgets(root, draft)["unchanged"], 1)
            self.assertEqual(path.read_bytes(), original)
            update_tss_budgets(root, write_draft(draft, [draft_entry(SECOND, target_tss=0)]))
            self.assertEqual(tss_budget_summary(root)["total"], 2)
            result = update_tss_budgets(
                root, write_draft(draft, [draft_entry(target_tss=320, status="confirmed")])
            )
            self.assertEqual(result["updated"], 1)
            self.assertEqual(
                load_tss_budgets(root)[(WEEK["start_date"], WEEK["end_date"])]["revision"], 2
            )
            result = update_tss_budgets(root, write_draft(draft, []), replace=True)
            self.assertEqual((result["removed"], result["total"]), (2, 0))
            self.assertEqual((root / "plan/weeks.json").read_bytes(), before)

    def test_fingerprint_staleness_reapproval_and_orphaning(self):
        from gradient_ascent.tss_budgets import (
            load_tss_budgets,
            update_tss_budgets,
            plan_tss_budget_fingerprints,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            setup(root)
            draft = Path(tmp) / "draft.json"
            key = (WEEK["start_date"], WEEK["end_date"])
            old = plan_tss_budget_fingerprints(root)[key]
            update_tss_budgets(
                root, write_draft(draft, [draft_entry(expected_plan_fingerprint=old)])
            )
            write_json(
                root / "plan/weeks.json", [{**WEEK, "hours_actual_text": "ignored actual"}, SECOND]
            )
            self.assertEqual(load_tss_budgets(root)[key]["state"], "current")
            write_json(
                root / "plan/weeks.json",
                [{**WEEK, "days": {"Tue": "Changed prescription"}}, SECOND],
            )
            self.assertEqual(load_tss_budgets(root)[key]["state"], "needs_review")
            with self.assertRaisesRegex(ValueError, "fingerprint"):
                update_tss_budgets(root, draft)
            with self.assertRaisesRegex(ValueError, "fingerprint"):
                update_tss_budgets(root, write_draft(draft, [draft_entry()]))
            current = plan_tss_budget_fingerprints(root)[key]
            update_tss_budgets(
                root, write_draft(draft, [draft_entry(expected_plan_fingerprint=current)])
            )
            self.assertEqual(load_tss_budgets(root)[key]["state"], "current")
            write_json(root / "plan/weeks.json", [SECOND])
            self.assertEqual(load_tss_budgets(root)[key]["state"], "orphaned")

    def test_source_conflict_needs_explicit_override_and_preserves_source(self):
        from gradient_ascent.tss_budgets import update_tss_budgets

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            setup(root)
            source = [{**WEEK, "tss_target": {"min": 400, "max": 450}}]
            write_json(root / "plan/weeks.json", source)
            before = (root / "plan/weeks.json").read_bytes()
            draft = write_draft(Path(tmp) / "draft.json", [draft_entry()])
            with self.assertRaisesRegex(ValueError, "override_source"):
                update_tss_budgets(root, draft)
            result = update_tss_budgets(
                root, write_draft(draft, [draft_entry(override_source=True)])
            )
            self.assertEqual(result["created"], 1)
            self.assertEqual((root / "plan/weeks.json").read_bytes(), before)

    def test_invalid_schema_ranges_duplicates_and_dates_fail_without_write(self):
        from gradient_ascent.tss_budgets import update_tss_budgets

        changes = [
            {"target_tss": True},
            {"target_tss": -1},
            {"target_tss": float("nan")},
            {"target_tss": 10**400},
            {"range": {"min": 301, "max": 320}},
            {"ceiling_tss": 299},
            {"rationale": ""},
            {"rationale": "\ud800"},
            {"conditions": "not a list"},
            {"override_source": 1},
            {"status": "approved"},
            {"unknown": 1},
            {"revision": 1},
            {"start_date": "2026-09-02"},
            {"expected_plan_fingerprint": "bad"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            setup(root)
            draft = Path(tmp) / "draft.json"
            for change in changes:
                with self.subTest(change=list(change)):
                    with self.assertRaises(ValueError):
                        update_tss_budgets(root, write_draft(draft, [draft_entry(**change)]))
                    self.assertFalse((root / "plan/tss_budgets.json").exists())
            with self.assertRaises(ValueError):
                update_tss_budgets(root, write_draft(draft, [draft_entry(), draft_entry()]))

    def test_empty_default_does_not_validate_unrelated_legacy_plan_or_need_posix(self):
        from gradient_ascent import tss_budgets

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_json(root / "plan/weeks.json", {"malformed": "legacy"})
            with mock.patch.object(tss_budgets, "_secure_files_supported", return_value=False):
                self.assertEqual(tss_budgets.load_tss_budgets(root), {})
                write_json(root / "plan/tss_budgets.json", {"version": 1, "budgets": []})
                self.assertEqual(tss_budgets.load_tss_budgets(root), {})

    def test_shared_secure_io_capability_is_consulted_dynamically(self):
        from gradient_ascent import recording_repair, tss_budgets

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            setup(root)
            write_json(root / "plan/tss_budgets.json", {"version": 1, "budgets": []})
            with mock.patch.object(recording_repair, "_secure_files_supported", return_value=False):
                self.assertEqual(tss_budgets.load_tss_budgets(root), {})
                with self.assertRaisesRegex(RuntimeError, "cannot safely"):
                    tss_budgets.plan_tss_budget_fingerprints(root)
            draft = write_draft(Path(tmp) / "draft.json", [draft_entry()])
            tss_budgets.update_tss_budgets(root, draft)
            with mock.patch.object(recording_repair, "_secure_files_supported", return_value=False):
                with self.assertRaisesRegex(RuntimeError, "nonempty"):
                    tss_budgets.load_tss_budgets(root)
                with self.assertRaisesRegex(RuntimeError, "cannot safely"):
                    tss_budgets.update_tss_budgets(root, draft)

    def test_symlink_budget_and_replaced_root_fail_closed(self):
        from gradient_ascent import tss_budgets
        from gradient_ascent.workspace_lock import workspace_identity

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            setup(root)
            draft = write_draft(Path(tmp) / "draft.json", [draft_entry()])
            destination = root / "plan/tss_budgets.json"
            destination.symlink_to(draft)
            with self.assertRaises((ValueError, OSError)):
                tss_budgets.update_tss_budgets(root, draft)
            destination.unlink()
            expected = workspace_identity(root)
            old = root.with_name("old")
            root.rename(old)
            setup(root)
            with self.assertRaisesRegex(RuntimeError, "generation changed"):
                tss_budgets.update_tss_budgets(root, draft, expected_identity=expected)
            self.assertFalse(destination.exists())

    def test_relevant_context_changes_stale_budget_but_other_dates_do_not(self):
        from gradient_ascent.tss_budgets import load_tss_budgets, update_tss_budgets

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            setup(root)
            key = (WEEK["start_date"], WEEK["end_date"])
            update_tss_budgets(root, write_draft(Path(tmp) / "draft.json", [draft_entry()]))
            write_json(root / "plan/events.json", [{"date": "2026-10-01", "name": "Unrelated"}])
            self.assertEqual(load_tss_budgets(root)[key]["state"], "current")
            write_json(root / "plan/events.json", [{"date": "2026-09-03", "name": "Changed event"}])
            self.assertEqual(load_tss_budgets(root)[key]["state"], "needs_review")
            write_json(root / "plan/events.json", [])
            write_json(root / "plan/athlete.json", {"ftp_w": 260, "constraints": []})
            self.assertEqual(load_tss_budgets(root)[key]["state"], "needs_review")
            write_json(root / "plan/athlete.json", {"ftp_w": 250, "constraints": []})
            (root / "plan/goals.md").write_text("Changed goal\n")
            self.assertEqual(load_tss_budgets(root)[key]["state"], "needs_review")

    def test_plan_change_during_validation_cannot_be_blessed_current(self):
        from gradient_ascent import tss_budgets

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            setup(root)
            draft = write_draft(Path(tmp) / "draft.json", [draft_entry()])
            fingerprint = tss_budgets._fingerprint
            changed = False

            def change_plan(week, context):
                nonlocal changed
                result = fingerprint(week, context)
                if not changed:
                    changed = True
                    write_json(
                        root / "plan/weeks.json",
                        [{**WEEK, "days": {"Tue": "Changed during validation"}}, SECOND],
                    )
                return result

            with mock.patch.object(tss_budgets, "_fingerprint", side_effect=change_plan):
                with self.assertRaisesRegex(ValueError, "fingerprint"):
                    tss_budgets.update_tss_budgets(root, draft)
            self.assertFalse((root / "plan/tss_budgets.json").exists())

    def test_workspace_replacement_during_validation_never_writes_replacement(self):
        from gradient_ascent import tss_budgets

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            setup(root)
            draft = write_draft(Path(tmp) / "draft.json", [draft_entry()])
            fingerprint = tss_budgets._fingerprint
            changed = False

            def replace_root(week, context):
                nonlocal changed
                result = fingerprint(week, context)
                if not changed:
                    changed = True
                    root.rename(root.with_name("old"))
                    root.mkdir(mode=0o700)
                    (root / "sentinel").write_text("replacement")
                return result

            with mock.patch.object(tss_budgets, "_fingerprint", side_effect=replace_root):
                with self.assertRaisesRegex(RuntimeError, "generation changed"):
                    tss_budgets.update_tss_budgets(root, draft)
            self.assertEqual([path.name for path in root.iterdir()], ["sentinel"])

    def test_check_to_write_race_still_targets_pinned_old_plan_directory(self):
        from gradient_ascent import coaching_history, tss_budgets

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            old = root.with_name("old")
            setup(root)
            draft = write_draft(Path(tmp) / "draft.json", [draft_entry()])
            write = coaching_history._write_target

            def replace_before_write(directory, name, body):
                root.rename(old)
                root.mkdir(mode=0o700)
                (root / "sentinel").write_text("replacement")
                return write(directory, name, body)

            with mock.patch.object(
                coaching_history, "_write_target", side_effect=replace_before_write
            ):
                with self.assertRaisesRegex(RuntimeError, "generation changed"):
                    tss_budgets.update_tss_budgets(root, draft)
            self.assertEqual([path.name for path in root.iterdir()], ["sentinel"])
            self.assertTrue((old / "plan/tss_budgets.json").is_file())
