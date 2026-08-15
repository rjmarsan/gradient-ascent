import json
import tempfile
import unittest
from pathlib import Path

from gradient_ascent.cli import _init_workspace
from gradient_ascent.onboarding import (
    add_onboarding_event,
    onboarding_status,
    set_onboarding_choice,
    set_onboarding_goals,
    set_onboarding_profile,
)
from gradient_ascent.storage import write_json
from gradient_ascent.training_center import build_training_center


class OnboardingTest(unittest.TestCase):
    def test_prompt_assets_use_compact_resumable_onboarding(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        skill = (repo_root / "skills" / "gradient-ascent" / "SKILL.md").read_text(encoding="utf-8")
        prompt_path = repo_root / "SETUP_PROMPT.md"

        self.assertIn("onboarding-status --json", skill)
        self.assertIn("onboarding-profile", skill)
        self.assertIn("onboarding-goals", skill)
        self.assertIn("onboarding-event", skill)
        self.assertIn("import-activity-recording", skill)
        self.assertIn("onboarding-choice events none", skill)
        self.assertIn("onboarding-choice plan none", skill)
        self.assertIn("onboarding-choice activities none", skill)
        self.assertTrue(prompt_path.exists())
        prompt = prompt_path.read_text(encoding="utf-8")
        self.assertLess(len(prompt), 1800)
        self.assertIn("prompt-driven", prompt.lower())
        self.assertIn("pip install --require-hashes -r requirements.lock", prompt)
        self.assertIn("pip install --no-build-isolation --no-deps -e .", prompt)
        self.assertIn("official strava account archive", prompt.lower())
        self.assertIn("fit/tcx/gpx", prompt.lower())
        self.assertIn("official garmin connect account export", prompt.lower())
        self.assertIn("apple health", prompt.lower())

    def test_empty_workspace_starts_with_profile_and_compact_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "workspace"
            _init_workspace(data_dir, force=False)

            status = onboarding_status(data_dir)

        self.assertFalse(status["complete"])
        self.assertEqual(status["current_step"], "profile")
        self.assertEqual(status["steps"][0]["status"], "complete")
        self.assertLess(len(json.dumps(status, separators=(",", ":"))), 2048)

    def test_prompt_flow_can_record_explicit_no_plan_and_no_activity_choices(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "workspace"
            _init_workspace(data_dir, force=False)
            write_json(
                data_dir / "plan" / "athlete.json",
                {
                    "display_name": "",
                    "timezone": "Europe/Paris",
                    "unit_system": "metric",
                    "disciplines": ["road", "commuting"],
                    "experience_level": "recreational",
                    "weekly_availability": "4-6 hours",
                    "constraints": ["weekday rides under 90 minutes"],
                    "sensors": ["heart_rate"],
                },
            )
            (data_dir / "plan" / "goals.md").write_text(
                "# Ride consistently\n\n## Main Goals\n\nBuild toward a comfortable first century.\n",
                encoding="utf-8",
            )
            set_onboarding_choice(data_dir, "plan", "none")
            set_onboarding_choice(data_dir, "events", "none")
            set_onboarding_choice(data_dir, "activities", "none")
            build_training_center(data_dir)

            status = onboarding_status(data_dir)

        self.assertTrue(status["complete"])
        self.assertIsNone(status["current_step"])
        statuses = {step["key"]: step["status"] for step in status["steps"]}
        self.assertEqual(statuses["plan"], "skipped")
        self.assertEqual(statuses["events"], "skipped")
        self.assertEqual(statuses["activities"], "skipped")
        self.assertEqual(statuses["dashboard"], "complete")

    def test_local_recording_satisfies_activity_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "workspace"
            _init_workspace(data_dir, force=False)
            write_json(
                data_dir / "recordings" / "activities.json",
                {"recording-ride": {"id": "recording-ride", "sport_type": "Ride"}},
            )

            status = set_onboarding_choice(data_dir, "activities", "local_recordings")

        activities = next(step for step in status["steps"] if step["key"] == "activities")
        self.assertEqual(activities["status"], "complete")
        self.assertEqual(activities["choice"], "local_recordings")
        self.assertEqual(status["summary"]["activities"], 1)

    def test_external_sync_manifest_satisfies_activity_history(self) -> None:
        from gradient_ascent.external_sync import import_sync_manifest

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "workspace"
            _init_workspace(data_dir, force=False)
            manifest_path = root / "manifest.json"
            write_json(
                manifest_path,
                {
                    "version": 1,
                    "provider": {"id": "ride-service", "label": "Ride Service"},
                    "activities": [
                        {
                            "id": "morning-ride",
                            "date": "2026-08-14",
                            "start_date_local": "2026-08-14T08:00:00",
                            "sport_type": "Ride",
                            "moving_time_s": 3600,
                            "distance_m": 25000,
                        }
                    ],
                    "recovery": [],
                },
            )
            import_sync_manifest(data_dir, manifest_path)

            status = set_onboarding_choice(data_dir, "activities", "external_sync")

        activities = next(step for step in status["steps"] if step["key"] == "activities")
        self.assertEqual(activities["status"], "complete")
        self.assertEqual(activities["choice"], "external_sync")
        self.assertEqual(status["summary"]["activities"], 1)

    def test_onboarding_choices_reject_unknown_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "workspace"
            _init_workspace(data_dir, force=False)

            with self.assertRaisesRegex(ValueError, "Unsupported activities choice"):
                set_onboarding_choice(data_dir, "activities", "mystery-provider")

    def test_profile_update_validates_timezone_and_preserves_existing_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "workspace"
            _init_workspace(data_dir, force=False)
            athlete_path = data_dir / "plan" / "athlete.json"
            athlete = json.loads(athlete_path.read_text(encoding="utf-8"))
            athlete["ftp_w"] = 275
            write_json(athlete_path, athlete)

            status = set_onboarding_profile(
                data_dir,
                timezone="Europe/Amsterdam",
                unit_system="metric",
                disciplines=["commuting", "road"],
                experience_level="recreational",
                weekly_availability="5-7 hours",
                constraints=["weekday rides are commutes"],
                sensors=["heart_rate"],
            )

            saved = json.loads(athlete_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["ftp_w"], 275)
            self.assertEqual(saved["timezone"], "Europe/Amsterdam")
            self.assertEqual(saved["unit_system"], "metric")
            self.assertEqual(saved["disciplines"], ["commuting", "road"])
            self.assertEqual(status["current_step"], "goals")

            before = athlete_path.read_bytes()
            with self.assertRaisesRegex(ValueError, "timezone"):
                set_onboarding_profile(data_dir, timezone="Mars/Olympus_Mons")
            self.assertEqual(athlete_path.read_bytes(), before)

    def test_prompt_flow_records_structured_goal_and_event_without_plan_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "workspace"
            _init_workspace(data_dir, force=False)
            set_onboarding_profile(
                data_dir,
                timezone="America/Chicago",
                unit_system="imperial",
                disciplines=["road"],
                experience_level="intermediate",
                weekly_availability="6-8 hours",
            )

            status = set_onboarding_goals(
                data_dir,
                north_star="Finish my first century feeling in control",
                goal="Complete the century without a late-ride fade",
                why="It is the main event I care about this season.",
                success="Finish safely while following the practiced pacing and fueling plan.",
                coaching_implication="Prioritize long-ride durability and fueling practice.",
                evidence="Long-ride completion, pacing stability, and practiced fueling tolerance.",
            )
            self.assertEqual(status["current_step"], "events")

            status = add_onboarding_event(
                data_dir,
                name="Community Century",
                event_date="2026-09-12",
                discipline="road",
                priority="A",
                location="Madison WI",
            )

            events = json.loads((data_dir / "plan" / "events.json").read_text(encoding="utf-8"))
            goals = (data_dir / "plan" / "goals.md").read_text(encoding="utf-8")
            self.assertEqual(status["current_step"], "plan")
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["name"], "Community Century")
            self.assertEqual(events[0]["date"], "2026-09-12")
            self.assertEqual(events[0]["priority"], "A")
            self.assertTrue(events[0]["markers"]["commitment"])
            self.assertIn("# Finish my first century feeling in control", goals)
            self.assertIn("### Complete the century without a late-ride fade", goals)

            with self.assertRaisesRegex(ValueError, "ISO date"):
                add_onboarding_event(
                    data_dir,
                    name="Bad Date Race",
                    event_date="next Saturday",
                    discipline="road",
                    priority="B",
                )


if __name__ == "__main__":
    unittest.main()
