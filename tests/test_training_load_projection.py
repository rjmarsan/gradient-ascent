import copy
import math
import unittest
from datetime import date, timedelta

from gradient_ascent.training_load import build_training_load


TODAY = date(2026, 8, 17)


def recorded(*, score=100, as_of=TODAY, **counts):
    return build_training_load(
        [{"date": as_of.isoformat(), "totals": {"estimated_tss": score, **counts}}],
        as_of=as_of,
    )


def target(day, score, source="source_target", status="prescribed"):
    return {"date": day.isoformat(), "target_tss": score, "tss_source": source, "status": status}


class TrainingLoadProjectionTest(unittest.TestCase):
    def project(self, model, targets, *, as_of=TODAY, end=None):
        from gradient_ascent.training_load_projection import build_training_load_projection

        return build_training_load_projection(
            model, targets, as_of=as_of, end=end or as_of + timedelta(days=3)
        )

    def test_tomorrow_recurrence_uses_recorded_anchor_and_never_adds_today_plan(self):
        model = recorded()
        targets = [
            target(TODAY, 900),
            target(TODAY + timedelta(days=1), 80),
            target(TODAY + timedelta(days=2), 0, "explicit_rest"),
            target(TODAY + timedelta(days=3), 50, "structured_power_model"),
        ]
        before = copy.deepcopy((model, targets))
        result = self.project(model, targets)
        anchor = model["rows"][-1]
        ctl, atl = anchor["ctl"], anchor["atl"]
        self.assertEqual(result["method"], "ctl_atl_daily_projection_v1")
        self.assertEqual(result["time_constants"], {"ctl": 42, "atl": 7})
        self.assertEqual(result["anchor"]["date"], TODAY.isoformat())
        self.assertEqual(result["anchor"]["ctl"], ctl)
        self.assertEqual([row["target_tss"] for row in result["rows"]], [80, 0, 50])
        for row, score in zip(result["rows"], (80, 0, 50)):
            self.assertAlmostEqual(row["tsb"], ctl - atl, places=6)
            ctl += (score - ctl) / 42
            atl += (score - atl) / 7
            self.assertAlmostEqual(row["ctl"], ctl, places=6)
            self.assertAlmostEqual(row["atl"], atl, places=6)
            self.assertTrue(row["projected"])
        self.assertEqual(result["through_date"], "2026-08-20")
        self.assertEqual(result["summary"]["stop_reason"], "complete")
        self.assertIsNone(result["summary"]["stop_date"])
        self.assertEqual((model, targets), before)

    def test_first_unknown_stops_without_splicing_later_targets(self):
        first = TODAY + timedelta(days=1)
        later = TODAY + timedelta(days=3)
        result = self.project(recorded(), [target(later, 100), target(first, 0, "explicit_rest")])
        self.assertEqual([row["date"] for row in result["rows"]], [first.isoformat()])
        self.assertEqual(result["summary"]["stop_reason"], "missing_daily_target")
        self.assertEqual(result["summary"]["stop_date"], "2026-08-19")
        empty = self.project(recorded(), [target(later, 100)])
        self.assertEqual(empty["rows"], [])
        self.assertEqual(empty["anchor"]["date"], TODAY.isoformat())
        self.assertEqual(empty["through_date"], TODAY.isoformat())
        self.assertFalse(empty["summary"]["available"])
        self.assertEqual(empty["summary"]["stop_date"], first.isoformat())

    def test_missing_or_stale_recorded_anchor_is_explicitly_unavailable(self):
        targets = [target(TODAY + timedelta(days=1), 100)]
        absent = self.project(build_training_load([], as_of=TODAY), targets)
        self.assertEqual(absent["summary"]["stop_reason"], "no_recorded_baseline")
        self.assertIsNone(absent["anchor"])
        self.assertIsNone(absent["through_date"])
        stale = self.project(recorded(as_of=TODAY - timedelta(days=1)), targets)
        self.assertEqual(stale["summary"]["stop_reason"], "stale_recorded_model")
        self.assertIsNone(stale["anchor"])
        cropped = recorded()
        cropped["rows"] = []
        self.assertEqual(
            self.project(cropped, targets)["summary"]["stop_reason"], "no_recorded_baseline"
        )

    def test_genuine_zero_anchor_and_explicit_zero_target_are_valid(self):
        result = self.project(
            recorded(score=0),
            [target(TODAY + timedelta(days=1), 0, "explicit_rest")],
            end=TODAY + timedelta(days=1),
        )
        self.assertTrue(result["summary"]["available"])
        self.assertEqual((result["rows"][0]["ctl"], result["rows"][0]["atl"]), (0, 0))
        none_requested = self.project(recorded(), [], end=TODAY)
        self.assertEqual(none_requested["summary"]["stop_reason"], "complete")
        self.assertEqual(none_requested["anchor"]["date"], TODAY.isoformat())

    def test_incomplete_seed_and_provisional_quality_are_carried_forward(self):
        model = recorded(estimated_tss_missing_activity_count=1)
        result = self.project(
            model,
            [
                target(TODAY + timedelta(days=1), 60, "coach_budget_allocation", "provisional"),
                target(TODAY + timedelta(days=2), 40, "coach_budget_allocation", "confirmed"),
            ],
            end=TODAY + timedelta(days=2),
        )
        self.assertTrue(result["anchor"]["history_incomplete"])
        self.assertTrue(result["summary"]["history_incomplete"])
        self.assertEqual(result["summary"]["provisional_days"], 1)
        for offset, row in enumerate(result["rows"], 1):
            self.assertTrue(row["history_incomplete"])
            self.assertTrue(row["projection_provisional"])
            self.assertEqual(row["recorded_history_days"], 1)
            self.assertAlmostEqual(
                row["seed_weight_ctl"],
                model["rows"][-1]["seed_weight_ctl"] * (41 / 42) ** offset,
                places=6,
            )
            self.assertAlmostEqual(
                row["seed_weight_atl"],
                model["rows"][-1]["seed_weight_atl"] * (6 / 7) ** offset,
                places=6,
            )

    def test_long_projection_keeps_internal_precision_and_enforces_horizon(self):
        inputs = [target(TODAY + timedelta(days=i), 100) for i in range(1, 731)]
        initial = recorded()["rows"][-1]
        result = self.project(recorded(), inputs, end=TODAY + timedelta(days=730))
        last = result["rows"][-1]
        self.assertAlmostEqual(
            last["ctl"], 100 + (initial["ctl"] - 100) * (41 / 42) ** 730, places=6
        )
        self.assertAlmostEqual(last["atl"], 100 + (initial["atl"] - 100) * (6 / 7) ** 730, places=6)
        with self.assertRaises(ValueError):
            self.project(recorded(), inputs, end=TODAY + timedelta(days=731))

    def test_invalid_or_implicit_targets_fail_closed(self):
        valid = target(TODAY + timedelta(days=1), 100)
        cases = [
            {**valid, "target_tss": value}
            for value in (None, True, -1, math.nan, math.inf, "100", 21601, 10**400)
        ] + [{**valid, "date": "2026-02-30"}]
        cases.extend(
            {**valid, "tss_source": source}
            for source in ("weekly_hours_budget", "session_if_forecast", "coach_budget")
        )
        cases.extend(
            (
                {**valid, "status": "needs_review"},
                {**valid, "tss_source": "coach_budget_allocation"},
                {**valid, "tss_source": "explicit_rest"},
                {**valid, "hours": 2},
            )
        )
        for invalid in cases:
            with self.subTest(invalid=repr(invalid)[:120]):
                with self.assertRaises(ValueError):
                    self.project(recorded(), [invalid])
        with self.assertRaises(ValueError):
            self.project(recorded(), [valid, dict(valid)])
        with self.assertRaises(ValueError):
            self.project(recorded(), [valid] * 3661)
        with self.assertRaises(ValueError):
            self.project(recorded(), {valid["date"]: valid})
        with self.assertRaises(ValueError):
            self.project(recorded(), [], end=TODAY - timedelta(days=1))

    def test_invalid_recorded_anchor_never_generates_a_forecast(self):
        for change in (
            {"method": "different"},
            {"time_constants": {"ctl": 7, "atl": 42}},
            {"through_date": "2026-02-30"},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.project({**recorded(), **change}, [])
        for field, value in (
            ("ctl", math.nan),
            ("atl", -1),
            ("seed_weight_ctl", 2),
            ("history_incomplete", "false"),
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                model = recorded()
                model["rows"][-1][field] = value
                self.project(model, [])


if __name__ == "__main__":
    unittest.main()
