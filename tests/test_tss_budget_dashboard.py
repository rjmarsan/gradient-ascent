import json
import shutil
import subprocess
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from gradient_ascent import training_center
from gradient_ascent.cli import _init_workspace
from gradient_ascent.storage import write_json


def coach_budget(**changes):
    return {
        "state": "current",
        "target_tss": 330,
        "range": {"min": 330, "max": 330},
        "ceiling_tss": 380,
        "status": "provisional",
        "rationale": "Protect the priority event; no catch-up riding.",
        "conditions": ["Revisit if the optional event is chosen."],
        "revision": 1,
        "override_source": False,
        **changes,
    }


class TssBudgetDashboardTest(unittest.TestCase):
    def test_unbudgeted_past_weeks_describe_recorded_evidence(self):
        base = {
            "start_date": "2026-08-10",
            "end_date": "2026-08-16",
            "status_meaningful": "above",
            "target_hours": {"min": 8, "max": 11},
        }
        cases = (
            ({"estimated_tss": 458.3, "activity_count": 4}, "recorded_history"),
            ({"estimated_tss": 0, "activity_count": 1}, "recorded_history"),
            (
                {"estimated_tss": 70, "estimated_tss_relevant_partial_activity_count": 1},
                "load_incomplete",
            ),
            (
                {
                    "estimated_tss": None,
                    "activity_count": 1,
                    "estimated_tss_missing_activity_count": 1,
                },
                "load_incomplete",
            ),
            ({"estimated_tss": None, "activity_count": 1}, "recorded_unscored"),
            ({"estimated_tss": None, "activity_count": 0}, "no_recordings"),
        )
        for totals, expected in cases:
            with self.subTest(expected=expected, totals=totals):
                row = {**base, "totals": totals}
                self.assertEqual(
                    training_center._week_display_status(row, [], today=date(2026, 8, 17)),
                    expected,
                )
                self.assertEqual(
                    training_center._week_display_status(row, [], today=date(2026, 8, 16)),
                    "budget_missing",
                )
        self.assertEqual(training_center._status_label("recorded_history"), "Recorded")
        self.assertEqual(training_center._status_label("recorded_unscored"), "No scored load")
        self.assertEqual(training_center._status_label("no_recordings"), "No recordings")

    def test_historical_stale_budget_keeps_review_or_valid_source_comparison(self):
        row = {
            "start_date": "2026-08-10",
            "end_date": "2026-08-16",
            "totals": {"estimated_tss": 320},
        }
        stale = coach_budget(state="needs_review")
        missing = training_center._planned_load_for_week([], coach_budget=stale)
        self.assertEqual(
            training_center._week_display_status(row, [], period="completed", planned_load=missing),
            "budget_review",
        )
        source = training_center._planned_load_for_week(
            [], tss_target={"min": 300, "max": 350}, coach_budget=stale
        )
        self.assertEqual(
            training_center._week_display_status(row, [], period="completed", planned_load=source),
            "within_budget",
        )
        self.assertTrue(source["budget_review_required"])

    def test_week_uses_the_authored_budget_without_converting_hours(self):
        days = [{"planned_load": training_center._planned_load_for_day("2 hours Z2", [])}]
        load = training_center._planned_load_for_week(
            days, hours_target={"min": 8, "max": 11}, coach_budget=coach_budget()
        )
        self.assertEqual(
            (load["estimated_tss_min"], load["estimated_tss"], load["estimated_tss_max"]),
            (330, 330, 330),
        )
        self.assertEqual(load["hours_label"], "8–11h")
        self.assertEqual(load["qualifier"], "Coach budget · provisional")
        self.assertEqual(load["budget_ceiling_label"], "380 TSS")
        self.assertEqual(load["confidence"], "authored")
        missing = training_center._planned_load_for_week(days, hours_target={"min": 8, "max": 11})
        self.assertIsNone(missing["estimated_tss"])
        self.assertEqual(missing["qualifier"], "Budget not set")

    def test_payload_reads_exact_week_budgets_and_retains_review_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "athlete"
            _init_workspace(root, force=False)
            weekly = [
                {
                    "start_date": "2026-08-17",
                    "end_date": "2026-08-23",
                    "plan": {"days": {"Tue": "2 hours Z2"}},
                    "target_hours": {"min": 8, "max": 11},
                    "totals": {"estimated_tss": 75.4, "activity_count": 1},
                },
                {
                    "start_date": "2026-08-24",
                    "end_date": "2026-08-30",
                    "plan": {"days": {"Tue": "2 hours Z2"}},
                    "target_hours": {"min": 8, "max": 11},
                    "totals": {"estimated_tss": None, "activity_count": 0},
                },
            ]
            write_json(root / "derived" / "weekly.json", weekly)
            original = (root / "derived" / "weekly.json").read_bytes()
            budgets = {
                ("2026-08-17", "2026-08-23"): coach_budget(),
                ("2026-08-24", "2026-08-30"): coach_budget(state="needs_review"),
            }
            with patch(
                "gradient_ascent.training_center.load_tss_budgets",
                return_value=budgets,
                create=True,
            ) as loader:
                payload, *_ = training_center._build_payload(root)
            loader.assert_called_once_with(root)
            first, second = payload["weeks"]
            self.assertEqual(first["planned_load"]["estimated_tss"], 330)
            self.assertEqual(first["totals"]["estimated_tss"], 75.4)
            self.assertEqual(
                first["coach_budget"]["rationale"],
                budgets[("2026-08-17", "2026-08-23")]["rationale"],
            )
            self.assertIsNone(second["planned_load"]["estimated_tss"])
            self.assertEqual(second["planned_load"]["qualifier"], "Budget needs review")
            self.assertEqual(second["status_by_period"]["future"]["status"], "budget_review")
            self.assertEqual((root / "derived" / "weekly.json").read_bytes(), original)

    def test_budget_status_is_not_hours_pacing_or_a_makeup_quota(self):
        row = {
            "start_date": "2026-08-18",
            "end_date": "2026-08-24",
            "status_meaningful": "above",
            "totals": {"estimated_tss": 200},
        }
        load = training_center._planned_load_for_week([], coach_budget=coach_budget())
        cases = (
            (date(2026, 8, 17), "budget_set"),
            (date(2026, 8, 18), "in_progress"),
            (date(2026, 8, 24), "in_progress"),
            (date(2026, 8, 25), "below_budget"),
        )
        for today, expected in cases:
            with self.subTest(today=today):
                self.assertEqual(
                    training_center._week_display_status(row, [], today=today, planned_load=load),
                    expected,
                )
        row["totals"]["estimated_tss"] = 329.6
        self.assertEqual(
            training_center._week_display_status(
                row, [], today=date(2026, 8, 25), planned_load=load
            ),
            "within_budget",
        )
        row["totals"].update(estimated_tss=390, estimated_tss_missing_activity_count=1)
        self.assertEqual(
            training_center._week_display_status(
                row, [], today=date(2026, 8, 20), planned_load=load
            ),
            "above_ceiling",
        )
        row["totals"]["estimated_tss"] = 200
        self.assertEqual(
            training_center._week_display_status(
                row, [], today=date(2026, 8, 25), planned_load=load
            ),
            "load_incomplete",
        )
        row["totals"] = {"estimated_tss": None}
        self.assertEqual(
            training_center._week_display_status(
                row, [], today=date(2026, 8, 20), planned_load=load
            ),
            "not_measured",
        )
        missing = training_center._planned_load_for_week([], hours_target={"min": 8, "max": 11})
        self.assertEqual(
            training_center._week_display_status(
                row, [], today=date(2026, 8, 20), planned_load=missing
            ),
            "budget_missing",
        )
        for dates in (
            {"start_date": "20260818"},
            {"end_date": "2026-02-30"},
            {"end_date": "2026-08-17"},
        ):
            with self.subTest(dates=dates):
                variants = training_center._week_status_variants({**row, **dates}, [], load)
                self.assertTrue(
                    all(value["status"] == "budget_missing" for value in variants.values())
                )

    def test_all_unscored_rides_are_incomplete_not_absent_evidence(self):
        row = {
            "start_date": "2026-08-17",
            "end_date": "2026-08-23",
            "totals": {
                "estimated_tss": None,
                "activity_count": 1,
                "estimated_tss_missing_activity_count": 1,
            },
        }
        load = training_center._planned_load_for_week([], coach_budget=coach_budget())
        variants = training_center._week_status_variants(row, [], load)
        self.assertEqual(variants["future"]["status"], "budget_set")
        self.assertEqual(variants["current"]["status"], "load_incomplete")
        self.assertEqual(variants["completed"]["status"], "load_incomplete")

    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for live-date status selection")
    def test_live_date_selects_precomputed_status_without_recalculating_load(self):
        template = training_center.HTML_TEMPLATE
        start = template.index("    function weekStatusForToday(")
        source = template[start : template.index("\n    function ", start + 5)]
        week = {
            "start_date": "2026-08-18",
            "end_date": "2026-08-24",
            "status_by_period": {
                "future": {"status": "budget_set", "label": "Budget set"},
                "current": {"status": "in_progress", "label": "In progress"},
                "completed": {"status": "below_budget", "label": "Below budget"},
            },
        }
        script = (
            source
            + f"\nconst week={json.dumps(week)};\n"
            + (
                "console.log(JSON.stringify(['2026-08-17','2026-08-18','2026-08-24','2026-08-25'].map(today=>weekStatusForToday(week,today).status)));"
            )
        )
        result = subprocess.run(
            [shutil.which("node"), "-e", script],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(
            json.loads(result.stdout), ["budget_set", "in_progress", "in_progress", "below_budget"]
        )


if __name__ == "__main__":
    unittest.main()
