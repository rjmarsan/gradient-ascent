import copy
import json
import shutil
import subprocess
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from gradient_ascent import training_center
from gradient_ascent.cli import _init_workspace
from gradient_ascent.storage import write_json
from gradient_ascent.training_load import build_training_load


def load_row(stamp, ctl, atl, **extra):
    return {
        "date": stamp,
        "ctl": ctl,
        "atl": atl,
        "tss_observed": 75.4,
        "load_applied": 75.4,
        "day_status": "complete",
        "history_days": 500,
        "seed_weight_ctl": 0.00001,
        "seed_weight_atl": 0,
        "history_incomplete": False,
        "recent_incomplete_days_42": 0,
        "recent_incomplete_days_7": 0,
        "to_date": False,
        **extra,
    }


def load_model(rows, *, through="2026-01-05", **extra):
    return {
        "method": "ctl_atl_daily_ewma_v1",
        "time_constants": {"ctl": 42, "atl": 7},
        "initialization": "zero_before_first_scored_day",
        "history_start": "2025-01-01",
        "through_date": through,
        "summary": {"available": bool(rows), "history_incomplete": False},
        "rows": rows,
        **extra,
    }


class PerformanceLoadPayloadTest(unittest.TestCase):
    def test_model_cutoff_uses_the_athletes_calendar_day(self):
        instant = datetime(2026, 1, 2, 0, 30, tzinfo=timezone.utc)
        self.assertEqual(
            training_center._athlete_today({"timezone": "America/Los_Angeles"}, now=instant),
            date(2026, 1, 1),
        )
        self.assertEqual(
            training_center._athlete_today({"timezone": "Asia/Tokyo"}, now=instant),
            date(2026, 1, 2),
        )
        self.assertEqual(
            training_center._athlete_today({"timezone": "not/a-zone"}, now=instant),
            instant.astimezone().date(),
        )
        self.assertEqual(
            training_center._athlete_today({}, now=instant), instant.astimezone().date()
        )

    def test_payload_uses_all_recorded_daily_history_not_weekly_budgets(self):
        daily = [
            {"date": "2025-12-31", "totals": {"estimated_tss": 100, "activity_count": 1}},
            {"date": "2026-01-02", "totals": {"estimated_tss": 50, "activity_count": 1}},
        ]
        weekly = [
            {
                "start_date": "2026-01-05",
                "end_date": "2026-01-11",
                "plan": {"days": {}, "tss_target": {"min": 1000, "max": 1000}},
                "totals": {"estimated_tss": None, "activity_count": 0},
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "athlete"
            _init_workspace(root, force=False)
            write_json(root / "derived" / "daily.json", daily)
            write_json(root / "derived" / "weekly.json", weekly)
            before = {
                name: (root / "derived" / name).read_bytes()
                for name in ("daily.json", "weekly.json")
            }
            with patch.object(
                training_center, "_athlete_today", return_value=date(2026, 1, 2), create=True
            ):
                payload, *_ = training_center._build_payload(root)
            self.assertEqual(
                payload["trainingLoad"], build_training_load(daily, as_of=date(2026, 1, 2))
            )
            self.assertEqual(payload["trainingLoad"]["history_start"], "2025-12-31")
            self.assertEqual(payload["weeks"][0]["planned_load"]["estimated_tss"], 1000)
            for name, original in before.items():
                self.assertEqual((root / "derived" / name).read_bytes(), original)

    def test_invalid_legacy_history_disables_only_the_analytic(self):
        valid = {"date": "2026-01-01", "totals": {"estimated_tss": 100}}
        cases = (
            [valid, valid],
            [{"date": "2026-01-01", "totals": {"estimated_tss": float("nan")}}],
            [{"date": "1800-01-01", "totals": {"estimated_tss": 100}}],
            [{"date": "2026-02-30", "totals": {"estimated_tss": 100}}],
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "athlete"
            _init_workspace(root, force=False)
            write_json(
                root / "derived" / "weekly.json",
                [{"start_date": "2026-01-05", "end_date": "2026-01-11", "plan": {"days": {}}}],
            )
            for daily in cases:
                with self.subTest(daily=daily):
                    write_json(root / "derived" / "daily.json", daily)
                    before = (root / "derived" / "daily.json").read_bytes()
                    with patch.object(
                        training_center, "_athlete_today", return_value=date(2026, 1, 2)
                    ):
                        payload, *_ = training_center._build_payload(root)
                    self.assertEqual(len(payload["weeks"]), 1)
                    self.assertEqual(payload["trainingLoad"]["rows"], [])
                    self.assertFalse(payload["trainingLoad"]["summary"]["available"])
                    self.assertEqual(
                        payload["trainingLoad"]["unavailable_reason"], "invalid_daily_history"
                    )
                    self.assertEqual((root / "derived" / "daily.json").read_bytes(), before)


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for chart regressions")
class PerformanceLoadChartTest(unittest.TestCase):
    def run_chart(self, expression, *, model=None, horizon=None, today="2026-01-05"):
        template = training_center.HTML_TEMPLATE
        source = template[
            template.index("    function seasonLoadSeries(") : template.index(
                "\n    function seasonHorizonLayout("
            )
        ]
        script = (
            "const escapeHtml=value=>String(value??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('\"','&quot;');\n"
            "const dayLabel=value=>value;\n"
            f"const model={json.dumps(model or load_model([]))};\n"
            f"const horizon={json.dumps(horizon or {'start_date': '2026-01-01', 'end_date': '2026-01-07'})};\n"
            f"const today={json.dumps(today)};\n"
            + source
            + f"\nconsole.log(JSON.stringify({expression}));"
        )
        result = subprocess.run(
            [shutil.which("node"), "-e", script],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_series_uses_precomputed_daily_values_and_exact_dates(self):
        model = load_model(
            [
                load_row("2026-01-04", 22.75, 35.125),
                load_row("2025-12-31", 9000, 9000),
                load_row("2026-01-01", 0, 0),
                load_row("2026-01-02", 20.25, 40.5),
                load_row("2026-01-06", 9999, 9999),
                load_row("2026-01-08", 9999, 9999),
            ],
            through="2026-01-04",
        )
        before = copy.deepcopy(model)
        result = self.run_chart("performanceLoadSeries(model,horizon,today)", model=model)
        self.assertEqual(
            [row["date"] for row in result["rows"]],
            ["2026-01-01", "2026-01-02", "2026-01-04"],
        )
        self.assertEqual([row["ctl"] for row in result["rows"]], [0, 20.25, 22.75])
        self.assertEqual([row["atl"] for row in result["rows"]], [0, 40.5, 35.125])
        self.assertEqual([len(run) for run in result["runs"]], [2, 1])
        self.assertEqual(result["rows"][0]["left"], 0)
        self.assertAlmostEqual(result["rows"][0]["center"], 100 * 0.5 / 7)
        self.assertAlmostEqual(result["rows"][-1]["right"], 100 * 4 / 7)
        self.assertEqual(result["through_date"], "2026-01-04")
        self.assertGreaterEqual(result["max_load"], 40.5)
        self.assertLess(result["max_load"], 9000)
        self.assertEqual(model, before)

    def test_invalid_values_break_lines_without_turning_missing_into_zero(self):
        model = load_model(
            [
                load_row("2026-01-01", 0, 0),
                load_row("2026-01-02", None, 50),
                load_row("2026-01-03", 10, True),
                load_row("2026-01-04", -1, 20),
                load_row("2026-01-05", 15, 25),
                load_row("2026-02-30", 9000, 9000),
            ]
        )
        result = self.run_chart("performanceLoadSeries(model,horizon,today)", model=model)
        self.assertEqual([len(run) for run in result["runs"]], [1, 1])
        self.assertEqual(result["runs"][0][0]["ctl"], 0)
        rendered = self.run_chart(
            "renderPerformanceLoadChart(performanceLoadSeries(model,horizon,today))", model=model
        )
        self.assertIn('class="season-ctl-area"', rendered)
        self.assertIn('class="season-ctl-line"', rendered)
        self.assertIn('class="season-atl-line"', rendered)
        self.assertNotIn("NaN", rendered)
        self.assertNotIn("Infinity", rendered)

    def test_chart_keeps_units_quality_and_recorded_only_cutoff(self):
        model = load_model(
            [
                load_row("2026-01-01", 10.25, 20.75),
                load_row(
                    "2026-01-02",
                    11.125,
                    23.75,
                    tss_observed=None,
                    load_applied=0,
                    day_status="missing",
                    history_days=2,
                    seed_weight_ctl=0.95,
                    seed_weight_atl=0.73,
                    history_incomplete=True,
                    recent_incomplete_days_42=1,
                    recent_incomplete_days_7=1,
                ),
            ],
            through="2026-01-02",
        )
        rendered, selected, future = self.run_chart(
            "[renderPerformanceLoadChart(performanceLoadSeries(model,horizon,today)),"
            "performanceLoadSelectionLabel(performanceLoadSeries(model,horizon,today),'2026-01-02'),"
            "performanceLoadSelectionLabel(performanceLoadSeries(model,horizon,today),'2026-01-07')]",
            model=model,
        )
        for expected in ("CTL", "ATL", "TSS/day", "11.1", "23.8", "Incomplete history"):
            self.assertIn(expected, rendered)
        self.assertIn("No supported recorded TSS", rendered)
        self.assertIn("Zero-seed", rendered)
        self.assertIn("2026-01-02", selected)
        self.assertIn("Incomplete history", selected)
        self.assertIn("2026-01-02", future)
        self.assertIn("daily TSS", future)
        self.assertNotIn('class="season-target-band"', rendered)
        self.assertNotIn('class="season-recorded-area"', rendered)
        self.assertNotIn("Weekly TSS", rendered)

    def test_empty_old_or_out_of_year_data_never_fabricates_curves(self):
        examples = (
            {},
            load_model([]),
            load_model([load_row("2025-12-31", 50, 60)]),
            load_model([load_row("2026-01-01", 50, 60)], through="not-a-date"),
            load_model([load_row("2026-01-01", 50, 60)], method="weekly_tss"),
        )
        for model in examples:
            with self.subTest(model=model.get("method")):
                rendered = self.run_chart(
                    "renderPerformanceLoadChart(performanceLoadSeries(model,horizon,today))",
                    model=model,
                )
                self.assertIn("No recorded CTL/ATL", rendered)
                self.assertNotIn('class="season-ctl-area"', rendered)
                self.assertNotIn('class="season-atl-line"', rendered)

    def test_live_day_cannot_extend_a_stale_model_or_plot_future_rows(self):
        model = load_model(
            [load_row("2026-01-02", 30, 40), load_row("2026-01-03", 100, 100)],
            through="2026-01-02",
        )
        stale = self.run_chart("performanceLoadSeries(model,horizon,today)", model=model)
        self.assertEqual([row["date"] for row in stale["rows"]], ["2026-01-02"])
        self.assertEqual(stale["through_date"], "2026-01-02")
        clock_behind = self.run_chart(
            "performanceLoadSeries(model,horizon,today)", model=model, today="2026-01-01"
        )
        self.assertEqual(clock_behind["rows"], [])

    def test_invalid_history_has_an_actionable_unavailable_state(self):
        model = load_model([], unavailable_reason="invalid_daily_history")
        rendered, selected = self.run_chart(
            "[renderPerformanceLoadChart(performanceLoadSeries(model,horizon,today)),"
            "performanceLoadSelectionLabel(performanceLoadSeries(model,horizon,today),'2026-01-02')]",
            model=model,
        )
        for value in (rendered, selected):
            self.assertIn("CTL/ATL unavailable", value)
            self.assertIn("rebuild local insights", value)
        self.assertNotIn('class="season-ctl-line"', rendered)


if __name__ == "__main__":
    unittest.main()
