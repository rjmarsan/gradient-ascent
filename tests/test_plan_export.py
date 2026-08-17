from __future__ import annotations

import io
import json
import stat
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import fitdecode

from gradient_ascent.plan_export import build_plan_export, write_plan_export
from gradient_ascent.storage import write_json
from gradient_ascent.workspace_lock import workspace_identity


def make_plan(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    write_json(
        root / "plan" / "weeks.json",
        [
            {
                "start_date": "2026-08-17",
                "days": {"Mon": "Easy ride", "Tue": "Rest day"},
            }
        ],
    )
    write_json(
        root / "plan" / "workouts.json",
        {
            "version": 1,
            "workouts": [
                {
                    "id": "tempo-session",
                    "date": "2026-08-19",
                    "name": "Tempo <script>alert(1)</script>",
                    "description": "Follow the explicitly prescribed targets.",
                    "device_description": "Steady tempo",
                    "sport": "cycling",
                    "steps": [
                        {
                            "name": "Warm up",
                            "duration_s": 300,
                            "intensity": "warmup",
                            "target": {"type": "open"},
                        },
                        {
                            "name": "Tempo",
                            "duration_s": 600,
                            "intensity": "active",
                            "target": {
                                "type": "power",
                                "unit": "percent_ftp",
                                "low": 80,
                                "high": 90,
                            },
                        },
                    ],
                }
            ],
        },
    )
    write_json(root / "plan" / "athlete.json", {"private_profile": "NEVER_EXPORT_THIS"})
    write_json(root / "strava" / "activities.json", {"private_activity": "NEVER_EXPORT_THIS"})


class PlanExportTest(unittest.TestCase):
    def test_workspace_initialization_adds_empty_prescriptions_without_replacing_them(self) -> None:
        from gradient_ascent.cli import _init_workspace

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "athlete"
            _init_workspace(root, force=False)
            prescriptions = root / "plan" / "workouts.json"
            self.assertEqual(json.loads(prescriptions.read_text()), {"version": 1, "workouts": []})
            self.assertIn("exports/", (root / ".gitignore").read_text().splitlines())
            make_plan(root)
            before = prescriptions.read_bytes()
            _init_workspace(root, force=False)
            self.assertEqual(prescriptions.read_bytes(), before)

    def test_cli_exports_a_selected_structured_workout(self) -> None:
        from gradient_ascent.cli import main
        from gradient_ascent.config import Config

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "athlete"
            make_plan(root)
            output = io.StringIO()
            with (
                patch(
                    "sys.argv",
                    [
                        "gradient-ascent",
                        "export-plan",
                        "--format",
                        "fit",
                        "--workout",
                        "tempo-session",
                    ],
                ),
                patch("gradient_ascent.cli.load_config", return_value=Config(root)),
                redirect_stdout(output),
            ):
                main()
            result = json.loads(output.getvalue())
            self.assertEqual(result["fit_files"], 1)
            self.assertFalse(result["external_access"])
            self.assertTrue(Path(result["path"]).is_file())

    def test_bundle_contains_only_plan_and_valid_device_workout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "athlete"
            make_plan(root)
            with patch("subprocess.Popen", side_effect=AssertionError("no subprocess")):
                artifact = build_plan_export(root, format="zip")
                again = build_plan_export(root, format="zip")
            self.assertEqual(artifact.body, again.body)
            self.assertEqual(artifact.content_type, "application/zip")
            self.assertEqual(artifact.summary["entries"], 3)
            self.assertEqual(artifact.summary["fit_files"], 1)
            self.assertFalse(artifact.summary["external_access"])
            with zipfile.ZipFile(io.BytesIO(artifact.body)) as archive:
                self.assertEqual(
                    set(archive.namelist()),
                    {
                        "index.html",
                        "schedule.ics",
                        "schedule.csv",
                        "manifest.json",
                        "README.txt",
                        "workouts/2026-08-19-tempo-session.fit",
                    },
                )
                html = archive.read("index.html").decode()
                self.assertIn("&lt;script&gt;", html)
                self.assertNotIn("<script>", html)
                self.assertIn("calendar-only", html)
                self.assertTrue(
                    all(
                        "NEVER_EXPORT_THIS" not in archive.read(name).decode("utf-8", "ignore")
                        for name in archive.namelist()
                    )
                )
                manifest = json.loads(archive.read("manifest.json"))
                self.assertEqual(manifest["version"], 1)
                self.assertEqual(manifest["entries"][2]["duration_s"], 900)
                with fitdecode.FitReader(
                    io.BytesIO(archive.read("workouts/2026-08-19-tempo-session.fit")),
                    check_crc=fitdecode.CrcCheck.RAISE,
                ) as reader:
                    messages = [
                        frame for frame in reader if isinstance(frame, fitdecode.FitDataMessage)
                    ]
                self.assertEqual(messages[0].get_value("type"), "workout")
                self.assertEqual(sum(frame.name == "workout_step" for frame in messages), 2)

    def test_range_single_workout_and_invalid_options(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "athlete"
            make_plan(root)
            ics = build_plan_export(root, format="ics", start="2026-08-18", end="2026-08-19")
            self.assertEqual(ics.summary["entries"], 2)
            self.assertIn(b"BEGIN:VCALENDAR", ics.body)
            fit = build_plan_export(root, format="fit", workout_id="tempo-session")
            self.assertEqual(fit.filename, "2026-08-19-tempo-session.fit")
            for kwargs in (
                {"format": "fit"},
                {"format": "fit", "workout_id": "week-2026-08-17-mon"},
                {"format": "zip", "workout_id": "../../escape"},
                {"format": "exe"},
                {"format": "csv", "start": "2026-08-20", "end": "2026-08-19"},
                {"format": "ics", "start": "2026-09-01"},
            ):
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    build_plan_export(root, **kwargs)

    def test_private_atomic_output_is_idempotent_and_never_overwrites_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "athlete"
            make_plan(root)
            first = write_plan_export(root, format="zip")
            path = Path(first["path"])
            self.assertTrue(path.is_relative_to(root.resolve() / "exports" / "planned"))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertIn("exports/", (root / ".gitignore").read_text().splitlines())
            second = write_plan_export(root, format="zip")
            self.assertFalse(second["written"])
            path.write_bytes(b"keep me")
            with self.assertRaises(FileExistsError):
                write_plan_export(root, format="zip")
            self.assertEqual(path.read_bytes(), b"keep me")
            third = write_plan_export(root, format="zip", overwrite=True)
            self.assertTrue(third["written"])
            self.assertTrue(zipfile.is_zipfile(path))

    def test_output_links_and_stale_workspace_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "athlete"
            make_plan(root)
            victim = Path(tmp) / "victim"
            victim.write_bytes(b"keep")
            linked = Path(tmp) / "linked.zip"
            linked.symlink_to(victim)
            with self.assertRaises((ValueError, OSError)):
                write_plan_export(root, format="zip", output_path=linked, overwrite=True)
            self.assertEqual(victim.read_bytes(), b"keep")
            identity = workspace_identity(root)
            root.rename(Path(tmp) / "old-athlete")
            make_plan(root)
            with self.assertRaises(RuntimeError):
                write_plan_export(root, format="zip", expected_identity=identity)
            self.assertFalse((root / "exports").exists())

    def test_workspace_replaced_after_build_receives_no_export_setup_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "athlete"
            make_plan(root)
            original_build = build_plan_export

            def replace_after_build(*args, **kwargs):
                artifact = original_build(*args, **kwargs)
                root.rename(Path(tmp) / "old-athlete")
                make_plan(root)
                return artifact

            with patch(
                "gradient_ascent.plan_export.build_plan_export", side_effect=replace_after_build
            ):
                with self.assertRaises(RuntimeError):
                    write_plan_export(root, format="zip")
            self.assertFalse((root / ".gitignore").exists())
            self.assertFalse((root / "exports").exists())


if __name__ == "__main__":
    unittest.main()
