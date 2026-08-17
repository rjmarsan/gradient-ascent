import tempfile
import unittest
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


class TSSBudgetsTest(unittest.TestCase):
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
        from gradient_ascent import tss_budgets

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            old = root.with_name("old")
            setup(root)
            draft = write_draft(Path(tmp) / "draft.json", [draft_entry()])
            write = tss_budgets._write

            def replace_before_write(directory, name, body, limit):
                root.rename(old)
                root.mkdir(mode=0o700)
                (root / "sentinel").write_text("replacement")
                return write(directory, name, body, limit)

            with mock.patch.object(tss_budgets, "_write", side_effect=replace_before_write):
                with self.assertRaisesRegex(RuntimeError, "generation changed"):
                    tss_budgets.update_tss_budgets(root, draft)
            self.assertEqual([path.name for path in root.iterdir()], ["sentinel"])
            self.assertTrue((old / "plan/tss_budgets.json").is_file())
