import json
import shutil
import subprocess
import unittest

from gradient_ascent import training_center


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for chart regressions")
class WeekLoadChartTest(unittest.TestCase):
    def run_chart(self, expression, week=None):
        template = training_center.HTML_TEMPLATE
        source = template[
            template.index("    function pointString(") : template.index(
                "\n    function powerZoneClass("
            )
        ]
        script = (
            "const TODAY='2026-08-14';\n"
            f"const week={json.dumps(week or {})};\n"
            + source
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

    def test_unknown_cumulative_values_never_become_zero_or_shift_dates(self):
        self.assertEqual(self.run_chart("cumulative([0,40,null,20])"), [0, 40, None, None])
        self.assertEqual(self.run_chart("cumulative([null,40])"), [None, None])
        week = {
            "days": [
                {
                    "date": "2026-08-10",
                    "planned_load": {"estimated_tss": 0},
                    "metrics": {"activity_count": 0, "tss_missing_activity_count": 0},
                },
                {
                    "date": "2026-08-11",
                    "planned_load": {"estimated_tss": 40},
                    "metrics": {
                        "activity_count": 1,
                        "estimated_tss": 40,
                        "tss_missing_activity_count": 0,
                    },
                },
                {
                    "date": "2026-08-12",
                    "planned_load": {"estimated_tss": None},
                    "metrics": {"activity_count": 1, "tss_missing_activity_count": 0},
                },
                {
                    "date": "2026-08-13",
                    "planned_load": {"estimated_tss": 20},
                    "metrics": {"activity_count": 1, "tss_missing_activity_count": 1},
                },
                {
                    "date": "2026-08-14",
                    "planned_load": {"estimated_tss": 20},
                    "metrics": {
                        "activity_count": 1,
                        "estimated_tss": 30,
                        "tss_missing_activity_count": 0,
                    },
                },
                {
                    "date": "2026-08-15",
                    "planned_load": {"estimated_tss": 50},
                    "metrics": {"activity_count": 0},
                },
            ]
        }
        result = self.run_chart("weekLoadSeries(week)", week)
        self.assertEqual(result["planned"], [0, 40, None, None, None, None])
        self.assertEqual(result["actual"], [0, 40, 40, None, None, None])
        self.assertEqual(result["totalPoints"], 6)
        self.assertIn("unspecified", result["note"])
        self.assertIn("unsupported", result["note"])

    def test_partial_power_stays_a_labeled_recorded_only_sum(self):
        week = {
            "tss_qualifier": "Calculated · 79% power coverage",
            "days": [
                {
                    "date": "2026-08-10",
                    "planned_load": {"estimated_tss": 80},
                    "metrics": {
                        "activity_count": 1,
                        "estimated_tss": 42.1,
                        "tss_missing_activity_count": 0,
                        "tss_power_incomplete": True,
                    },
                },
            ],
        }
        result = self.run_chart("weekLoadSeries(week)", week)
        self.assertEqual(result["actual"], [42.1])
        self.assertIn("79% power coverage", result["note"])

    def test_empty_chart_has_no_fake_dot_and_null_points_keep_their_positions(self):
        empty = self.run_chart("renderLoadSvg([null,null],{plannedValues:[null,null]})")
        self.assertNotIn('class="chart-dot"', empty)
        self.assertNotIn('class="actual-area"', empty)
        self.assertNotIn('class="planned-line"', empty)
        rendered = self.run_chart(
            "renderLoadSvg([0,40,null,null],{width:100,height:100,pad:10,totalPoints:4,plannedValues:[0,50,null,null]})"
        )
        self.assertIn('aria-label="Cumulative recorded and planned TSS chart"', rendered)
        self.assertIn("36.7,", rendered)
        self.assertNotIn("NaN", rendered)


if __name__ == "__main__":
    unittest.main()
