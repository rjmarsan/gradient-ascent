import json
from datetime import date
import tempfile
import unittest
from pathlib import Path

from gradient_ascent import training_center
from gradient_ascent.cli import _init_workspace
from gradient_ascent.insights import AggregateTotals


class DashboardLoadDisplayTest(unittest.TestCase):
    def test_source_plan_values_win_and_forecasts_remain_identified(self):
        source = training_center._planned_load_for_day(
            "2 hours Z2",
            [],
            source_load={"hours_min": 1.5, "hours_max": 1.5, "tss_min": 75.4, "tss_max": 75.4},
        )
        self.assertEqual(source["hours"], 1.5)
        self.assertEqual(source["estimated_tss"], 75.4)
        self.assertEqual(source["tss_source"], "source_target")
        self.assertEqual(source["tss_value_label"], "75 TSS")
        self.assertEqual(source["qualifier"], "Source target")
        forecast = training_center._planned_load_for_day("Z2 90–150min", [])
        self.assertEqual((forecast["hours_min"], forecast["hours_max"]), (1.5, 2.5))
        self.assertEqual(forecast["tss_source"], "session_if_forecast")
        self.assertIn("forecast", forecast["qualifier"].lower())
        self.assertIn("IF", forecast["note"])
        self.assertIsNone(
            training_center._planned_load_for_day("3x10min threshold", [])["estimated_tss"]
        )
        self.assertIsNone(
            training_center._planned_load_for_day("Run 90 minutes", [])["estimated_tss"]
        )
        self.assertEqual(training_center._planned_load_for_day("OFF", [])["estimated_tss"], 0)
        cancelled = training_center._planned_load_for_day(
            "Z2 90–150min easy; road race canceled", [{"name": "Road race", "status": "cancelled"}]
        )
        self.assertEqual(cancelled["intensity"], "endurance")

    def test_incomplete_imported_multi_session_duration_is_not_guessed_from_prose(self):
        load = training_center._planned_load_for_day(
            "Z2 ride (90 min)\nAnother ride",
            [],
            source_load={"hours_min": None, "hours_max": None, "tss_min": None, "tss_max": None},
        )
        self.assertIsNone(load["hours"])
        self.assertIsNone(load["estimated_tss"])

    def test_interval_recovery_is_not_the_whole_planned_session(self):
        for text in (
            "2x15-18min threshold at 240-260W; 6min easy; reduce to 2x12min if tired",
            "3x12min threshold; 5min easy between",
            "3x12min threshold; 5min easy with high cadence between efforts",
            "3x12min threshold; 5min easy ride between intervals",
            "10min warmup; 3x10min threshold; 5min recovery; 10min cooldown",
            "3x10min threshold with 5min recovery",
        ):
            with self.subTest(text=text):
                load = training_center._planned_load_for_day(text, [])
                self.assertIsNone(load["hours"])
                self.assertIsNone(load["estimated_tss"])
        for text, hours in (
            ("90min total including 3x10min threshold; 5min easy between", 1.5),
            ("90min controlled threshold ride; 3x10min at threshold; 5min recovery", 1.5),
            ("90min ride including 3x10min threshold with 5min recovery", 1.5),
            ("2h Z2 with 4x8s sprints", 2),
        ):
            with self.subTest(text=text):
                load = training_center._planned_load_for_day(text, [])
                self.assertEqual(load["hours"], hours)
                self.assertIsNotNone(load["estimated_tss"])
        explicit = training_center._planned_load_for_day(
            "3x10min threshold; 5min easy between",
            [],
            source_load={"hours_min": 1.5, "hours_max": 1.5, "tss_min": 90, "tss_max": 90},
        )
        self.assertEqual((explicit["hours"], explicit["estimated_tss"]), (1.5, 90))

    def test_cancellation_invalidates_an_ambiguous_imported_day_total(self):
        source = {"hours_min": 1.5, "hours_max": 1.5, "tss_min": 100, "tss_max": 100}
        active = training_center._planned_load_for_day(
            "Cancelled ride 60min; Ride 30min", [], source_load=source
        )
        self.assertEqual(active["hours"], 0.5)
        self.assertNotEqual(active["tss_source"], "source_target")
        cancelled = training_center._planned_load_for_day(
            "Cancelled ride 60min",
            [{"name": "Other event", "discipline": "Cycling"}],
            source_load=source,
        )
        self.assertIsNone(cancelled["hours"])
        self.assertIsNone(cancelled["estimated_tss"])

    def test_cancelled_negative_and_noncycling_prose_never_becomes_cycling_tss(self):
        cases = (
            ("Cancelled ride 90min 75TSS", []),
            ("Ride 90min -20TSS", []),
            ("90 minutes", [{"name": "Synthetic Running Race", "discipline": "Running"}]),
            ("Run 90 minutes easy", []),
            ("Bike and strength 120 minutes", []),
            ("Ride -90 minutes", []),
        )
        for text, events in cases:
            with self.subTest(text=text):
                load = training_center._planned_load_for_day(text, events)
                self.assertNotEqual(load["tss_source"], "source_target")
                if text != "Ride 90min -20TSS":
                    self.assertIsNone(load["estimated_tss"])
        cancelled_source = training_center._planned_load_for_day(
            "Canceled ride",
            [],
            source_load={"hours_min": 1, "hours_max": 1, "tss_min": 75, "tss_max": 75},
        )
        self.assertIsNone(cancelled_source["estimated_tss"])
        mixed = training_center._planned_load_for_day("Bike 60 minutes; strength 120 minutes", [])
        self.assertEqual(mixed["hours"], 1)
        self.assertEqual(mixed["tss_source"], "session_if_forecast")

    def test_week_status_never_falls_back_to_hours_or_rough_forecasts(self):
        row = {
            "start_date": "2026-08-10",
            "end_date": "2026-08-16",
            "status_meaningful": "within",
            "totals": {"estimated_tss": 1, "estimated_tss_missing_activity_count": 1},
        }
        days = [
            {
                "date": "2026-08-10",
                "planned_load": training_center._planned_load_for_day("2 hours Z2", []),
            }
        ]
        self.assertEqual(
            training_center._week_display_status(row, days, today=date(2026, 8, 10)),
            "budget_missing",
        )
        row["plan"] = {"tss_target": {"min": 100, "max": 150}}
        self.assertEqual(
            training_center._week_display_status(row, days, today=date(2026, 8, 17)),
            "load_incomplete",
        )

    def test_structured_sum_does_not_turn_an_invalid_upper_bound_into_an_exact_value(self):
        loads = [
            {
                "load": {
                    "hours_min": 10,
                    "hours_max": 14,
                    "estimated_tss_min": 100,
                    "estimated_tss_max": 200,
                    "estimated_tss": 150,
                }
            }
        ] * 2
        result = training_center._structured_day_load(loads)
        self.assertIsNone(result["hours"])
        self.assertEqual(result["estimated_tss"], 300)

    def test_week_hours_remain_visible_without_inventing_a_tss_budget(self):
        days = [{"planned_load": training_center._planned_load_for_day("3x10min threshold", [])}]
        result = training_center._planned_load_for_week(days, hours_target={"min": 8, "max": 10})
        self.assertEqual((result["hours_min"], result["hours_max"]), (8, 10))
        self.assertEqual(result["tss_source"], "missing")
        self.assertIsNone(result["estimated_tss"])
        self.assertEqual(result["known_tss_days"], 0)
        self.assertIsNone(days[0]["planned_load"]["hours"])
        self.assertEqual(result["qualifier"], "Budget not set")
        self.assertTrue(result["tss_value_label"].endswith(" TSS"))

    def test_coach_budget_range_ceiling_and_review_state_are_distinct(self):
        coach = {
            "state": "current",
            "target_tss": 350,
            "range": {"min": 330, "max": 370},
            "ceiling_tss": 400,
            "status": "provisional",
            "revision": 2,
            "conditions": ["Synthetic condition"],
            "rationale": "Synthetic coaching rationale.",
            "override_source": False,
        }
        load = training_center._planned_load_for_week(
            [], hours_target={"min": 8, "max": 10}, coach_budget=coach
        )
        self.assertEqual(
            (load["estimated_tss_min"], load["estimated_tss"], load["estimated_tss_max"]),
            (330, 350, 370),
        )
        self.assertEqual(load["budget_ceiling_label"], "400 TSS")
        self.assertEqual(load["qualifier"], "Coach budget · provisional")
        self.assertIn("Synthetic coaching rationale", load["note"])
        self.assertFalse(load["budget_review_required"])
        imported = training_center._planned_load_for_week(
            [], tss_target={"min": 450, "max": 450}, coach_budget=coach
        )
        self.assertEqual(imported["tss_source"], "source_target")
        override = training_center._planned_load_for_week(
            [], tss_target={"min": 450, "max": 450}, coach_budget={**coach, "override_source": True}
        )
        self.assertEqual(override["qualifier"], "Coach override · provisional")
        stale = training_center._planned_load_for_week(
            [], coach_budget={**coach, "state": "needs_review"}
        )
        self.assertIsNone(stale["estimated_tss"])
        self.assertEqual(stale["qualifier"], "Budget needs review")
        self.assertTrue(stale["budget_review_required"])

    def test_week_status_compares_explicit_tss_and_honors_ceiling(self):
        coach = {
            "state": "current",
            "target_tss": 350,
            "range": {"min": 330, "max": 370},
            "ceiling_tss": 400,
            "status": "confirmed",
            "override_source": False,
        }
        load = training_center._planned_load_for_week([], coach_budget=coach)
        row = {
            "start_date": "2026-08-10",
            "end_date": "2026-08-16",
            "status_meaningful": "below",
            "actual_hours": 1,
            "target_hours": {"min": 8, "max": 10},
            "totals": {"estimated_tss": 350},
        }
        self.assertEqual(
            training_center._week_display_status(row, [], planned_load=load, period="completed"),
            "within_budget",
        )
        self.assertEqual(
            training_center._week_display_status(row, [], planned_load=load, period="future"),
            "budget_set",
        )
        row["totals"]["estimated_tss"] = 401
        self.assertEqual(
            training_center._week_display_status(row, [], planned_load=load, period="completed"),
            "above_ceiling",
        )
        zero = training_center._planned_load_for_week([], tss_target={"min": 0, "max": 0})
        row["totals"]["estimated_tss"] = 0
        self.assertEqual(
            training_center._week_display_status(row, [], planned_load=zero, period="completed"),
            "within_budget",
        )

    def test_actual_scores_show_decimal_value_and_specific_coverage(self):
        totals = AggregateTotals()
        totals.add_activity(
            {
                "sport_type": "Ride",
                "moving_time_s": 3600,
                "estimated_tss": 56.8,
                "estimated_tss_source": "estimated_power_stream",
                "power_load_estimate": {
                    "scope": "full_duration",
                    "observed_duration_s": 3600,
                    "load_duration_s": 3600,
                },
            }
        )
        totals.add_activity(
            {
                "sport_type": "Ride",
                "moving_time_s": 2400,
                "estimated_tss": 18.6,
                "estimated_tss_source": "estimated_power_stream",
                "power_load_estimate": {
                    "scope": "recorded_power",
                    "observed_duration_s": 2325,
                    "load_duration_s": 2325,
                },
            }
        )
        display = training_center._totals_load_display(totals.finalize())
        self.assertEqual(display["tss_label"], "75 TSS")
        self.assertEqual(display["tss_short_label"], "75")
        self.assertEqual(totals.finalize()["estimated_tss"], 75.4)
        self.assertTrue(display["tss_estimated"])
        self.assertTrue(display["tss_partial"])
        self.assertIn("Calculated", display["tss_qualifier"])
        self.assertIn("98.8% power coverage", display["tss_qualifier"])
        self.assertNotIn("partial total", display["tss_qualifier"])
        self.assertIn("configured FTP", display["tss_description"])

    def test_whole_tss_labels_leave_values_and_coverage_precise(self):
        for value, label in ((0, "0"), (74.5, "75"), (75.4, "75"), (75.5, "76")):
            with self.subTest(value=value):
                activity = {
                    "estimated_tss": value,
                    "estimated_tss_source": "estimated_power_stream",
                    "power_load_estimate": {"scope": "recorded_power", "coverage_ratio": 0.999},
                }
                display = training_center._activity_load_display(activity)
                self.assertEqual(display["tss_label"], f"{label} TSS")
                self.assertEqual(display["tss_short_label"], label)
                self.assertEqual(activity["estimated_tss"], value)
                self.assertIn("99.9% power coverage", display["tss_qualifier"])
                self.assertIn("Recorded power duration", display["tss_description"])
                self.assertEqual(training_center._tss_label(value), f"{label} TSS")
        source_range = training_center._planned_load_display(
            {
                "estimated_tss": 75,
                "estimated_tss_min": 74.6,
                "estimated_tss_max": 75.4,
                "tss_source": "source_target",
            }
        )
        self.assertEqual(source_range["tss_value_label"], "75 TSS")
        self.assertEqual(source_range["estimated_tss_min"], 74.6)
        self.assertEqual(source_range["estimated_tss_max"], 75.4)

    def test_timer_based_coverage_is_not_described_as_moving_time(self):
        activity = {
            "estimated_tss": 50,
            "estimated_tss_source": "estimated_power_stream",
            "power_load_estimate": {
                "scope": "recorded_power",
                "coverage_ratio": 0.8,
                "reported_duration_source": "timer_time",
            },
        }
        display = training_center._activity_load_display(activity)
        self.assertIn("device timer time", display["tss_description"])
        self.assertNotIn("reported moving time", display["tss_description"])
        total = training_center._totals_load_display(
            {
                "estimated_tss": 50,
                "estimated_tss_estimated_activity_count": 1,
                "estimated_tss_relevant_partial_activity_count": 1,
                "estimated_tss_power_coverage_ratio": 0.8,
            }
        )
        self.assertIn("device timer time", total["tss_description"])
        self.assertIn("otherwise moving time", total["tss_description"])

    def test_unscored_walk_does_not_invalidate_a_cycling_total(self):
        totals = AggregateTotals()
        totals.add_activity(
            {
                "sport_type": "Ride",
                "moving_time_s": 3600,
                "estimated_tss": 75,
                "estimated_tss_source": "source",
            }
        )
        totals.add_activity({"sport_type": "Walk", "moving_time_s": 0})
        source = training_center._totals_load_display(totals.finalize())
        self.assertEqual(source["tss_label"], "75 TSS")
        self.assertFalse(source["tss_partial"])
        totals.add_activity({"sport_type": "Ride", "moving_time_s": 600})
        missing = training_center._totals_load_display(totals.finalize())
        self.assertTrue(missing["tss_partial"])
        self.assertIn("1 ride without load", missing["tss_qualifier"])

    def test_payload_consumes_imported_targets_and_keeps_structured_plan_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            _init_workspace(workspace, force=False)
            plan = {
                "start_date": "2026-08-10",
                "end_date": "2026-08-16",
                "days": {"Tue": "90 minutes Z2"},
                "day_loads": {
                    "Tue": {"hours_min": 1.5, "hours_max": 1.5, "tss_min": 67.5, "tss_max": 67.5}
                },
                "hours_target": {"min": 8, "max": 10},
                "tss_target": {"min": 400, "max": 500},
            }
            workout = {
                "id": "explicit-steady",
                "date": "2026-08-11",
                "name": "Independent explicit workout",
                "sport": "cycling",
                "steps": [
                    {
                        "name": "Steady",
                        "duration_s": 1800,
                        "intensity": "active",
                        "target": {"type": "power", "unit": "percent_ftp", "low": 70, "high": 70},
                    }
                ],
            }
            (workspace / "plan" / "weeks.json").write_text(json.dumps([plan]), encoding="utf-8")
            (workspace / "plan" / "workouts.json").write_text(
                json.dumps({"version": 1, "workouts": [workout]}), encoding="utf-8"
            )
            weekly = [
                {
                    "start_date": plan["start_date"],
                    "end_date": plan["end_date"],
                    "plan": plan,
                    "target_hours": plan["hours_target"],
                    "totals": {"activity_count": 0},
                }
            ]
            (workspace / "derived" / "weekly.json").write_text(json.dumps(weekly), encoding="utf-8")
            original = (workspace / "plan" / "workouts.json").read_bytes()
            payload, *_ = training_center._build_payload(workspace)
            day = next(item for item in payload["days"] if item["date"] == "2026-08-11")
            week = payload["weeks"][0]
            self.assertEqual(day["planned_load"]["estimated_tss"], 67.5)
            self.assertEqual(day["structured_workouts"][0]["load"]["hours"], 0.5)
            self.assertEqual(day["structured_workouts"][0]["load"]["estimated_tss"], 24.5)
            self.assertEqual(week["planned_load"]["tss_source"], "source_target")
            self.assertEqual(
                (
                    week["planned_load"]["estimated_tss_min"],
                    week["planned_load"]["estimated_tss_max"],
                ),
                (400, 500),
            )
            self.assertEqual((workspace / "plan" / "workouts.json").read_bytes(), original)

    def test_structured_only_date_gets_a_dashboard_week_without_source_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            _init_workspace(workspace, force=False)
            workout = {
                "id": "standalone",
                "date": "2026-08-20",
                "name": "Explicit session",
                "sport": "cycling",
                "steps": [
                    {
                        "name": "Steady",
                        "duration_s": 1800,
                        "intensity": "active",
                        "target": {"type": "power", "unit": "percent_ftp", "low": 70, "high": 70},
                    }
                ],
            }
            source = workspace / "plan" / "workouts.json"
            source.write_text(json.dumps({"version": 1, "workouts": [workout]}), encoding="utf-8")
            before = source.read_bytes()
            payload, *_ = training_center._build_payload(workspace)
            day = next(item for item in payload["days"] if item["date"] == "2026-08-20")
            self.assertEqual(day["planned"], "Explicit session")
            self.assertTrue(day["structured_is_primary"])
            self.assertEqual(day["planned_load"]["hours"], 0.5)
            self.assertEqual(day["planned_load"]["estimated_tss"], 24.5)
            self.assertEqual(source.read_bytes(), before)

    def test_empty_structured_file_does_not_validate_unrelated_legacy_exports(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            _init_workspace(workspace, force=False)
            (workspace / "plan" / "weeks.json").write_text(json.dumps([{}]), encoding="utf-8")
            (workspace / "plan" / "workouts.json").write_text(
                json.dumps({"version": 1, "workouts": []}), encoding="utf-8"
            )
            self.assertEqual(training_center._structured_dashboard_workouts(workspace, {}), {})

    def test_structured_week_projection_does_not_overlap_a_non_monday_source_week(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            _init_workspace(workspace, force=False)
            plan = {
                "start_date": "2026-09-01",
                "end_date": "2026-09-07",
                "days": {"Mon": "Monday source session", "Tue": "Tuesday source session"},
            }
            workout = {
                "id": "before-source-week",
                "date": "2026-08-31",
                "name": "Separate Monday",
                "sport": "cycling",
                "steps": [
                    {
                        "name": "Steady",
                        "duration_s": 1800,
                        "intensity": "active",
                        "target": {"type": "power", "unit": "percent_ftp", "low": 70, "high": 70},
                    }
                ],
            }
            (workspace / "plan" / "weeks.json").write_text(json.dumps([plan]), encoding="utf-8")
            (workspace / "plan" / "workouts.json").write_text(
                json.dumps({"version": 1, "workouts": [workout]}), encoding="utf-8"
            )
            (workspace / "derived" / "weekly.json").write_text(
                json.dumps([{**plan, "plan": plan, "totals": {}}]), encoding="utf-8"
            )
            payload, *_ = training_center._build_payload(workspace)
            dates = [item["date"] for item in payload["days"]]
            self.assertEqual(len(dates), len(set(dates)))
            self.assertEqual(
                next(item["planned"] for item in payload["days"] if item["date"] == "2026-09-01"),
                "Tuesday source session",
            )
            self.assertEqual(
                next(item["planned"] for item in payload["days"] if item["date"] == "2026-09-07"),
                "Monday source session",
            )


if __name__ == "__main__":
    unittest.main()
