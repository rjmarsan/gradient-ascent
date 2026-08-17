import json
import shutil
import subprocess
import unittest

from gradient_ascent import training_center


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for chart regressions")
class SeasonLoadChartTest(unittest.TestCase):
    def run_chart(self, expression: str, *, weeks: list[dict] | None = None):
        template = training_center.HTML_TEMPLATE
        start = template.index("    function seasonLoadSeries(")
        end = template.index("\n    function seasonHorizonLayout(", start)
        script = (
            "const escapeHtml = value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('\"', '&quot;');\n"
            "const dayLabel = value => value;\n"
            f"const weeks = {json.dumps(weeks or [])};\n"
            "const horizon = {start_date:'2026-01-05',end_date:'2026-02-08'};\n"
            + template[start:end]
            + f"\nconsole.log(JSON.stringify({expression}));"
        )
        result = subprocess.run(
            [shutil.which("node"), "-e", script],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        return json.loads(result.stdout)

    def test_series_uses_real_dates_and_preserves_missing_zero_and_future(self):
        weeks = [
            {
                "start_date": "2025-12-29",
                "end_date": "2026-01-04",
                "planned_load": {
                    "estimated_tss_min": 9000,
                    "estimated_tss_max": 9900,
                    "tss_source": "source_target",
                },
                "totals": {"activity_count": 1, "estimated_tss": 9000},
            },
            {
                "start_date": "2026-01-26",
                "end_date": "2026-02-01",
                "planned_load": {
                    "estimated_tss_min": 400,
                    "estimated_tss_max": 600,
                    "tss_source": "source_target",
                },
                "totals": {"activity_count": 0, "estimated_tss": None},
            },
            {
                "start_date": "2026-01-05",
                "end_date": "2026-01-11",
                "planned_load": {
                    "estimated_tss_min": 300,
                    "estimated_tss_max": 450,
                    "tss_source": "source_target",
                },
                "target_hours": {"min": 6, "max": 9},
                "actual_hours": 7.5,
                "totals": {"activity_count": 3, "estimated_tss": 375.4},
            },
            {
                "start_date": "2026-01-12",
                "end_date": "2026-01-18",
                "planned_load": {"estimated_tss_min": None, "estimated_tss_max": None},
                "target_hours": {"min": 8, "max": 12},
                "actual_hours": 4,
                "totals": {"activity_count": 2, "estimated_tss": None},
            },
            {
                "start_date": "2026-01-19",
                "end_date": "2026-01-25",
                "planned_load": {
                    "estimated_tss_min": 0,
                    "estimated_tss_max": 0,
                    "tss_source": "source_target",
                },
                "totals": {
                    "activity_count": 1,
                    "estimated_tss_activity_count": 1,
                    "estimated_tss": 0,
                },
            },
            {
                "start_date": "2026-02-02",
                "end_date": "2026-02-08",
                "planned_load": {
                    "estimated_tss_min": 450,
                    "estimated_tss_max": 700,
                    "tss_source": "source_target",
                },
                "totals": {"activity_count": 1, "estimated_tss": 200},
            },
        ]
        result = self.run_chart("seasonLoadSeries(weeks,horizon,'2026-01-28')", weeks=weeks)
        rows = result["rows"]
        self.assertEqual(
            [row["start_date"] for row in rows],
            ["2026-01-05", "2026-01-12", "2026-01-19", "2026-01-26", "2026-02-02"],
        )
        self.assertEqual([row["recorded_tss"] for row in rows], [375.4, None, 0, None, None])
        self.assertEqual([row["target_min"] for row in rows], [300, None, 0, 400, 450])
        self.assertAlmostEqual(rows[0]["left"], 0)
        self.assertAlmostEqual(rows[0]["right"], 20)
        self.assertAlmostEqual(rows[-1]["right"], 100)
        self.assertTrue(rows[3]["to_date"])
        self.assertFalse(rows[4]["to_date"])
        self.assertEqual([len(run) for run in result["target_runs"]], [1, 3])
        self.assertEqual([len(run) for run in result["recorded_runs"]], [1, 1])
        self.assertGreaterEqual(result["max_tss"], 700)
        self.assertLess(result["max_tss"], 9000)

    def test_week_budget_labels_preserve_ranges_and_recorded_provenance(self):
        weeks = [
            {
                "start_date": "2026-01-05",
                "end_date": "2026-01-11",
                "planned_load": {
                    "estimated_tss_min": 300,
                    "estimated_tss_max": 450,
                    "estimated": False,
                    "tss_source": "source_target",
                    "qualifier": "Source target",
                },
                "totals": {"activity_count": 3, "estimated_tss": 375.4},
                "tss_qualifier": "Source",
            },
            {
                "start_date": "2026-01-12",
                "end_date": "2026-01-18",
                "planned_load": {
                    "estimated_tss_min": 400.4,
                    "estimated_tss_max": 650.6,
                    "estimated": True,
                    "tss_source": "complete_prescribed_sum",
                    "qualifier": "Modeled prescriptions",
                },
                "totals": {"activity_count": 4, "estimated_tss": 450.7},
                "tss_qualifier": "Calculated · 80.1% power coverage · 1 ride without load",
            },
        ]
        labels = self.run_chart(
            "seasonLoadSeries(weeks,horizon,'2026-01-18').rows.map(seasonWeekLoadLabel)",
            weeks=weeks,
        )
        self.assertIn("Planned 300–450 TSS (Source target)", labels[0])
        self.assertIn("Recorded 375 TSS (Source)", labels[0])
        self.assertIn("Planned 400–651 TSS (Modeled prescriptions)", labels[1])
        self.assertIn("Recorded 451 TSS so far", labels[1])
        self.assertIn("80.1% power coverage", labels[1])
        self.assertIn("1 ride without load", labels[1])
        self.assertNotIn("NaN", " ".join(labels))
        self.assertNotIn("Infinity", " ".join(labels))

    def test_missing_week_budget_stays_missing_and_ranges_are_clipped(self):
        empty = self.run_chart("seasonLoadSeries(weeks,horizon,'2026-01-18')")
        self.assertEqual(empty["rows"], [])
        self.assertEqual(self.run_chart("seasonWeekLoadLabel(null)"), "No weekly TSS data")
        weeks = [
            {
                "start_date": "2026-01-01",
                "end_date": "2026-01-07",
                "planned_load": {
                    "estimated_tss_min": 100,
                    "estimated_tss_max": 200,
                    "tss_source": "source_target",
                },
                "totals": {"activity_count": 1, "estimated_tss": 150},
            },
            {
                "start_date": "2026-02-05",
                "end_date": "2026-02-11",
                "planned_load": {
                    "estimated_tss_min": 400,
                    "estimated_tss_max": 300,
                    "estimated_tss": 350,
                    "tss_source": "source_target",
                },
                "totals": {"activity_count": 1, "estimated_tss": -1},
            },
        ]
        rows = self.run_chart("seasonLoadSeries(weeks,horizon,'2026-02-08').rows", weeks=weeks)
        self.assertEqual(rows[0]["left"], 0)
        self.assertAlmostEqual(rows[0]["right"], 100 * 3 / 35)
        self.assertEqual(rows[-1]["right"], 100)
        self.assertIsNone(rows[-1]["target_min"])
        self.assertIsNone(rows[-1]["recorded_tss"])

    def test_exact_target_fallback_and_missing_recorded_load_are_explicit(self):
        weeks = [
            {
                "start_date": "2026-01-05",
                "end_date": "2026-01-11",
                "planned_load": {
                    "estimated_tss": 74.5,
                    "qualifier": "Source target",
                    "tss_source": "source_target",
                },
                "totals": {"activity_count": 1, "estimated_tss": None},
                "tss_qualifier": "1 ride without load",
            },
            {
                "start_date": "2026-01-12",
                "end_date": "2026-01-18",
                "planned_load": {
                    "estimated_tss_min": 100,
                    "estimated_tss_max": None,
                    "estimated_tss": 150,
                    "tss_source": "source_target",
                },
                "totals": {"activity_count": 1, "estimated_tss": True},
            },
        ]
        rows = self.run_chart("seasonLoadSeries(weeks,horizon,'2026-01-18').rows", weeks=weeks)
        self.assertEqual((rows[0]["target_min"], rows[0]["target_max"]), (74.5, 74.5))
        self.assertIsNone(rows[1]["target_min"])
        self.assertTrue(all(row["recorded_tss"] is None for row in rows))
        label = self.run_chart(
            "seasonWeekLoadLabel(seasonLoadSeries(weeks,horizon,'2026-01-18').rows[0])",
            weeks=weeks,
        )
        self.assertIn("Planned 75 TSS (Source target)", label)
        self.assertIn("No supported recorded TSS (1 ride without load)", label)

    def test_coach_budget_keeps_central_target_range_and_ceiling_separate(self):
        weeks = [
            {
                "start_date": "2026-01-05",
                "end_date": "2026-01-11",
                "planned_load": {
                    "estimated_tss": 441,
                    "estimated_tss_min": 400,
                    "estimated_tss_max": 480,
                    "estimated": False,
                    "tss_source": "coach_budget",
                    "budget_status": "provisional",
                    "budget_ceiling_tss": 5000,
                    "qualifier": "Coach budget · provisional",
                },
                "totals": {"estimated_tss": 375.4},
            },
            {
                "start_date": "2026-01-12",
                "end_date": "2026-01-18",
                "planned_load": {
                    "estimated_tss_min": 300,
                    "estimated_tss_max": 500,
                    "tss_source": "source_target",
                },
            },
            {
                "start_date": "2026-01-19",
                "end_date": "2026-01-25",
                "planned_load": {
                    "estimated_tss": 900,
                    "estimated_tss_min": 300,
                    "estimated_tss_max": 500,
                    "tss_source": "source_target",
                },
            },
        ]
        series = self.run_chart("seasonLoadSeries(weeks,horizon,'2026-01-18')", weeks=weeks)
        self.assertEqual([row["target_value"] for row in series["rows"]], [441, 400, None])
        self.assertEqual([len(run) for run in series["trajectory_runs"]], [2])
        label = self.run_chart(
            "seasonWeekLoadLabel(seasonLoadSeries(weeks,horizon,'2026-01-18').rows[0])",
            weeks=weeks,
        )
        self.assertIn("central estimate 441 TSS", label)
        self.assertIn("planning ceiling 5,000 TSS", label)
        self.assertEqual(series["rows"][0]["target_max"], 480)
        self.assertLess(series["max_tss"], 5000)

    def test_season_provenance_counts_explicit_budgets_and_incomplete_recorded_weeks(self):
        weeks = [
            {
                "start_date": "2026-01-05",
                "end_date": "2026-01-11",
                "planned_load": {
                    "estimated_tss": 400,
                    "estimated_tss_min": 380,
                    "estimated_tss_max": 420,
                    "estimated": False,
                    "tss_source": "coach_budget",
                    "budget_status": "provisional",
                },
                "totals": {"estimated_tss": 75.4},
                "tss_partial": True,
            },
            {
                "start_date": "2026-01-12",
                "end_date": "2026-01-18",
                "planned_load": {"estimated_tss": 0, "tss_source": "source_target"},
                "totals": {"estimated_tss": None},
            },
            {
                "start_date": "2026-01-19",
                "end_date": "2026-01-25",
                "planned_load": {
                    "estimated_tss": 100,
                    "tss_source": "complete_prescribed_sum",
                    "estimated": True,
                },
            },
            {
                "start_date": "2026-01-26",
                "end_date": "2026-02-01",
                "planned_load": {
                    "estimated_tss": 441,
                    "tss_source": "weekly_hours_budget",
                    "estimated": True,
                },
            },
        ]
        result = self.run_chart(
            "seasonLoadProvenance(seasonLoadSeries(weeks,horizon,'2026-01-18'))", weeks=weeks
        )
        self.assertEqual(
            {
                key: result[key]
                for key in (
                    "planned",
                    "source",
                    "prescribed",
                    "budget",
                    "provisional",
                    "missing",
                    "recorded",
                    "incomplete",
                )
            },
            {
                "planned": 3,
                "source": 1,
                "prescribed": 1,
                "budget": 1,
                "provisional": 1,
                "missing": 1,
                "recorded": 1,
                "incomplete": 1,
            },
        )
        self.assertIn("coach budgets", result["note"])
        self.assertIn("target range", result["note"])
        self.assertNotIn("weekly hours", result["note"])
        self.assertIn("not measured fitness", result["note"])

    def test_visible_chart_key_uses_ctl_atl_and_keeps_hours_in_week_totals(self):
        template = training_center.HTML_TEMPLATE
        for expected in (
            "CTL",
            "ATL",
            "42-day",
            "7-day",
            "TSS/day",
            "<span>Scheduled hours</span>",
            "<span>Recorded hours</span>",
        ):
            self.assertTrue(expected in template, expected)
        self.assertNotIn("<strong>Weekly TSS</strong>", template)

    def test_legacy_hours_and_rough_daily_totals_remain_chart_gaps(self):
        weeks = []
        for start, end, source, estimated in (
            ("2026-01-05", "2026-01-11", "weekly_hours_budget", True),
            ("2026-01-12", "2026-01-18", "session_if_forecast", True),
            ("2026-01-19", "2026-01-25", "complete_daily_sum", True),
            ("2026-01-26", "2026-02-01", "complete_prescribed_sum", True),
        ):
            weeks.append(
                {
                    "start_date": start,
                    "end_date": end,
                    "planned_load": {
                        "estimated_tss": 100,
                        "estimated_tss_min": 90,
                        "estimated_tss_max": 110,
                        "tss_source": source,
                        "estimated": estimated,
                    },
                    "totals": {"estimated_tss": 75},
                }
            )
        result = self.run_chart("seasonLoadSeries(weeks,horizon,'2026-02-08')", weeks=weeks)
        self.assertEqual([row["target_value"] for row in result["rows"]], [None, None, None, 100])
        self.assertEqual([row["recorded_tss"] for row in result["rows"]], [75, 75, 75, 75])
        self.assertEqual([len(run) for run in result["target_runs"]], [1])
        labels = self.run_chart(
            "seasonLoadSeries(weeks,horizon,'2026-02-08').rows.map(seasonWeekLoadLabel)",
            weeks=weeks,
        )
        self.assertTrue(all("No historical TSS target" in label for label in labels[:3]))
        self.assertTrue(all("TSS budget not set" not in label for label in labels[:3]))
        self.assertIn("Planned 90–110 TSS", labels[3])
        active_labels = self.run_chart(
            "seasonLoadSeries(weeks,horizon,'2026-01-12').rows.map(seasonWeekLoadLabel)",
            weeks=weeks,
        )
        self.assertIn("No historical TSS target", active_labels[0])
        self.assertTrue(all("TSS budget not set" in label for label in active_labels[1:3]))

    def test_browser_rounds_tss_without_rounding_incomplete_coverage_to_100(self):
        template = training_center.HTML_TEMPLATE
        start = template.index("    function formatTssNumber(")
        end = template.index("\n    function eventIsSkipped(", start)
        result = subprocess.run(
            [
                shutil.which("node"),
                "-e",
                template[start:end]
                + "\nconsole.log(JSON.stringify({tss:[74.5,75.4,75.5,0,null].map(formatTssNumber),coverage:formatCoverageNumber(99.9)}));",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(
            json.loads(result.stdout),
            {"tss": ["75", "75", "76", "0", "--"], "coverage": "99.9"},
        )


if __name__ == "__main__":
    unittest.main()
