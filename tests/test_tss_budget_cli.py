from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from gradient_ascent import cli, training_center
from gradient_ascent.config import Config
from gradient_ascent.storage import write_json
from gradient_ascent.tss_budgets import load_tss_budgets, update_tss_budgets


WEEK = {
    "start_date": "2026-08-17",
    "end_date": "2026-08-23",
    "days": {"Tue": "Synthetic prescribed session"},
    "hours_target": {"min": 8, "max": 11},
}


def make_workspace(root: Path) -> None:
    cli._init_workspace(root, force=False)
    write_json(root / "plan" / "weeks.json", [WEEK])


def make_draft(path: Path, **changes: object) -> Path:
    write_json(
        path,
        {
            "version": 1,
            "budgets": [
                {
                    "start_date": WEEK["start_date"],
                    "end_date": WEEK["end_date"],
                    "target_tss": 330,
                    "ceiling_tss": 380,
                    "rationale": "Private synthetic coaching rationale.",
                    **changes,
                }
            ],
        },
    )
    return path


def run_cli(root: Path, *args: str) -> dict:
    output = io.StringIO()
    with (
        patch("sys.argv", ["gradient-ascent", *args]),
        patch("gradient_ascent.cli.load_config", return_value=Config(root)),
        redirect_stdout(output),
    ):
        cli.main()
    return json.loads(output.getvalue())


