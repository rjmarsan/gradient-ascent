import copy
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from gradient_ascent import training_center
from gradient_ascent.cli import _init_workspace
from gradient_ascent.storage import write_json
from gradient_ascent.tss_budgets import update_tss_budgets


TODAY = date(2026, 8, 17)
DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


class DailyTssProjectionTest(unittest.TestCase):
    def workspace(self, root, *, days=None, source_target=None):
        _init_workspace(root, force=False)
        week = {
            "start_date": TODAY.isoformat(),
            "end_date": (TODAY + timedelta(days=6)).isoformat(),
            "phase": "Synthetic training block",
            "days": days
            or dict(zip(DAY_NAMES, ("OFF", "Ride", "OFF", "Ride", "Ride", "Ride", "Ride"))),
        }
        if source_target is not None:
            week["tss_target"] = {"min": source_target, "max": source_target}
        write_json(root / "plan/weeks.json", [week])
        write_json(
            root / "derived/weekly.json",
            [
                {
                    **week,
                    "plan": copy.deepcopy(week),
                    "totals": {"estimated_tss": 100, "activity_count": 1},
                }
            ],
        )
        write_json(
            root / "derived/daily.json",
            [{"date": TODAY.isoformat(), "totals": {"estimated_tss": 100, "activity_count": 1}}],
        )
        return week

    def author(self, root, *, values=None, target=210, override=False):
        entry = {
            "start_date": TODAY.isoformat(),
            "end_date": (TODAY + timedelta(days=6)).isoformat(),
            "target_tss": target,
            "status": "provisional",
            "rationale": "Synthetic full-week coaching decision.",
        }
        if values is not None:
            entry["daily_tss"] = [
                {
                    "date": (TODAY + timedelta(days=index)).isoformat(),
                    "target_tss": value,
                    "rationale": "Explicit full-day planning scenario.",
                }
                for index, value in enumerate(values)
            ]
            entry["override_daily_source"] = override
        draft = root.parent / "draft.json"
        write_json(draft, {"version": 1, "budgets": [entry]})
        update_tss_budgets(root, draft)

    def payload(self, root):
        with patch.object(training_center, "_athlete_today", return_value=TODAY):
            return training_center._build_payload(root)[0]

    def test_fractional_source_and_coach_targets_remain_exact_for_activation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "athlete"
            days = dict.fromkeys(DAY_NAMES, "OFF")
            days["Tue"] = "Ride 75.25 TSS"
            self.workspace(root, days=days, source_target=75.25)
            self.author(root, values=[0, 75.25, 0, 0, 0, 0, 0], target=75.25)
            payload = self.payload(root)
            self.assertEqual(
                payload["weeks"][0]["daily_tss_allocation"], {"state": "current", "days": 7}
            )
            self.assertEqual(payload["weeks"][0]["planned_load"]["estimated_tss"], 75.25)
            self.assertEqual(payload["days"][1]["coach_tss_target"]["target_tss"], 75.25)
            self.assertEqual(payload["trainingLoadProjection"]["rows"][0]["target_tss"], 75.25)

    def test_authored_daily_targets_drive_projection_and_cards_not_recorded_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "athlete"
            self.workspace(root)
            self.author(root, values=[0, 70, 0, 40, 20, 60, 20])
            names = (
                "plan/weeks.json",
                "plan/tss_budgets.json",
                "derived/daily.json",
                "derived/weekly.json",
            )
            before = {name: (root / name).read_bytes() for name in names}
            payload = self.payload(root)
            week = payload["weeks"][0]
            tuesday = payload["days"][1]
            self.assertEqual(week["planned_load"]["estimated_tss"], 210)
            self.assertEqual(week["daily_tss_allocation"], {"state": "current", "days": 7})
            self.assertEqual(tuesday["planned_load"]["estimated_tss"], 70)
            self.assertEqual(tuesday["planned_load"]["tss_source"], "coach_budget_allocation")
            self.assertEqual(tuesday["planned_load"]["qualifier"], "Coach day target · provisional")
            self.assertFalse(tuesday["planned_load"]["estimated"])
            self.assertIsNone(tuesday["planned_load"]["hours"])
            self.assertEqual(tuesday["source_planned_load"]["tss_source"], "missing")
            projection = payload["trainingLoadProjection"]
            self.assertEqual(
                [row["target_tss"] for row in projection["rows"]], [70, 0, 40, 20, 60, 20]
            )
            self.assertEqual(projection["summary"]["stop_reason"], "complete")
            self.assertTrue(all(row["status"] == "provisional" for row in projection["rows"]))
            anchor = payload["trainingLoad"]["rows"][-1]
            self.assertAlmostEqual(
                projection["rows"][0]["ctl"], anchor["ctl"] + (70 - anchor["ctl"]) / 42, places=6
            )
            self.assertEqual(payload["trainingLoad"]["rows"][-1]["tss_observed"], 100)
            for name, original in before.items():
                self.assertEqual((root / name).read_bytes(), original)

    def test_weekly_budget_and_rough_prose_are_not_daily_projection_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "athlete"
            self.workspace(
                root,
                days=dict(
                    zip(
                        DAY_NAMES,
                        ("OFF", "Ride 75 TSS", "OFF", "2 hours Z2", "Ride", "Ride", "Ride"),
                    )
                ),
            )
            self.author(root)
            payload = self.payload(root)
            projection = payload["trainingLoadProjection"]
            self.assertEqual([row["target_tss"] for row in projection["rows"]], [75, 0])
            self.assertEqual(
                [row["tss_source"] for row in projection["rows"]],
                ["source_target", "explicit_rest"],
            )
            self.assertEqual(projection["summary"]["stop_date"], "2026-08-20")
            self.assertEqual(projection["summary"]["stop_reason"], "missing_daily_target")
            self.assertEqual(payload["weeks"][0]["planned_load"]["estimated_tss"], 210)

    def test_stale_or_conflicting_allocations_do_not_get_spliced_into_a_week(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "athlete"
            week = self.workspace(root)
            self.author(root, values=[0, 70, 0, 40, 20, 60, 20])
            changed = copy.deepcopy(week)
            changed["days"]["Tue"] = "Ride 75 TSS"
            write_json(root / "derived/weekly.json", [{**changed, "plan": changed, "totals": {}}])
            conflicted = self.payload(root)
            self.assertEqual(conflicted["weeks"][0]["daily_tss_allocation"]["state"], "conflict")
            self.assertTrue(all(day.get("coach_tss_target") is None for day in conflicted["days"]))
            self.assertEqual(
                [row["target_tss"] for row in conflicted["trainingLoadProjection"]["rows"]], [75, 0]
            )
            self.author(root, values=[0, 70, 0, 40, 20, 60, 20], override=True)
            overridden = self.payload(root)
            self.assertEqual(overridden["days"][1]["planned_load"]["estimated_tss"], 70)
            self.assertEqual(overridden["days"][1]["source_planned_load"]["estimated_tss"], 75)
            self.assertEqual(
                overridden["days"][1]["planned_load"]["qualifier"],
                "Coach day override · provisional",
            )
            write_json(root / "plan/weeks.json", [changed])
            stale = self.payload(root)
            self.assertEqual(stale["weeks"][0]["daily_tss_allocation"]["state"], "needs_review")
            self.assertTrue(all(day.get("coach_tss_target") is None for day in stale["days"]))

    def test_matching_imported_weekly_target_keeps_its_source_precedence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "athlete"
            self.workspace(root, source_target=210)
            self.author(root, values=[0, 70, 0, 40, 20, 60, 20])
            payload = self.payload(root)
            self.assertEqual(payload["weeks"][0]["planned_load"]["tss_source"], "source_target")
            self.assertEqual(payload["trainingLoadProjection"]["summary"]["projected_days"], 6)

    def test_separate_structured_workouts_are_not_implicitly_added(self):
        source = {
            "date": "2026-08-18",
            "planned_load": {
                "estimated_tss": 75,
                "estimated_tss_min": 75,
                "estimated_tss_max": 75,
                "tss_source": "source_target",
            },
            "structured_workouts": [{"id": "independent"}],
            "structured_is_primary": False,
        }
        self.assertIsNone(training_center._day_projection_target(source))
        source["coach_tss_target"] = {
            "target_tss": 80,
            "status": "provisional",
            "tss_source": "coach_budget_allocation",
        }
        self.assertEqual(
            training_center._day_projection_target(source),
            {
                "date": "2026-08-18",
                "target_tss": 80,
                "tss_source": "coach_budget_allocation",
                "status": "provisional",
            },
        )


if __name__ == "__main__":
    unittest.main()
