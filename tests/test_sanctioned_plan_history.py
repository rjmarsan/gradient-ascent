import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gradient_ascent.storage import write_json


def setup(root):
    root.mkdir(mode=0o700)
    write_json(root / "plan/athlete.json", {"ftp_w": 250})
    write_json(root / "plan/events.json", [])
    write_json(
        root / "plan/weeks.json",
        [{"start_date": "2026-09-01", "end_date": "2026-09-07", "days": {}}],
    )
    write_json(root / "plan/workouts.json", {"version": 1, "workouts": []})
    (root / "plan/goals.md").write_text("# Replace with athlete goals\n")


def metadata(key):
    return {
        "idempotency_key": key,
        "title": "Synthetic change",
        "rationale": "Synthetic authorized update.",
    }


class SanctionedPlanHistoryTest(unittest.TestCase):
    def test_budget_changes_and_noop_preserve_revisions_and_source(self):
        from gradient_ascent.coaching_history import plan_history
        from gradient_ascent.tss_budgets import load_tss_budgets, update_tss_budgets

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            setup(root)
            source = (root / "plan/weeks.json").read_bytes()
            draft = Path(tmp) / "draft.json"
            entry = {
                "start_date": "2026-09-01",
                "end_date": "2026-09-07",
                "target_tss": 75.25,
                "rationale": "Synthetic precise budget.",
            }
            write_json(draft, {"version": 1, "budgets": [entry]})
            result = update_tss_budgets(root, draft, history_request=metadata("budget-first"))
            self.assertEqual(result["created"], 1)
            self.assertEqual(len(plan_history(root)), 1)
            retry = update_tss_budgets(root, draft, history_request=metadata("budget-first"))
            self.assertEqual(retry["history"]["id"], result["history"]["id"])
            self.assertFalse(retry["history"]["created"])
            with self.assertRaisesRegex(ValueError, "different content"):
                update_tss_budgets(
                    root,
                    draft,
                    history_request={
                        **metadata("budget-first"),
                        "rationale": "Different decision.",
                    },
                )
            self.assertEqual(update_tss_budgets(root, draft)["unchanged"], 1)
            self.assertEqual(len(plan_history(root)), 1)
            write_json(draft, {"version": 1, "budgets": [{**entry, "target_tss": 80.25}]})
            update_tss_budgets(root, draft, history_request=metadata("budget-second"))
            self.assertEqual(len(plan_history(root)), 2)
            stored = next(iter(load_tss_budgets(root).values()))
            self.assertEqual((stored["target_tss"], stored["revision"]), (80.25, 2))
            self.assertEqual((root / "plan/weeks.json").read_bytes(), source)

    def test_profile_goals_and_event_are_logged_but_setup_choice_is_not(self):
        from gradient_ascent.coaching_history import plan_history
        from gradient_ascent.onboarding import (
            add_onboarding_event,
            set_onboarding_choice,
            set_onboarding_goals,
            set_onboarding_profile,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            setup(root)
            initial = set_onboarding_profile(
                root, timezone="Europe/Paris", history_request=metadata("profile")
            )
            retry = set_onboarding_profile(
                root, timezone="Europe/Paris", history_request=metadata("profile")
            )
            self.assertEqual(retry["history"]["id"], initial["history"]["id"])
            with self.assertRaisesRegex(ValueError, "different content"):
                set_onboarding_profile(
                    root, timezone="Europe/London", history_request=metadata("profile")
                )
            set_onboarding_profile(root, timezone="Europe/Paris")
            self.assertEqual(len(plan_history(root)), 1)
            self.assertEqual(json.loads((root / "plan/athlete.json").read_text())["ftp_w"], 250)
            goals = dict(
                north_star="Consistency",
                goal="Ride regularly",
                why="Enjoy cycling",
                success="Regular safe rides",
                coaching_implication="Prefer sustainable work",
                evidence="Local completed rides",
            )
            initial = set_onboarding_goals(root, **goals, history_request=metadata("goals"))
            self.assertEqual(
                set_onboarding_goals(root, **goals, history_request=metadata("goals"))["history"][
                    "id"
                ],
                initial["history"]["id"],
            )
            set_onboarding_goals(root, **goals)
            self.assertEqual(len(plan_history(root)), 2)
            with self.assertRaisesRegex(ValueError, "already configured"):
                set_onboarding_goals(root, **{**goals, "goal": "Different goal"})
            event = dict(
                name="Synthetic ride", event_date="2026-09-03", discipline="road", priority="B"
            )
            initial = add_onboarding_event(root, **event, history_request=metadata("event"))
            self.assertEqual(
                add_onboarding_event(root, **event, history_request=metadata("event"))["history"][
                    "id"
                ],
                initial["history"]["id"],
            )
            add_onboarding_event(root, **event)
            self.assertEqual(len(plan_history(root)), 3)
            set_onboarding_choice(root, "plan", "none")
            self.assertEqual(len(plan_history(root)), 3)
            self.assertEqual(
                [list(item["files"]) for item in plan_history(root)],
                [["plan/athlete.json"], ["plan/goals.md"], ["plan/events.json"]],
            )

    def test_onboarding_unsupported_empty_history_fallback_and_existing_history_block(self):
        from gradient_ascent import coaching_history, recording_repair
        from gradient_ascent.onboarding import set_onboarding_profile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            setup(root)
            with mock.patch.object(recording_repair, "_secure_files_supported", return_value=False):
                result = set_onboarding_profile(root, timezone="Europe/Paris")
                self.assertEqual(result["history_status"], "unavailable")
            self.assertEqual(coaching_history.plan_history(root), [])
            set_onboarding_profile(root, unit_system="metric")
            before = (root / "plan/athlete.json").read_bytes()
            with mock.patch.object(recording_repair, "_secure_files_supported", return_value=False):
                with self.assertRaisesRegex(RuntimeError, "existing coaching history"):
                    set_onboarding_profile(root, unit_system="imperial")
            self.assertEqual((root / "plan/athlete.json").read_bytes(), before)
