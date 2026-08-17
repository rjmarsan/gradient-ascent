from pathlib import Path
import json
import tempfile
import unittest

from gradient_ascent.calendar import ingest_calendar
from gradient_ascent.plan import build_plan_from_csv


class PlanImportTest(unittest.TestCase):
    def test_daily_import_preserves_numeric_minutes_and_explicit_load_ranges(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "daily.csv"
            source.write_text(
                "Date,Workout,Duration (min),Planned TSS\n"
                "2026-09-01,First session,60,40-50\n"
                "2026-09-01,Second session,30,20\n"
                "2026-09-02,Rest,0,0\n"
                "2026-09-03,Unknown,,\n"
                "2026-09-03,Known component,30,25\n"
                "2026-09-04,Large range,600-1200,100\n"
                "2026-09-04,Exceeds day,600,100\n",
                encoding="utf-8",
            )
            build_plan_from_csv(source, root / "plan")
            week = json.loads((root / "plan/weeks.json").read_text())[0]
        self.assertIn("60 min", week["days"]["Tue"])
        self.assertEqual(week["day_loads"]["Tue"], {
            "hours_min": 1.5, "hours_max": 1.5, "tss_min": 60.0, "tss_max": 70.0,
        })
        self.assertEqual(week["day_loads"]["Wed"], {
            "hours_min": 0.0, "hours_max": 0.0, "tss_min": 0.0, "tss_max": 0.0,
        })
        self.assertEqual(week["day_loads"]["Thu"], {
            "hours_min": None, "hours_max": None, "tss_min": None, "tss_max": None,
        })
        self.assertIsNone(week["day_loads"]["Fri"]["hours_min"])
        self.assertIsNone(week["day_loads"]["Fri"]["hours_max"])

    def test_weekly_import_preserves_explicit_tss_target_and_rejects_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "weekly.csv"
            source.write_text(
                "Week,Hours Target,TSS Target,Mon\n"
                "2026-09-07 – 2026-09-13,5h-7h,300-450,Rest\n"
                "2026-09-14 – 2026-09-20,4h,-20,Rest\n",
                encoding="utf-8",
            )
            build_plan_from_csv(source, root / "plan")
            weeks = json.loads((root / "plan/weeks.json").read_text())
        self.assertEqual(weeks[0]["tss_target"], {"min": 300.0, "max": 450.0})
        self.assertEqual(weeks[0]["hours_target"], {"min": 5.0, "max": 7.0})
        self.assertEqual(weeks[1]["tss_target"], {"min": None, "max": None})

    def test_daily_workout_rows_are_grouped_into_weekly_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "daily-plan.csv"
            csv_path.write_text(
                "Date,Workout,Duration,Phase,Notes\n"
                "2026-07-06,Endurance ride,90 min,Base,Keep it conversational\n"
                "2026-07-08,Threshold intervals,75 min,Base,3 x 10 minutes\n"
                "2026-07-13,Recovery spin,45 min,Build,Easy gearing\n",
                encoding="utf-8",
            )

            result = build_plan_from_csv(csv_path, root / "plan")
            weeks = json.loads((root / "plan" / "weeks.json").read_text(encoding="utf-8"))

        self.assertEqual(result["weeks"], 2)
        self.assertEqual(weeks[0]["days"]["Mon"], "Endurance ride (90 min) — Keep it conversational")
        self.assertEqual(weeks[0]["days"]["Wed"], "Threshold intervals (75 min) — 3 x 10 minutes")
        self.assertEqual(weeks[1]["phase"], "Build")

    def test_unrecognized_plan_layout_does_not_overwrite_existing_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "unsupported.csv"
            csv_path.write_text("Thing,Value\nfoo,bar\n", encoding="utf-8")
            out_dir = root / "plan"
            out_dir.mkdir()
            existing = '[{"id":"keep-me"}]'
            (out_dir / "weeks.json").write_text(existing, encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "supported weekly or daily training-plan layout"):
                build_plan_from_csv(csv_path, out_dir)
            self.assertEqual((out_dir / "weeks.json").read_text(encoding="utf-8"), existing)

    def test_unrecognized_calendar_layout_does_not_overwrite_existing_calendar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "unsupported.csv"
            csv_path.write_text("Thing,Value\nfoo,bar\n", encoding="utf-8")
            output_path = root / "calendar.json"
            existing = '{"weeks":[{"keep":true}]}'
            output_path.write_text(existing, encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "weekly calendar layout"):
                ingest_calendar(csv_path, output_path)
            self.assertEqual(output_path.read_text(encoding="utf-8"), existing)

    def test_plan_import_preserves_completed_onboarding_profile(self) -> None:
        csv_path = Path("examples/calendar/sample-training-calendar.csv")
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "plan"
            out_dir.mkdir(parents=True)
            existing = {
                "display_name": "",
                "timezone": "Europe/Paris",
                "unit_system": "metric",
                "disciplines": ["road", "commuting"],
                "experience_level": "recreational",
                "weekly_availability": "4-6 hours",
                "constraints": ["weekday rides under 90 minutes"],
                "sensors": ["heart_rate"],
                "ftp_w": 300,
            }
            (out_dir / "athlete.json").write_text(json.dumps(existing), encoding="utf-8")

            build_plan_from_csv(csv_path, out_dir)
            athlete = json.loads((out_dir / "athlete.json").read_text(encoding="utf-8"))

        self.assertEqual(athlete["timezone"], "Europe/Paris")
        self.assertEqual(athlete["unit_system"], "metric")
        self.assertEqual(athlete["disciplines"], ["road", "commuting"])
        self.assertEqual(athlete["ftp_w"], 300)

    def test_daily_plan_import_preserves_onboarding_events(self) -> None:
        csv_path = Path("examples/calendar/sample-daily-plan.csv")
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "plan"
            out_dir.mkdir(parents=True)
            existing = {
                "id": "2026-09-19-front-range-gravel-century-gravel",
                "date": "2026-09-19",
                "name": "Front Range Gravel Century",
                "discipline": "gravel",
                "priority": "A",
                "week_id": "2026-09-14",
            }
            (out_dir / "events.json").write_text(json.dumps([existing]), encoding="utf-8")

            result = build_plan_from_csv(csv_path, out_dir)
            events = json.loads((out_dir / "events.json").read_text(encoding="utf-8"))

        self.assertEqual(result["events"], 1)
        self.assertEqual(events, [existing])

    def test_build_plan_imports_event_markers_and_locations(self) -> None:
        csv_path = Path("examples/calendar/sample-training-calendar.csv")
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "plan"

            result = build_plan_from_csv(csv_path, out_dir)

            self.assertEqual(result["weeks"], 4)
            events = (out_dir / "events.json").read_text(encoding="utf-8")
            self.assertIn("Spring Road Race", events)
            self.assertIn("Girona ES", events)
            self.assertIn('"commitment": true', events)
            self.assertIn('"team_priority": true', events)
            self.assertIn("Community Criterium", events)
            self.assertIn('"maybe": true', events)
            self.assertIn("Regional Gravel", events)
            self.assertIn('"skip": true', events)

    def test_plan_import_accepts_iso_ranges_and_generic_cycling_event_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "plan.csv"
            csv_path.write_text(
                "Week,Phase,Primary Focus,Hours Target,Mon,Tue,Wed,Thu,Fri,Sat,Sun,Events,MTB\n"
                "2026-07-06 – 2026-07-12,Base,Consistency,6-8h,OFF,Z2,Z2,Tempo,OFF,Long ride,Easy,"
                '"[commit] 2026-07-11 Community Fondo - Utrecht NL",'
                '"[maybe] 07/12/26 Trail Race - Zeist NL"\n',
                encoding="utf-8",
            )
            out_dir = root / "out"

            calendar_result = ingest_calendar(csv_path, root / "calendar.json")
            result = build_plan_from_csv(csv_path, out_dir)
            weeks = json.loads((out_dir / "weeks.json").read_text(encoding="utf-8"))
            events = json.loads((out_dir / "events.json").read_text(encoding="utf-8"))

        self.assertEqual(calendar_result["weeks"], 1)
        self.assertEqual(result["weeks"], 1)
        self.assertEqual(weeks[0]["start_date"], "2026-07-06")
        self.assertEqual(weeks[0]["end_date"], "2026-07-12")
        self.assertEqual({event["discipline"] for event in events}, {"Cycling", "MTB"})
        self.assertEqual({event["date"] for event in events}, {"2026-07-11", "2026-07-12"})


if __name__ == "__main__":
    unittest.main()
