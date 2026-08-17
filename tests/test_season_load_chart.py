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
                "target_hours": {"min": 90, "max": 99},
                "actual_hours": 90,
                "totals": {"activity_count": 1},
            },
            {
                "start_date": "2026-01-26",
                "end_date": "2026-02-01",
                "target_hours": {"min": 8, "max": 10},
                "actual_hours": 0,
                "totals": {"activity_count": 0},
            },
            {
                "start_date": "2026-01-05",
                "end_date": "2026-01-11",
                "target_hours": {"min": 6, "max": 9},
                "actual_hours": 7.5,
                "totals": {"activity_count": 3},
            },
            {
                "start_date": "2026-01-12",
                "end_date": "2026-01-18",
                "target_hours": {"min": None, "max": None},
                "actual_hours": 0,
                "totals": {"activity_count": 0},
            },
            {
                "start_date": "2026-01-19",
                "end_date": "2026-01-25",
                "target_hours": {"min": 0, "max": 0},
                "actual_hours": 0,
                "totals": {"activity_count": 1},
            },
            {
                "start_date": "2026-02-02",
                "end_date": "2026-02-08",
                "target_hours": {"min": 9, "max": 12},
                "actual_hours": 4,
                "totals": {"activity_count": 1},
            },
        ]
        result = self.run_chart("seasonLoadSeries(weeks,horizon,'2026-01-28')", weeks=weeks)
        rows = result["rows"]
        self.assertEqual(
            [row["start_date"] for row in rows],
            ["2026-01-05", "2026-01-12", "2026-01-19", "2026-01-26", "2026-02-02"],
        )
        self.assertEqual([row["recorded_hours"] for row in rows], [7.5, None, 0, None, None])
        self.assertEqual([row["target_min"] for row in rows], [6, None, 0, 8, 9])
        self.assertAlmostEqual(rows[0]["left"], 0)
        self.assertAlmostEqual(rows[0]["right"], 20)
        self.assertAlmostEqual(rows[-1]["right"], 100)
        self.assertTrue(rows[3]["to_date"])
        self.assertFalse(rows[4]["to_date"])
        self.assertEqual([len(run) for run in result["target_runs"]], [1, 3])
        self.assertEqual([len(run) for run in result["recorded_runs"]], [1, 1])
        self.assertLess(result["max_hours"], 90)

    def test_chart_is_a_real_area_with_range_band_and_value_tooltips(self):
        weeks = [
            {
                "start_date": "2026-01-05",
                "end_date": "2026-01-11",
                "target_hours": {"min": 6, "max": 9},
                "actual_hours": 7.5,
                "totals": {"activity_count": 3},
            },
            {
                "start_date": "2026-01-12",
                "end_date": "2026-01-18",
                "target_hours": {"min": 8, "max": 11},
                "actual_hours": 9,
                "totals": {"activity_count": 4},
            },
        ]
        rendered = self.run_chart(
            "renderSeasonLoadChart(seasonLoadSeries(weeks,horizon,'2026-01-18'))", weeks=weeks
        )
        self.assertIn('class="season-load-chart"', rendered)
        self.assertIn('class="season-target-band"', rendered)
        self.assertIn('class="season-recorded-area"', rendered)
        self.assertIn('class="season-recorded-line"', rendered)
        self.assertIn("Scheduled 6–9 h", rendered)
        self.assertIn("Recorded 7.5 h", rendered)
        self.assertIn("Weekly moving hours", rendered)
        self.assertNotIn("profile-peak", rendered)
        self.assertNotIn("NaN", rendered)
        self.assertNotIn("Infinity", rendered)

    def test_missing_chart_has_no_fabricated_area_and_ranges_are_clipped(self):
        empty = self.run_chart(
            "renderSeasonLoadChart(seasonLoadSeries(weeks,horizon,'2026-01-18'))"
        )
        self.assertIn("No weekly hours data", empty)
        self.assertNotIn('class="season-recorded-area"', empty)
        self.assertNotIn('class="season-target-band"', empty)
        weeks = [
            {
                "start_date": "2026-01-01",
                "end_date": "2026-01-07",
                "target_hours": {"min": 2, "max": 4},
                "actual_hours": 3,
                "totals": {"activity_count": 1},
            },
            {
                "start_date": "2026-02-05",
                "end_date": "2026-02-11",
                "target_hours": {"min": 8, "max": 6},
                "actual_hours": -1,
                "totals": {"activity_count": 1},
            },
        ]
        rows = self.run_chart("seasonLoadSeries(weeks,horizon,'2026-02-08').rows", weeks=weeks)
        self.assertEqual(rows[0]["left"], 0)
        self.assertAlmostEqual(rows[0]["right"], 100 * 3 / 35)
        self.assertEqual(rows[-1]["right"], 100)
        self.assertIsNone(rows[-1]["target_min"])
        self.assertIsNone(rows[-1]["recorded_hours"])


if __name__ == "__main__":
    unittest.main()