class TssBudgetCliTest(unittest.TestCase):
    def test_current_daily_allocations_are_an_explicit_read_only_status_option(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "athlete"
            make_workspace(root)
            start = date.fromisoformat(WEEK["start_date"])
            values = (0, 80, 30, 60, 20, 100, 40)
            draft = make_draft(
                Path(tmp) / "draft.json",
                daily_tss=[
                    {
                        "date": (start + timedelta(days=index)).isoformat(),
                        "target_tss": value,
                        "rationale": "Private synthetic daily rationale.",
                    }
                    for index, value in enumerate(values)
                ],
            )
            update_tss_budgets(root, draft)
            before = (root / "plan/tss_budgets.json").read_bytes()
            with (
                patch("socket.socket", side_effect=AssertionError("offline")),
                patch("subprocess.Popen", side_effect=AssertionError("offline")),
            ):
                status = run_cli(root, "tss-budget-status")
                detailed = run_cli(root, "tss-budget-status", "--daily")
            self.assertNotIn("daily_tss", status)
            self.assertNotIn("Private synthetic", json.dumps(status))
            self.assertEqual([item["target_tss"] for item in detailed["daily_tss"]], list(values))
            self.assertTrue(
                all(
                    item["tss_source"] == "coach_budget_allocation"
                    for item in detailed["daily_tss"]
                )
            )
            self.assertTrue(all(item["status"] == "provisional" for item in detailed["daily_tss"]))
            self.assertFalse(detailed["external_access"])
            self.assertEqual((root / "plan/tss_budgets.json").read_bytes(), before)

    def test_status_is_aggregate_and_fingerprints_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "athlete"
            make_workspace(root)
            self.assertEqual(
                json.loads((root / "plan" / "tss_budgets.json").read_text()),
                {"version": 1, "budgets": []},
            )
            with (
                patch("socket.socket", side_effect=AssertionError("offline")),
                patch("subprocess.Popen", side_effect=AssertionError("offline")),
            ):
                status = run_cli(root, "tss-budget-status")
                detailed = run_cli(root, "tss-budget-status", "--fingerprints")
            self.assertEqual(status["total"], 0)
            self.assertNotIn("weeks", status)
            self.assertFalse(status["external_access"])
            self.assertEqual(len(detailed["weeks"]), 1)
            entry = detailed["weeks"][0]
            self.assertEqual(set(entry), {"start_date", "end_date", "plan_fingerprint"})
            self.assertEqual(
                (entry["start_date"], entry["end_date"]), (WEEK["start_date"], WEEK["end_date"])
            )
            self.assertRegex(entry["plan_fingerprint"], r"^[a-f0-9]{64}$")
            self.assertNotIn("Synthetic prescribed session", json.dumps(detailed))

    def test_update_rebuilds_local_insights_and_dashboard_and_preserves_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "athlete"
            make_workspace(root)
            source = (root / "plan" / "weeks.json").read_bytes()
            fingerprint = run_cli(root, "tss-budget-status", "--fingerprints")["weeks"][0][
                "plan_fingerprint"
            ]
            draft = make_draft(Path(tmp) / "draft.json", expected_plan_fingerprint=fingerprint)
            with (
                patch("gradient_ascent.cli.build_insights", wraps=cli.build_insights) as insights,
                patch(
                    "gradient_ascent.training_center.build_training_center",
                    return_value={"weeks": 1, "days": 7, "html": "PRIVATE_PATH"},
                ) as rebuild,
                patch(
                    "gradient_ascent.refresh.refresh_workspace",
                    side_effect=AssertionError("no source refresh"),
                ),
                patch("socket.socket", side_effect=AssertionError("offline")),
                patch("subprocess.Popen", side_effect=AssertionError("offline")),
            ):
                result = run_cli(root, "update-tss-budgets", "--file", str(draft))
            insights.assert_called_once_with(root, None, root / "derived")
            rebuild.assert_called_once_with(root)
            self.assertEqual((result["created"], result["current"]), (1, 1))
            self.assertTrue(result["rebuilt"])
            self.assertFalse(result["external_access"])
            self.assertNotIn("PRIVATE_PATH", json.dumps(result))
            self.assertNotIn("Private synthetic", json.dumps(result))
            self.assertEqual((root / "plan" / "weeks.json").read_bytes(), source)
            self.assertEqual(
                load_tss_budgets(root)[(WEEK["start_date"], WEEK["end_date"])]["target_tss"], 330
            )
            saved = (root / "plan" / "tss_budgets.json").read_bytes()
            cli._init_workspace(root, force=False)
            self.assertEqual((root / "plan" / "tss_budgets.json").read_bytes(), saved)
            with (
                patch("gradient_ascent.cli.build_insights") as insights,
                patch("gradient_ascent.training_center.build_training_center") as rebuild,
            ):
                repeated = run_cli(root, "update-tss-budgets", "--file", str(draft), "--no-rebuild")
            insights.assert_not_called()
            rebuild.assert_not_called()
            self.assertEqual(repeated["unchanged"], 1)
            self.assertFalse(repeated["rebuilt"])
            empty = Path(tmp) / "empty.json"
            write_json(empty, {"version": 1, "budgets": []})
            removed = run_cli(
                root, "update-tss-budgets", "--file", str(empty), "--replace", "--no-rebuild"
            )
            self.assertEqual((removed["removed"], removed["total"]), (1, 0))

    def test_default_rebuild_uses_current_source_targets_and_new_weeks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "athlete"
            make_workspace(root)
            old = {**WEEK, "tss_target": {"min": 500, "max": 500}}
            write_json(root / "plan" / "weeks.json", [old])
            write_json(root / "plan" / "athlete.json", {"ftp_w": 250})
            write_json(
                root / "strava" / "activities.json",
                {
                    "1": {
                        "id": 1,
                        "name": "Synthetic ride",
                        "start_date": "2026-08-18T12:00:00Z",
                        "sport_type": "Ride",
                        "moving_time": 3600,
                        "elapsed_time": 3600,
                        "weighted_average_watts": 200,
                    }
                },
            )
            cli.build_insights(root, None, root / "derived")
            recorded_before = (root / "derived" / "activities.json").read_bytes()
            current = {**WEEK, "tss_target": {"min": 400, "max": 400}}
            next_week = {**WEEK, "start_date": "2026-08-24", "end_date": "2026-08-30"}
            write_json(root / "plan" / "weeks.json", [current, next_week])
            draft = make_draft(Path(tmp) / "draft.json", target_tss=400, ceiling_tss=450)
            with (
                patch("socket.socket", side_effect=AssertionError("offline")),
                patch("subprocess.Popen", side_effect=AssertionError("offline")),
            ):
                run_cli(root, "update-tss-budgets", "--file", str(draft))
                payload, *_ = training_center._build_payload(root)
            weeks = {week["start_date"]: week for week in payload["weeks"]}
            self.assertEqual(weeks[WEEK["start_date"]]["planned_load"]["estimated_tss"], 400)
            self.assertEqual(
                weeks[WEEK["start_date"]]["planned_load"]["tss_source"], "source_target"
            )
            self.assertIn(next_week["start_date"], weeks)
            self.assertEqual((root / "derived" / "activities.json").read_bytes(), recorded_before)

    def test_replacement_after_update_cannot_receive_dashboard_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "athlete"
            make_workspace(root)
            draft = make_draft(Path(tmp) / "draft.json")

            def replace_after_update(*args, **kwargs):
                result = update_tss_budgets(*args, **kwargs)
                root.rename(Path(tmp) / "old-athlete")
                make_workspace(root)
                return result

            with (
                patch(
                    "gradient_ascent.tss_budgets.update_tss_budgets",
                    side_effect=replace_after_update,
                ) as update,
                patch("gradient_ascent.training_center.build_training_center") as rebuild,
                self.assertRaises(SystemExit) as caught,
            ):
                run_cli(root, "update-tss-budgets", "--file", str(draft))
            update.assert_called_once()
            rebuild.assert_not_called()
            self.assertIn("could not finish safely", str(caught.exception))
            self.assertNotIn(str(root), str(caught.exception))
            self.assertEqual(load_tss_budgets(root), {})

    def test_replacement_after_insights_cannot_receive_dashboard_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "athlete"
            make_workspace(root)
            draft = make_draft(Path(tmp) / "draft.json")
            original = cli.build_insights

            def replace_after_insights(*args, **kwargs):
                result = original(*args, **kwargs)
                root.rename(Path(tmp) / "old-athlete")
                make_workspace(root)
                return result

            with (
                patch(
                    "gradient_ascent.cli.build_insights", side_effect=replace_after_insights
                ) as insights,
                patch("gradient_ascent.training_center.build_training_center") as rebuild,
                self.assertRaises(SystemExit) as caught,
            ):
                run_cli(root, "update-tss-budgets", "--file", str(draft))
            insights.assert_called_once()
            rebuild.assert_not_called()
            self.assertIn("could not finish safely", str(caught.exception))
            self.assertEqual(load_tss_budgets(root), {})

    def test_filesystem_failures_do_not_echo_private_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "athlete"
            make_workspace(root)
            draft = make_draft(Path(tmp) / "draft.json")
            for error in (OSError("PRIVATE_PATH"), ValueError("PRIVATE_PATH")):
                with (
                    self.subTest(error=type(error).__name__),
                    patch(
                        "gradient_ascent.tss_budgets.tss_budget_summary", side_effect=error
                    ) as summary,
                    self.assertRaises(SystemExit) as caught,
                ):
                    run_cli(root, "tss-budget-status")
                summary.assert_called_once_with(root)
                self.assertIn("could not finish safely", str(caught.exception))
                self.assertNotIn("PRIVATE_PATH", str(caught.exception))
            with (
                patch(
                    "gradient_ascent.training_center.build_training_center",
                    side_effect=ValueError("PRIVATE_PATH"),
                ) as rebuild,
                self.assertRaises(SystemExit) as caught,
            ):
                run_cli(root, "update-tss-budgets", "--file", str(draft))
            rebuild.assert_called_once_with(root)
            self.assertIn("could not finish safely", str(caught.exception))
            self.assertNotIn("PRIVATE_PATH", str(caught.exception))

    def test_schema_validation_remains_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "athlete"
            make_workspace(root)
            draft = make_draft(Path(tmp) / "draft.json", target_tss=-1)
            with self.assertRaises(SystemExit) as caught:
                run_cli(root, "update-tss-budgets", "--file", str(draft), "--no-rebuild")
            self.assertIn("nonnegative", str(caught.exception))
            self.assertEqual(load_tss_budgets(root), {})


if __name__ == "__main__":
    unittest.main()
