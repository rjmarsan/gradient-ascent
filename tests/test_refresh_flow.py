import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from gradient_ascent.cli import _init_workspace
from gradient_ascent.connections import update_provider


REPO_ROOT = Path(__file__).resolve().parents[1]
REFRESH = REPO_ROOT / "scripts" / "post-sync-refresh.sh"


def _refresh(workspace: Path, *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT),
        "PYTHON_BIN": sys.executable,
    }
    env.pop("COACH_DATA_DIR", None)
    if cwd is None:
        env["COACH_WORKSPACE_DIR"] = str(workspace)
        cwd = REPO_ROOT
    else:
        env.pop("COACH_WORKSPACE_DIR", None)
    return subprocess.run(
        [str(REFRESH)],
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )


class RefreshFlowTest(unittest.TestCase):
    def test_local_refresh_reports_ridewithgps_recording_coverage(self) -> None:
        from gradient_ascent.refresh import refresh_workspace
        from gradient_ascent.storage import write_json

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            _init_workspace(workspace, force=False)
            write_json(workspace / "recordings" / "activities.json", {
                "recording-synthetic": {
                    "id": "recording-synthetic", "name": "Synthetic ride",
                    "sport_type": "Ride", "start_date": "2026-08-15T01:00:00+00:00",
                    "start_date_local": "2026-08-14T18:00:00-07:00", "moving_time": 1800,
                    "distance": 12000, "source_provider": "ridewithgps", "source_activity_id": "123",
                }
            })
            summary = refresh_workspace(workspace)

        self.assertEqual(summary["sources"]["recordings"]["count"], 1)
        self.assertEqual(summary["sources"]["ridewithgps"], {"count": 1, "first": "2026-08-14", "last": "2026-08-14"})

    def test_refresh_includes_imported_external_sync_data(self) -> None:
        from gradient_ascent.external_sync import import_sync_manifest

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            _init_workspace(workspace, force=False)
            manifest_path = root / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "provider": {"id": "ride-service", "label": "Ride Service"},
                        "synced_at": "2026-08-14T08:30:00Z",
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
                        "recovery": [
                            {
                                "id": "morning-recovery",
                                "date": "2026-08-14",
                                "resting_hr": 48,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            import_sync_manifest(workspace, manifest_path)

            result = _refresh(workspace)
            summary = json.loads((workspace / "derived" / "post_sync_summary.json").read_text())
            daily = json.loads((workspace / "derived" / "daily.json").read_text())
            activities = json.loads((workspace / "derived" / "activities.json").read_text())
            weekly = json.loads((workspace / "derived" / "weekly.json").read_text())
            dashboard_data = (workspace / "derived" / "training_center_data.js").read_text()

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(summary["canonical"]["activities"], 1)
        self.assertEqual(summary["canonical"]["recovery"], 1)
        self.assertEqual(summary["sources"]["external"]["ride-service"]["activities"]["count"], 1)
        self.assertEqual(summary["sources"]["external"]["ride-service"]["recovery"]["count"], 1)
        self.assertEqual(daily[0]["primary_recovery"]["source"]["provider"], "ride-service")
        self.assertEqual(activities[0]["id"], "ride-service:morning-ride")
        self.assertEqual(weekly[0]["activity_ids"], ["ride-service:morning-ride"])
        self.assertIn("ride-service:morning-ride", dashboard_data)

    def test_blank_workspace_refresh_builds_canonical_insights_and_dashboard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            _init_workspace(workspace, force=False)

            result = _refresh(workspace)
            summary = json.loads((workspace / "derived" / "post_sync_summary.json").read_text())

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(summary["sources"]["strava"]["count"], 0)
        self.assertEqual(summary["sources"]["garmin"]["count"], 0)
        self.assertEqual(summary["imports"]["apple_health"]["status"], "not_configured")
        self.assertTrue(Path(summary["training_center"]["html"]).name == "training_center.html")

    def test_refresh_reimports_configured_apple_health_path(self) -> None:
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData>
  <Record type="HKQuantityTypeIdentifierRestingHeartRate" startDate="2026-05-01 08:00:00 -0700" endDate="2026-05-01 08:00:00 -0700" value="49"/>
</HealthData>
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            export_path = root / "export.xml"
            export_path.write_text(xml, encoding="utf-8")
            _init_workspace(workspace, force=False)
            update_provider(workspace, "apple_health", fields={"export_path": str(export_path)})

            result = _refresh(workspace)
            summary = json.loads((workspace / "derived" / "post_sync_summary.json").read_text())

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(summary["imports"]["apple_health"]["status"], "imported")
        self.assertEqual(summary["canonical"]["recovery"], 1)

    def test_wrapper_defaults_to_the_calling_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            _init_workspace(workspace, force=False)

            result = _refresh(workspace, cwd=workspace)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("Workspace refresh complete", result.stdout)

    def test_refresh_refuses_a_workspace_inside_the_plugin_checkout(self) -> None:
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "post_sync_refresh.py"), "--data-dir", str(REPO_ROOT / "private-data")],
            cwd=REPO_ROOT,
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Refusing to refresh coaching workspace", result.stdout)


if __name__ == "__main__":
    unittest.main()
