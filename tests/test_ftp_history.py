import copy
import hashlib
import io
import json
import tempfile
import unittest
from datetime import date
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from gradient_ascent.storage import write_json


class FTPHistoryTest(unittest.TestCase):
    def test_cached_power_uses_dated_ftp_and_missing_baseline_stays_unknown(self):
        from gradient_ascent.ftp_history import updated_ftp_profile
        from gradient_ascent.insights import _normalize_activity
        from gradient_ascent.power_metrics import estimate_normalized_power
        from gradient_ascent.training_center import _activity_load_display

        estimate = estimate_normalized_power(list(range(3601)), [200] * 3601)
        original = copy.deepcopy(estimate)
        ride = {"sport_type": "Ride", "moving_time_s": 3600, "power_load_estimate": estimate}
        profile = updated_ftp_profile({"ftp_w": 200}, 250, "2026-07-01", today=date(2026, 8, 1))
        before = _normalize_activity({**ride, "date": "2026-06-30"}, profile)
        after = _normalize_activity({**ride, "date": "2026-07-01"}, profile)
        self.assertEqual((before["estimated_tss"], after["estimated_tss"]), (100, 64))
        self.assertEqual(estimate, original)
        self.assertIn(
            "250 W FTP effective 2026-07-01", _activity_load_display(after)["tss_description"]
        )
        self.assertIn("legacy FTP baseline", _activity_load_display(before)["tss_description"])
        unknown = updated_ftp_profile({}, 250, "2026-07-01", today=date(2026, 8, 1))
        self.assertIsNone(
            _normalize_activity({**ride, "date": "2026-06-30"}, unknown)["estimated_tss"]
        )
        self.assertEqual(
            _normalize_activity({**ride, "date": "2026-07-01"}, unknown)["estimated_tss"], 64
        )

    def test_cli_rebuilds_locally_and_keeps_applied_status_on_rebuild_failure(self):
        from gradient_ascent import cli
        from gradient_ascent.config import Config

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            cli._init_workspace(root, force=False)
            write_json(root / "plan/athlete.json", {"ftp_w": 200, "timezone": "UTC"})

            def run(*args):
                output = io.StringIO()
                with (
                    patch("sys.argv", ["gradient-ascent", *args]),
                    patch("gradient_ascent.cli.load_config", return_value=Config(root)),
                    redirect_stdout(output),
                ):
                    cli.main()
                return json.loads(output.getvalue())

            status = run("ftp-history", "--date", "2026-06-30")
            self.assertEqual(status["ftp_w"], 200)
            with patch(
                "gradient_ascent.cli.build_insights", side_effect=ValueError("PRIVATE_PATH")
            ):
                changed = run(
                    "set-ftp",
                    "--watts",
                    "250",
                    "--effective-date",
                    "2026-07-01",
                    "--reason",
                    "Accepted synthetic test",
                    "--change-key",
                    "ftp-cli",
                    "--expected-profile",
                    status["profile_sha256"],
                )
            self.assertEqual(changed["status"], "applied")
            self.assertEqual(changed["rebuild_status"], "failed")
            self.assertNotIn("PRIVATE_PATH", json.dumps(changed))
            self.assertEqual(run("ftp-history", "--date", "2026-06-30")["ftp_w"], 200)
            self.assertEqual(run("ftp-history", "--date", "2026-07-01")["ftp_w"], 250)

    def test_recorded_tss_uses_local_activity_date_and_source_scores_still_win(self):
        from gradient_ascent.ftp_history import updated_ftp_profile
        from gradient_ascent.insights import _normalize_activity

        profile = updated_ftp_profile({"ftp_w": 200}, 250, "2026-07-01", today=date(2026, 8, 1))
        ride = {
            "id": "synthetic",
            "sport_type": "Ride",
            "moving_time": 3600,
            "weighted_average_watts": 200,
            "start_date": "2026-07-01T02:00:00Z",
            "start_date_local": "2026-06-30T19:00:00-07:00",
        }
        old = _normalize_activity(ride, profile)
        new = _normalize_activity({**ride, "date": "2026-07-01"}, profile)
        supplied = _normalize_activity(
            {**ride, "date": "2026-07-01", "estimated_tss": 87.25, "intensity_factor": 0.91},
            profile,
        )
        self.assertEqual(
            (old["estimated_tss"], old["ftp_w"], old["ftp_source"]), (100, 200, "legacy_baseline")
        )
        self.assertEqual(
            (new["estimated_tss"], new["estimated_tss_ftp_w"], new["ftp_effective_date"]),
            (64, 250, "2026-07-01"),
        )
        self.assertEqual((supplied["estimated_tss"], supplied["intensity_factor"]), (87.25, 0.91))
        self.assertIsNone(supplied["estimated_tss_ftp_w"])

    def test_budget_fingerprints_change_only_for_affected_dates(self):
        from gradient_ascent.ftp_history import updated_ftp_profile
        from gradient_ascent.tss_budgets import _fingerprint

        old_profile = {"ftp_w": 200, "constraints": None}
        profile = updated_ftp_profile(old_profile, 250, "2026-07-01", today=date(2026, 8, 1))
        context = {
            "athlete": old_profile,
            "goals_sha256": "x",
            "events": [],
            "phases": [],
            "workouts": [],
        }
        updated = {
            **context,
            "athlete": {**old_profile, "ftp_w": 250},
            "ftp_history": profile["ftp_history"],
        }
        for start, end, same in (
            ("2026-06-22", "2026-06-28", True),
            ("2026-06-29", "2026-07-05", False),
            ("2026-07-06", "2026-07-12", False),
        ):
            week = {"start_date": start, "end_date": end}
            self.assertEqual(_fingerprint(week, context) == _fingerprint(week, updated), same)

    def test_date_resolution_preserves_legacy_baseline_and_exact_boundaries(self):
        from gradient_ascent.ftp_history import resolve_ftp, updated_ftp_profile

        original = {"ftp_w": 200, "display_name": "Synthetic rider"}
        profile = updated_ftp_profile(original, 250, "2026-07-01", today=date(2026, 8, 1))
        self.assertEqual(original, {"ftp_w": 200, "display_name": "Synthetic rider"})
        self.assertEqual(profile["ftp_w"], 250)
        self.assertEqual(
            resolve_ftp(profile, "2026-06-30"),
            {"ftp_w": 200.0, "source": "legacy_baseline", "effective_date": None},
        )
        self.assertEqual(
            resolve_ftp(profile, "2026-07-01"),
            {"ftp_w": 250.0, "source": "dated_history", "effective_date": "2026-07-01"},
        )
        self.assertIsNone(resolve_ftp(profile, None)["ftp_w"])
        self.assertEqual(resolve_ftp(original, None)["ftp_w"], 200)
        earlier = updated_ftp_profile(profile, 180, "2026-01-01", today=date(2026, 8, 1))
        self.assertEqual(earlier["ftp_w"], 250)
        self.assertEqual(resolve_ftp(earlier, "2026-02-01")["ftp_w"], 180)

    def test_invalid_or_conflicting_history_fails_without_mutation(self):
        from gradient_ascent.ftp_history import FTPHistoryError, resolve_ftp, updated_ftp_profile

        profile = updated_ftp_profile({"ftp_w": 200}, 250, "2026-07-01", today=date(2026, 8, 1))
        before = copy.deepcopy(profile)
        for watts, when in (
            (True, "2026-07-01"),
            (float("nan"), "2026-07-01"),
            (10**400, "2026-07-01"),
            (0, "2026-07-01"),
            (250, "2026-02-30"),
            (250, "2026-09-01"),
            (260, "2026-07-01"),
        ):
            with self.subTest(watts=watts, when=when), self.assertRaises(FTPHistoryError):
                updated_ftp_profile(profile, watts, when, today=date(2026, 8, 1))
        self.assertEqual(profile, before)
        corrected = updated_ftp_profile(
            profile, 260, "2026-07-01", replace=True, today=date(2026, 8, 1)
        )
        self.assertEqual(resolve_ftp(corrected, "2026-06-30")["ftp_w"], 200)
        broken = copy.deepcopy(profile)
        broken["ftp_history"]["entries"].append(broken["ftp_history"]["entries"][0])
        with self.assertRaises(FTPHistoryError):
            resolve_ftp(broken, "2026-08-01")

    def test_writer_is_logged_idempotent_and_rejects_stale_profile(self):
        from gradient_ascent.coaching_history import plan_history
        from gradient_ascent.ftp_history import FTPHistoryError, set_ftp

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            root.mkdir(mode=0o700)
            path = root / "plan/athlete.json"
            write_json(path, {"ftp_w": 200, "timezone": "UTC"})
            old_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            metadata = {
                "idempotency_key": "synthetic-ftp",
                "rationale": "Accepted synthetic threshold.",
            }
            first = set_ftp(
                root,
                250,
                "2026-07-01",
                expected_profile_sha256=old_hash,
                history_request=metadata,
                today=date(2026, 8, 1),
            )
            retry = set_ftp(
                root,
                250,
                "2026-07-01",
                expected_profile_sha256=old_hash,
                history_request=metadata,
                today=date(2026, 8, 1),
            )
            self.assertEqual(first["history"]["id"], retry["history"]["id"])
            self.assertEqual(len(plan_history(root)), 1)
            self.assertEqual(list(plan_history(root)[0]["files"]), ["plan/athlete.json"])
            before = path.read_bytes()
            with self.assertRaises(FTPHistoryError):
                set_ftp(
                    root,
                    275,
                    "2026-07-15",
                    expected_profile_sha256=old_hash,
                    history_request=metadata,
                    today=date(2026, 8, 1),
                )
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(json.loads(before)["ftp_history"]["baseline_w"], 200)

    def test_local_rebuild_and_structured_forecast_use_dated_ftp_without_source_edits(self):
        from gradient_ascent import cli
        from gradient_ascent.ftp_history import updated_ftp_profile
        from gradient_ascent.insights import build_insights
        from gradient_ascent.training_center import _structured_dashboard_workouts
        from gradient_ascent.tss_budgets import _context, _daily_source_bounds
        from gradient_ascent.recording_repair import _directory

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            cli._init_workspace(root, force=False)
            profile = updated_ftp_profile({"ftp_w": 200}, 250, "2026-07-01", today=date(2026, 8, 1))
            write_json(root / "plan/athlete.json", profile)
            rides = {
                str(i): {
                    "id": i,
                    "name": "Synthetic ride",
                    "sport_type": "Ride",
                    "start_date": day + "T12:00:00Z",
                    "start_date_local": day + "T12:00:00Z",
                    "moving_time": 3600,
                    "weighted_average_watts": 200,
                }
                for i, day in ((1, "2026-06-30"), (2, "2026-07-01"))
            }
            write_json(root / "strava/activities.json", rides)
            original = (root / "strava/activities.json").read_bytes()
            build_insights(root, None, root / "derived")
            rows = json.loads((root / "derived/activities.json").read_text())
            self.assertEqual(
                {r["date"]: r["estimated_tss"] for r in rows}, {"2026-06-30": 100, "2026-07-01": 64}
            )
            self.assertEqual((root / "strava/activities.json").read_bytes(), original)
            workouts = [
                {
                    "id": "synthetic-" + str(i),
                    "date": day,
                    "name": "Synthetic steady",
                    "sport": "cycling",
                    "steps": [
                        {
                            "name": "Steady",
                            "duration_s": 3600,
                            "intensity": "active",
                            "target": {"type": "power", "unit": "watts", "low": 200, "high": 200},
                        }
                    ],
                }
                for i, day in ((1, "2026-06-30"), (2, "2026-07-01"))
            ]
            write_json(root / "plan/workouts.json", {"version": 1, "workouts": workouts})
            week = {"start_date": "2026-06-29", "end_date": "2026-07-05", "days": {}}
            write_json(root / "plan/weeks.json", [week])
            source = (root / "plan/workouts.json").read_bytes()
            loads = _structured_dashboard_workouts(root, profile)
            self.assertEqual(
                [loads[d][0]["load"]["estimated_tss"] for d in ("2026-06-30", "2026-07-01")],
                [100, 64],
            )
            with _directory(root) as fd:
                _, context = _context(fd)
            self.assertEqual(_daily_source_bounds(week, context, "2026-07-01"), (64, 64))
            self.assertEqual((root / "plan/workouts.json").read_bytes(), source)

    def test_plan_reimport_preserves_dated_ftp_and_legacy_fingerprints(self):
        from gradient_ascent import cli
        from gradient_ascent.ftp_history import ftp_period_context, updated_ftp_profile
        from gradient_ascent.plan import build_plan_from_csv

        for baseline in (200, 200.0, "200"):
            old = {"ftp_w": baseline}
            updated = updated_ftp_profile(old, 250, "2026-07-01", today=date(2026, 8, 1))
            self.assertEqual(
                json.dumps(ftp_period_context(old, "2026-06-01", "2026-06-07")),
                json.dumps(ftp_period_context(updated, "2026-06-01", "2026-06-07")),
            )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            cli._init_workspace(root, force=False)
            profile = updated_ftp_profile({"ftp_w": 200}, 250, "2026-07-01", today=date(2026, 8, 1))
            write_json(root / "plan/athlete.json", profile)
            build_plan_from_csv(
                Path(__file__).resolve().parents[1]
                / "examples/calendar/sample-training-calendar.csv",
                root / "plan",
            )
            after = json.loads((root / "plan/athlete.json").read_text())
            self.assertEqual(after["ftp_w"], profile["ftp_w"])
            self.assertEqual(after["ftp_history"], profile["ftp_history"])
