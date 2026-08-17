import copy
import math
import unittest
from datetime import date, timedelta


def day(stamp, tss, **counts):
    return {
        "date": stamp,
        "totals": {
            "estimated_tss": tss,
            "estimated_tss_activity_count": int(tss is not None),
            "estimated_tss_relevant_activity_count": 1,
            "estimated_tss_missing_activity_count": 0,
            "estimated_tss_relevant_partial_activity_count": 0,
            **counts,
        },
    }


class TrainingLoadTest(unittest.TestCase):
    def test_single_impulse_uses_published_daily_recurrence_and_next_day_form(self):
        from gradient_ascent.training_load import build_training_load

        result = build_training_load([day("2026-01-01", 100)], as_of=date(2026, 1, 2))
        first, second = result["rows"]
        self.assertAlmostEqual(first["ctl"], 100 / 42, places=6)
        self.assertAlmostEqual(first["atl"], 100 / 7, places=6)
        self.assertEqual(first["tsb"], 0)
        self.assertAlmostEqual(second["ctl"], 100 / 42 * 41 / 42, places=6)
        self.assertAlmostEqual(second["atl"], 100 / 7 * 6 / 7, places=6)
        self.assertAlmostEqual(second["tsb"], 100 / 42 - 100 / 7, places=6)
        self.assertEqual(second["day_status"], "no_recording")
        self.assertEqual(second["load_applied"], 0)
        self.assertEqual(result["time_constants"], {"ctl": 42, "atl": 7})

    def test_constant_load_approaches_constant_without_rounding_each_step(self):
        from gradient_ascent.training_load import build_training_load

        start = date(2025, 1, 1)
        inputs = [day((start + timedelta(days=i)).isoformat(), 100) for i in range(365)]
        last = build_training_load(inputs, as_of=start + timedelta(days=364))["rows"][-1]
        self.assertAlmostEqual(last["ctl"], 100 * (1 - (41 / 42) ** 365), places=6)
        self.assertAlmostEqual(last["atl"], 100 * (1 - (6 / 7) ** 365), places=6)
        self.assertAlmostEqual(last["seed_weight_ctl"], (41 / 42) ** 365, places=6)

    def test_year_boundary_and_display_filter_preserve_prior_history(self):
        from gradient_ascent.training_load import build_training_load

        rows = [day("2024-12-31", 100), day("2025-01-02", 50)]
        full = build_training_load(rows, as_of=date(2025, 1, 3))
        selected = build_training_load(
            rows, as_of=date(2025, 1, 3), start=date(2025, 1, 2), end=date(2025, 1, 2)
        )
        self.assertEqual(selected["rows"], [full["rows"][2]])
        self.assertEqual(selected["history_start"], "2024-12-31")
        self.assertEqual(selected["through_date"], "2025-01-03")
        leap = build_training_load([day("2024-02-28", 0)], as_of=date(2024, 3, 1))
        self.assertEqual(
            [row["date"] for row in leap["rows"]], ["2024-02-28", "2024-02-29", "2024-03-01"]
        )

    def test_true_zero_starts_history_but_missing_scores_never_invent_a_seed(self):
        from gradient_ascent.training_load import build_training_load

        missing = day("2026-01-01", None, estimated_tss_missing_activity_count=1)
        unavailable = build_training_load([missing], as_of=date(2026, 1, 4))
        self.assertFalse(unavailable["summary"]["available"])
        self.assertEqual(unavailable["rows"], [])
        self.assertIsNone(unavailable["history_start"])
        result = build_training_load([missing, day("2026-01-03", 0)], as_of=date(2026, 1, 4))
        self.assertEqual(result["history_start"], "2026-01-03")
        self.assertEqual(result["rows"][0]["ctl"], 0)
        self.assertEqual(result["summary"]["prior_unscored_days"], 1)
        self.assertTrue(result["rows"][0]["history_incomplete"])

    def test_partial_and_missing_days_use_known_subtotal_and_remain_qualified(self):
        from gradient_ascent.training_load import build_training_load

        inputs = [
            day("2026-01-01", 100),
            day("2026-01-02", 40, estimated_tss_missing_activity_count=1),
            day("2026-01-03", None, estimated_tss_missing_activity_count=1),
            day("2026-01-04", 20, estimated_tss_relevant_partial_activity_count=1),
        ]
        result = build_training_load(inputs, as_of=date(2026, 2, 20))
        self.assertEqual(
            [row["day_status"] for row in result["rows"][:4]],
            ["complete", "partial", "missing", "partial"],
        )
        self.assertEqual([row["load_applied"] for row in result["rows"][:4]], [100, 40, 0, 20])
        self.assertIsNone(result["rows"][2]["tss_observed"])
        self.assertTrue(result["rows"][-1]["history_incomplete"])
        self.assertEqual(result["rows"][-1]["recent_incomplete_days_42"], 0)
        self.assertEqual(result["rows"][3]["recent_incomplete_days_7"], 3)
        self.assertEqual(result["summary"]["incomplete_days"], 3)

    def test_future_rows_ignored_and_published_daily_score_scope_preserved(self):
        from gradient_ascent.training_load import build_training_load

        rows = [
            day("2026-01-01", 25, estimated_tss_relevant_activity_count=0),
            day("2026-01-10", 100),
        ]
        before = copy.deepcopy(rows)
        result = build_training_load(rows, as_of=date(2026, 1, 2))
        self.assertEqual(result["rows"][0]["load_applied"], 25)
        self.assertEqual(len(result["rows"]), 2)
        self.assertEqual(rows, before)

    def test_invalid_scores_dates_duplicates_and_span_fail_closed(self):
        from gradient_ascent.training_load import build_training_load

        for value in (True, -1, math.nan, math.inf, "100", 10**400):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(ValueError):
                    build_training_load([day("2026-01-01", value)], as_of=date(2026, 1, 2))
        for rows in ([day("2026-01-01", 0)] * 2, [day("2026-02-30", 0)]):
            with self.assertRaises(ValueError):
                build_training_load(rows, as_of=date(2026, 3, 1))
        with self.assertRaises(ValueError):
            build_training_load([day("1800-01-01", 0)], as_of=date(2026, 1, 1))
        with self.assertRaises(ValueError):
            build_training_load(
                [], as_of=date(2026, 1, 1), start=date(2026, 2, 1), end=date(2026, 1, 1)
            )


if __name__ == "__main__":
    unittest.main()
