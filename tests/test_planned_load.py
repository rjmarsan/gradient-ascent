import math
import unittest

from gradient_ascent.planned_load import (
    day_planned_load,
    parse_source_range,
    structured_workout_load,
    week_planned_load,
)


class PlannedLoadTest(unittest.TestCase):
    def test_explicit_source_targets_win_and_zero_is_real(self) -> None:
        load = day_planned_load(
            hours_min=1, hours_max=2, tss_min=0, tss_max=0, intensity="threshold"
        )
        self.assertEqual(load["estimated_tss"], 0)
        self.assertEqual(load["tss_source"], "source_target")
        self.assertFalse(load["estimated"])
        self.assertIsNone(day_planned_load()["hours"])
        self.assertIsNone(day_planned_load()["estimated_tss"])
        rest = day_planned_load(is_rest=True)
        self.assertEqual((rest["hours"], rest["estimated_tss"]), (0, 0))

    def test_whole_session_forecast_exposes_formula_and_assumptions(self) -> None:
        load = day_planned_load(hours_min=1, hours_max=2, intensity="endurance")
        self.assertEqual(load["method"], "whole_session_if_v1")
        self.assertEqual(load["tss_source"], "session_if_forecast")
        self.assertTrue(load["estimated"])
        self.assertAlmostEqual(
            load["estimated_tss_min"], round(100 * load["assumed_if_min"] ** 2, 1)
        )
        self.assertAlmostEqual(
            load["estimated_tss_max"], round(200 * load["assumed_if_max"] ** 2, 1)
        )
        self.assertIsNone(day_planned_load(intensity="threshold")["estimated_tss"])
        self.assertIsNone(day_planned_load(hours_min=1, intensity="unknown")["estimated_tss"])
        self.assertEqual(
            day_planned_load(hours_min=0, hours_max=2, intensity="endurance")["estimated_tss_min"],
            0,
        )

    def test_invalid_ranges_never_become_zero_or_forecasts(self) -> None:
        for low, high in ((2, 1), (-1, 2), (True, 2), (math.nan, 2), (1, math.inf), (25, 25)):
            with self.subTest(low=low, high=high):
                load = day_planned_load(hours_min=low, hours_max=high, intensity="endurance")
                self.assertIsNone(load["hours"])
                self.assertIsNone(load["estimated_tss"])
        self.assertIsNone(day_planned_load(tss_min=-5, tss_max=10)["estimated_tss"])

    def test_weekly_source_then_complete_sum_then_separate_budget(self) -> None:
        source = day_planned_load(hours_min=1, tss_min=50)
        unknown = day_planned_load()
        days = [source, unknown]
        explicit = week_planned_load(
            days, hours_target={"min": 5, "max": 7}, tss_target={"min": 0, "max": 0}
        )
        self.assertEqual(explicit["estimated_tss"], 0)
        self.assertEqual(explicit["tss_source"], "source_target")
        complete = week_planned_load([source, source], hours_target={"min": 5, "max": 7})
        self.assertEqual(complete["estimated_tss"], 100)
        self.assertEqual(complete["tss_source"], "complete_daily_sum")
        budget = week_planned_load(days, hours_target={"min": 5, "max": 7})
        self.assertEqual(budget["method"], "weekly_hours_budget_if_v1")
        self.assertEqual(budget["estimated_tss_min"], round(100 * 5 * 0.55**2, 1))
        self.assertEqual(budget["estimated_tss_max"], round(100 * 7 * 0.85**2, 1))
        self.assertEqual(
            (budget["known_hours_days"], budget["known_tss_days"], budget["total_days"]),
            (1, 1, 2),
        )
        self.assertIsNone(unknown["hours"])
        self.assertIsNone(unknown["estimated_tss"])
        self.assertIsNone(week_planned_load(days)["estimated_tss"])

    def test_source_range_parser_preserves_units_and_rejects_ambiguous_values(self) -> None:
        self.assertEqual(parse_source_range("90", unit="minutes", maximum=24), (1.5, 1.5))
        self.assertEqual(parse_source_range("60–90 min", unit="hours", maximum=24), (1, 1.5))
        self.assertEqual(parse_source_range("8h-10h", unit="hours", maximum=168), (8, 10))
        self.assertEqual(
            parse_source_range("8 hours to 10 hours", unit="hours", maximum=168), (8, 10)
        )
        self.assertEqual(parse_source_range("01:30:00", unit="hours", maximum=24), (1.5, 1.5))
        self.assertEqual(parse_source_range("45-65 TSS", unit="tss", maximum=21600), (45, 65))
        for value in ("-10", "65-45", "NaN", "inf", True, "3x10 min", "90 mystery"):
            with self.subTest(value=value):
                self.assertEqual(parse_source_range(value, unit="hours", maximum=24), (None, None))

    def test_structured_workout_is_separate_exact_duration_and_power_model(self) -> None:
        def step(duration, target):
            return {
                "name": "Synthetic",
                "duration_s": duration,
                "intensity": "active",
                "target": target,
            }

        relative = {"type": "power", "unit": "percent_ftp", "low": 80, "high": 100}
        workout = {"sport": "cycling", "steps": [step(3600, relative)]}
        load = structured_workout_load(workout)
        self.assertEqual(load["hours"], 1)
        self.assertEqual(load["duration_source"], "structured_steps")
        self.assertEqual(load["method"], "structured_power_fourth_moment_v1")
        self.assertEqual((load["estimated_tss_min"], load["estimated_tss_max"]), (64, 100))
        absolute = {"type": "power", "unit": "watts", "low": 200, "high": 200}
        workout["steps"] = [step(3600, absolute)]
        self.assertIsNone(structured_workout_load(workout)["estimated_tss"])
        self.assertEqual(structured_workout_load(workout, ftp_w=200)["estimated_tss"], 100)
        workout["steps"] = [step(3600, {"type": "open"})]
        opened = structured_workout_load(workout, ftp_w=200)
        self.assertEqual(opened["hours"], 1)
        self.assertIsNone(opened["estimated_tss"])
