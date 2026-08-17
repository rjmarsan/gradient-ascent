import json
from datetime import date, timedelta
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from gradient_ascent import training_center
from gradient_ascent.cli import _init_workspace


class TrainingCenterPolishTest(unittest.TestCase):
    def test_activity_titles_use_plan_without_unmasking_private_names(self) -> None:
        plan = "Endurance with four relaxed sprints"
        cases = (
            ({"name": "405588560", "source_activity_id": "405588560"}, plan),
            ({"name": "08/14/26"}, plan),
            ({"name": "Private ride"}, plan),
            ({"name": "Hidden route near home", "private": True}, plan),
            ({"name": "A real ride title"}, "A real ride title"),
            ({"name": "405588560", "name_is_authored": True}, "405588560"),
        )
        for activity, expected in cases:
            with self.subTest(activity=activity):
                original = dict(activity)
                self.assertEqual(
                    training_center._activity_label(activity, planned_name=plan), expected
                )
                self.assertEqual(activity, original)

    def test_legacy_provider_name_mismatch_does_not_make_placeholders_authored(self) -> None:
        plan = "Steady endurance"
        raw = {
            "source_provider": "ridewithgps",
            "source_activity_id": "765432100",
            "source_provider_name": "A newer provider title",
        }
        for name in ("765432100", "08/14/26", "Private ride", "765432100.tcx"):
            with self.subTest(name=name):
                activity = {"name": name, "raw": dict(raw)}
                self.assertEqual(
                    training_center._activity_title_info(activity, planned_name=plan),
                    (plan, True),
                )
                explicit = {**activity, "raw": {**raw, "name_is_authored": True}}
                self.assertEqual(
                    training_center._activity_title_info(explicit, planned_name=plan),
                    (name, False),
                )
        meaningful = {"name": "My favorite recovery loop", "raw": raw}
        self.assertEqual(
            training_center._activity_title_info(meaningful, planned_name=plan),
            (meaningful["name"], False),
        )

    def test_plan_fallback_reaches_overview_and_lazy_activity_details(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            _init_workspace(workspace, force=False)
            activity_id = "recording:recording-" + "a" * 64
            plan = "Z2 endurance, 90 minutes"
            activity = {
                "id": activity_id,
                "provider_id": "recording-" + "a" * 64,
                "name": "405588560",
                "source": {"provider": "recording"},
                "source_provider": "ridewithgps",
                "source_activity_id": "405588560",
                "name_is_authored": False,
                "start_date_local": "2026-08-15T08:00:00-07:00",
                "moving_time_s": 5400,
                "is_meaningful_ride": True,
            }
            totals = {"activity_count": 1, "moving_time_s": 5400}
            weekly = [
                {
                    "start_date": "2026-08-10",
                    "end_date": "2026-08-16",
                    "plan": {"days": {"Sat": plan}},
                    "activity_ids": [activity_id],
                    "totals": totals,
                }
            ]
            daily = [{"date": "2026-08-15", "activity_ids": [activity_id], "totals": totals}]
            for name, value in (("weekly", weekly), ("daily", daily), ("activities", [activity])):
                (workspace / "derived" / f"{name}.json").write_text(
                    json.dumps(value), encoding="utf-8"
                )
            original = (workspace / "derived" / "activities.json").read_bytes()

            payload, ids, activities, annotations = training_center._build_payload(workspace)
            day = next(item for item in payload["days"] if item["date"] == "2026-08-15")
            details = training_center._build_week_activity_details(
                ids["2026-08-10"], activities, annotations, workspace
            )["2026-08-15"]

            self.assertEqual(day["actual"], plan)
            self.assertTrue(day["actual_title_from_plan"])
            self.assertEqual(day["activities"][0]["name"], plan)
            self.assertTrue(day["activities"][0]["name_from_plan"])
            self.assertEqual(details[0]["name"], plan)
            self.assertEqual(details[0]["source_url"], "https://ridewithgps.com/trips/405588560")
            self.assertEqual((workspace / "derived" / "activities.json").read_bytes(), original)

    def test_week_horizon_has_real_phases_and_explicit_selected_range(self) -> None:
        html = training_center.HTML_TEMPLATE
        for removed in ("profile-peak", "renderNextEventNotice", "week-event-notice"):
            self.assertFalse(removed in html, removed)
        for added in (
            'class="season-selected-range"',
            "Shown week",
            "data-season-phase",
            "function seasonHorizonLayout(",
            'event.key === "ArrowLeft"',
        ):
            self.assertTrue(added in html, added)

    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for the browser date-layout test")
    def test_horizon_geometry_uses_inclusive_dates_and_moves_only_the_selection(self) -> None:
        html = training_center.HTML_TEMPLATE
        start = html.index("    function seasonHorizonLayout(")
        end = html.index("\n    function renderSeasonHorizon(", start)
        cases = [
            {"start_date": "2026-01-26", "end_date": "2026-02-01"},
            {"start_date": "2026-02-23", "end_date": "2026-03-01"},
        ]
        phases = [
            {"name": "Build", "start_date": "2026-02-01", "end_date": "2026-02-28"},
            {"name": "Base", "start_date": "2026-01-01", "end_date": "2026-01-31"},
            {"name": "Invalid", "start_date": "2026-02-30", "end_date": "2026-03-01"},
        ]
        script = html[start:end] + (
            f"\nconst phases = {json.dumps(phases)};"
            f"\nconst weeks = {json.dumps(cases)};"
            "\nconsole.log(JSON.stringify(weeks.map(week => "
            "seasonHorizonLayout(phases, week, week.start_date, '2026-01-26'))));"
        )
        result = subprocess.run(
            [shutil.which("node"), "-e", script],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        first, last = json.loads(result.stdout)
        self.assertEqual((first["start_date"], first["end_date"]), ("2026-01-01", "2026-02-28"))
        self.assertEqual([phase["name"] for phase in first["phases"]], ["Base", "Build"])
        self.assertEqual(first["phases"], last["phases"])
        self.assertAlmostEqual(first["selection"]["left"], 100 * 25 / 59)
        self.assertAlmostEqual(first["selection"]["width"], 100 * 7 / 59)
        self.assertAlmostEqual(last["selection"]["width"], 100 * 6 / 59)
        self.assertAlmostEqual(sum(month["width"] for month in first["months"]), 100)

    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for the horizon rendering test")
    def test_dense_horizon_has_quiet_phase_marks_and_a_today_control(self) -> None:
        phases = []
        for index in range(43):
            start = date(2026, 1, 5) + timedelta(days=7 * index)
            phases.append(
                {
                    "name": f"A complete and deliberately long build phase {index + 1}",
                    "start_date": start.isoformat(),
                    "end_date": (start + timedelta(days=6)).isoformat(),
                }
            )
        template = training_center.HTML_TEMPLATE
        source = template[
            template.index("    function phaseTone(") : template.index(
                "\n    function renderWeekSelect("
            )
        ]
        script = (
            f"const DATA = {json.dumps({'phases': phases, 'weeks': phases, 'days': [{'date': '2026-08-17', 'events': []}]})};\n"
            "const state = {selectedDate: '2026-08-16'};\n"
            "const TODAY = '2026-08-17';\n"
            "const dayLabel = value => value;\n"
            "const utcDate = value => new Date(value + 'T00:00:00Z');\n"
            "const escapeHtml = value => String(value);\n"
            "const eventIsSkipped = () => false;\n"
            "const todayAnchorDate = () => TODAY;\n"
            "const dayByDate = value => DATA.days.find(day => day.date === value);\n"
            + source
            + "\nconsole.log(JSON.stringify(renderSeasonHorizon({start_date:'2026-08-10',end_date:'2026-08-16',phase:'Current build phase'})));"
        )
        rendered = json.loads(
            subprocess.run(
                [shutil.which("node"), "-e", script],
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            ).stdout
        )
        segments = re.findall(
            r'<div class="season-phase [^"]*"[^>]*>(.*?)</div>', rendered, re.DOTALL
        )
        self.assertEqual(len(segments), 43)
        self.assertTrue(all(not text.strip() for text in segments))
        self.assertIn(phases[0]["name"], rendered)
        self.assertIn("data-season-today", rendered)
        self.assertIn('class="season-today-marker"', rendered)
        self.assertIn('aria-valuetext="2026-08-10–2026-08-16 · Current build phase"', rendered)

    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for the horizon date test")
    def test_today_marker_is_independent_and_never_clamped_into_the_horizon(self) -> None:
        template = training_center.HTML_TEMPLATE
        source = template[
            template.index("    function seasonHorizonLayout(") : template.index(
                "\n    function renderSeasonHorizon("
            )
        ]
        phases = [{"name": "Build", "start_date": "2026-08-01", "end_date": "2026-08-31"}]
        script = source + (
            f"\nconst phases = {json.dumps(phases)};"
            "\nconst week = {start_date:'2026-08-10',end_date:'2026-08-16'};"
            "\nconst next = {start_date:'2026-08-17',end_date:'2026-08-23'};"
            "\nconsole.log(JSON.stringify(["
            "seasonHorizonLayout(phases,week,'2026-08-16','2026-08-17'),"
            "seasonHorizonLayout(phases,next,'2026-08-20','2026-08-17'),"
            "seasonHorizonLayout(phases,week,'2026-08-16','2026-07-31'),"
            "seasonHorizonLayout(phases,week,'2026-08-16','2026-09-01'),"
            "seasonHorizonLayout(phases,week,'2026-08-16','2026-02-30')"
            "]));"
        )
        first, moved, before, after, invalid = json.loads(
            subprocess.run(
                [shutil.which("node"), "-e", script],
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            ).stdout
        )
        self.assertEqual(first["today_marker"], moved["today_marker"])
        self.assertEqual(first["today_marker"]["date"], "2026-08-17")
        self.assertAlmostEqual(first["today_marker"]["left"], 100 * 16 / 31)
        self.assertNotEqual(first["selection"], moved["selection"])
        self.assertEqual(first["marker"]["date"], "2026-08-16")
        for outside in (before, after, invalid):
            self.assertIsNone(outside["today_marker"])

    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for the horizon date test")
    def test_out_of_season_selection_does_not_create_a_false_edge_marker(self) -> None:
        template = training_center.HTML_TEMPLATE
        source = template[
            template.index("    function seasonHorizonLayout(") : template.index(
                "\n    function renderSeasonHorizon("
            )
        ]
        script = source + (
            "\nconst phases=[{name:'Build',start_date:'2026-08-01',end_date:'2026-08-31'}];"
            "\nconsole.log(JSON.stringify(["
            "seasonHorizonLayout(phases,{start_date:'2026-07-20',end_date:'2026-07-26'},'2026-07-25','2026-08-17'),"
            "seasonHorizonLayout(phases,{start_date:'2026-09-07',end_date:'2026-09-13'},'2026-09-09','2026-08-17'),"
            "seasonHorizonLayout(phases,{start_date:'2026-07-27',end_date:'2026-08-02'},'2026-07-31','2026-08-17'),"
            "seasonHorizonLayout(phases,{start_date:'2026-07-27',end_date:'2026-08-02'},'2026-08-01','2026-08-17')"
            "]));"
        )
        before, after, partial, boundary = json.loads(
            subprocess.run(
                [shutil.which("node"), "-e", script],
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            ).stdout
        )
        for outside in (before, after):
            self.assertEqual(outside["selection"]["width"], 0)
            self.assertIsNone(outside["marker"])
        self.assertAlmostEqual(partial["selection"]["width"], 100 * 2 / 31)
        self.assertIsNone(partial["marker"])
        self.assertEqual(boundary["marker"]["date"], "2026-08-01")
        self.assertEqual(boundary["marker"]["left"], 0)

    def test_day_brief_can_wrap_without_a_fixed_duration_column(self) -> None:
        html = training_center.HTML_TEMPLATE
        for added in (
            'class="session-copy"',
            'class="session-duration"',
            "body.primary-shell .coach-rail .session-duration",
            "body.primary-shell .coach-rail .section-title-row",
        ):
            self.assertTrue(added in html, added)

    def test_estimated_and_partial_load_are_explicit_in_activity_and_day_labels(self) -> None:
        activity = {
            "id": "recording:synthetic",
            "source": {"provider": "recording"},
            "estimated_tss": 42.1,
            "estimated_tss_source": "estimated_power_stream",
            "weighted_average_watts": 210,
            "weighted_average_watts_source": "estimated_power_stream",
            "power_load_estimate": {"scope": "recorded_power", "coverage_ratio": 0.793},
        }
        detail = training_center._activity_detail(activity, {}, Path("/tmp"), include_heavy=False)
        self.assertEqual(detail["tss_label"], "42.1 TSS")
        self.assertTrue(detail["tss_partial"])
        self.assertIn("79.3%", detail["tss_description"])
        self.assertEqual(detail["tss_qualifier"], "Calculated · 79.3% power coverage")
        self.assertIn("configured FTP", detail["tss_description"])
        self.assertEqual(detail["np_label"], "~210 NP")
        metrics = training_center._day_metrics(
            {
                "totals": {
                    "activity_count": 3,
                    "estimated_tss": 120,
                    "estimated_tss_activity_count": 2,
                    "estimated_tss_estimated_activity_count": 1,
                    "estimated_tss_partial_activity_count": 1,
                }
            }
        )
        self.assertEqual(metrics["tss_label"], "120 TSS")
        self.assertTrue(metrics["tss_partial"])
        self.assertIn("recorded power is missing", metrics["tss_description"])
        source = training_center._activity_detail(
            {**activity, "estimated_tss": 0, "estimated_tss_source": "source"},
            {},
            Path("/tmp"),
            include_heavy=False,
        )
        self.assertEqual(source["tss_label"], "0 TSS")
        self.assertFalse(source["tss_partial"])
        for invalid in (float("nan"), float("inf"), True):
            with self.subTest(invalid=invalid):
                missing = training_center._day_metrics(
                    {
                        "totals": {
                            "estimated_tss": invalid,
                            "estimated_tss_estimated_activity_count": invalid,
                        }
                    }
                )
                self.assertIsNone(missing["tss_label"])


if __name__ == "__main__":
    unittest.main()
