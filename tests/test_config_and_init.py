import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from gradient_ascent.cli import (
    WORKSPACE_GITIGNORE,
    _init_data_dir,
    _init_workspace,
    _install_goal_files,
)
from gradient_ascent.config import default_data_dir, ensure_private_output_path, load_config
from gradient_ascent.storage import write_text as storage_write_text


class ConfigAndInitTest(unittest.TestCase):
    @staticmethod
    def _synthetic_checkout(root: Path, *, project_name: str = "gradient-ascent") -> Path:
        checkout = root / "synthetic-checkout"
        (checkout / ".codex-plugin").mkdir(parents=True)
        (checkout / "gradient_ascent").mkdir()
        (checkout / ".codex-plugin" / "plugin.json").write_text(
            json.dumps({"name": project_name}), encoding="utf-8"
        )
        (checkout / "pyproject.toml").write_text(
            f'[project]\nname = "{project_name}"\n', encoding="utf-8"
        )
        (checkout / "gradient_ascent" / "config.py").write_text(
            "# Synthetic checkout marker.\n", encoding="utf-8"
        )
        return checkout

    def test_load_config_uses_only_the_private_workspace_path(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            config = load_config()
        self.assertEqual(config.data_dir, default_data_dir())
        self.assertEqual(set(vars(config)), {"data_dir"})

    def test_workspace_environment_takes_precedence_over_legacy_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            legacy = Path(tmp) / "legacy"
            with patch.dict(
                os.environ,
                {"COACH_WORKSPACE_DIR": str(workspace), "COACH_DATA_DIR": str(legacy)},
                clear=True,
            ):
                self.assertEqual(load_config().data_dir, workspace)

    def test_workspace_dotenv_can_select_the_current_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            (workspace / ".env").write_text("COACH_WORKSPACE_DIR=.\n", encoding="utf-8")
            previous = Path.cwd()
            try:
                os.chdir(workspace)
                with patch.dict(os.environ, {}, clear=True):
                    self.assertEqual(load_config().data_dir, Path("."))
            finally:
                os.chdir(previous)

    def test_init_data_creates_local_import_and_plan_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "workspace"
            result = _init_data_dir(data_dir)

            self.assertEqual(result["mode"], "empty")
            for relative in (
                "plan/athlete.json",
                "plan/goals.md",
                "plan/events.json",
                "plan/weeks.json",
                "strava/activities.json",
                "recordings/activities.json",
                "recordings/streams",
                "garmin",
                "apple_health",
                "imports/strava-export",
                "imports/activity-recordings",
                "imports/garmin-connect",
                "imports/apple-health",
                "connections/config.json",
            ):
                self.assertTrue((data_dir / relative).exists(), relative)
            self.assertFalse((data_dir / "connections" / "secrets").exists())
            self.assertEqual(
                json.loads((data_dir / "plan" / "coach_notes.json").read_text()),
                {"notes": [], "version": 1},
            )

    def test_init_workspace_creates_launcher_and_blank_onboarding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            result = _init_workspace(workspace, force=False)
            launcher = workspace / ".codex" / "bin" / "gradient-ascent"
            environment_path = workspace / ".codex" / "environments" / "environment.toml"

            self.assertEqual(result["data"]["mode"], "empty")
            self.assertTrue(launcher.stat().st_mode & 0o111)
            launcher_text = launcher.read_text(encoding="utf-8")
            self.assertIn(sys.executable, launcher_text)
            self.assertNotIn("MPLCONFIGDIR", launcher_text)
            environment = tomllib.loads(environment_path.read_text(encoding="utf-8"))
            self.assertEqual(environment["name"], "Gradient Ascent")
            self.assertIn("serve-training-center", environment["actions"][0]["command"])

            status = subprocess.run(
                [str(launcher), "onboarding-status", "--json"],
                cwd=workspace,
                env={**os.environ, "COACH_WORKSPACE_DIR": str(workspace)},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(status.returncode, 0, status.stderr or status.stdout)
            self.assertEqual(json.loads(status.stdout)["current_step"], "profile")

    def test_init_workspace_force_preserves_existing_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            env_path = workspace / ".env"
            env_path.write_text("COACH_WORKSPACE_DIR=.\nCUSTOM_SETTING=keep-me\n", encoding="utf-8")
            result = _init_workspace(workspace, force=True)
            self.assertFalse(result["env"]["created"])
            self.assertIn("CUSTOM_SETTING=keep-me", env_path.read_text(encoding="utf-8"))

    def test_repo_local_data_and_output_are_rejected(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit):
                _init_data_dir(repo_root / "private-data")
            with self.assertRaises(SystemExit):
                ensure_private_output_path(repo_root / "calendar.json", action="write test output")

    def test_installed_package_refuses_workspace_inside_separate_source_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkout = self._synthetic_checkout(root)
            installed_packages = root / "installed-site-packages"
            installed_packages.mkdir()
            shutil.copytree(
                Path(__file__).resolve().parents[1] / "gradient_ascent",
                installed_packages / "gradient_ascent",
            )
            target = checkout / "nested" / "private-data"
            env = {**os.environ, "PYTHONPATH": str(installed_packages)}
            env.pop("COACH_WORKSPACE_DIR", None)
            env.pop("COACH_DATA_DIR", None)

            result = subprocess.run(
                [sys.executable, "-m", "gradient_ascent.cli", "init-workspace", str(target)],
                cwd=root,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn("inside the Gradient Ascent checkout", result.stderr)
            self.assertFalse(target.exists())
            self.assertFalse((checkout / "nested").exists())

    def test_installed_package_refuses_checkout_output_and_resolved_symlink_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkout = self._synthetic_checkout(root)
            installed_packages = root / "installed-site-packages"
            installed_packages.mkdir()
            checkout_alias = root / "checkout-alias"
            checkout_alias.symlink_to(checkout, target_is_directory=True)

            with patch("gradient_ascent.config.repo_root", return_value=installed_packages):
                with self.assertRaisesRegex(SystemExit, "inside the Gradient Ascent checkout"):
                    _init_workspace(checkout, force=False)
                with self.assertRaisesRegex(SystemExit, "inside the Gradient Ascent checkout"):
                    ensure_private_output_path(checkout / "calendar.json", action="write output")
                with self.assertRaisesRegex(SystemExit, "inside the Gradient Ascent checkout"):
                    _init_workspace(checkout_alias / "private-data", force=False)

            self.assertFalse((checkout / "calendar.json").exists())
            self.assertFalse((checkout / "private-data").exists())

    def test_installed_package_allows_workspace_inside_an_unrelated_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unrelated_checkout = self._synthetic_checkout(root, project_name="another-plugin")
            installed_packages = root / "installed-site-packages"
            installed_packages.mkdir()
            target = unrelated_checkout / "private-workspace"

            with patch("gradient_ascent.config.repo_root", return_value=installed_packages):
                result = _init_workspace(target, force=False)

            self.assertEqual(result["workspace_dir"], str(target))
            self.assertTrue((target / "plan" / "athlete.json").is_file())

    def test_incomplete_or_malformed_checkout_markers_do_not_block_unrelated_workspaces(self) -> None:
        cases = {
            "malformed plugin manifest": lambda checkout: (
                checkout / ".codex-plugin" / "plugin.json"
            ).write_text("{not-json", encoding="utf-8"),
            "malformed project metadata": lambda checkout: (checkout / "pyproject.toml").write_text(
                "[project\n", encoding="utf-8"
            ),
            "invalid project shape": lambda checkout: (checkout / "pyproject.toml").write_text(
                'project = "not-a-table"\n', encoding="utf-8"
            ),
            "missing package marker": lambda checkout: (
                checkout / "gradient_ascent" / "config.py"
            ).unlink(),
        }
        for description, alter in cases.items():
            with self.subTest(case=description), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                checkout = self._synthetic_checkout(root)
                alter(checkout)
                installed_packages = root / "installed-site-packages"
                installed_packages.mkdir()
                target = checkout / "private-workspace"

                with patch("gradient_ascent.config.repo_root", return_value=installed_packages):
                    self.assertEqual(ensure_private_output_path(target), target)

    def test_workspace_gitignore_covers_all_raw_local_imports(self) -> None:
        for entry in (
            ".env",
            ".codex/cache/",
            "derived/.cache/",
            "strava/details/",
            "strava/laps/",
            "strava/streams/",
            "recordings/",
            "garmin/",
            "apple_health/",
            "integrations/",
            "imports/strava-export/",
            "imports/activity-recordings/",
            "imports/garmin-connect/",
            "imports/apple-health/",
        ):
            self.assertIn(entry, WORKSPACE_GITIGNORE)

    def test_existing_workspace_gitignore_gains_private_cache_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            gitignore = workspace / ".gitignore"
            gitignore.write_text("custom-private-path/\n", encoding="utf-8")

            _init_data_dir(workspace)
            first = gitignore.read_text(encoding="utf-8")
            _init_data_dir(workspace)
            second = gitignore.read_text(encoding="utf-8")

        self.assertIn("custom-private-path/", first)
        self.assertEqual(first.count(".codex/cache/"), 1)
        self.assertEqual(first.count("derived/.cache/"), 1)
        self.assertEqual(first.count("integrations/"), 1)
        self.assertEqual(second, first)

    def test_goal_files_update_through_validated_locked_cli_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            _init_workspace(workspace, force=False)
            goals_draft = root / "goals.md"
            measurement_draft = root / "goal_measurement.py"
            goals_draft.write_text("# Build durable climbing fitness\n", encoding="utf-8")
            measurement_draft.write_text(
                "def build_progress(context):\n    return {'title': 'Climbing'}\n",
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["COACH_WORKSPACE_DIR"] = str(workspace)

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "gradient_ascent.cli",
                    "update-goal-files",
                    "--goals-file",
                    str(goals_draft),
                    "--measurement-file",
                    str(measurement_draft),
                    "--no-rebuild",
                ],
                cwd=Path(__file__).resolve().parents[1],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertEqual(
                (workspace / "plan" / "goals.md").read_text(encoding="utf-8"),
                goals_draft.read_text(encoding="utf-8"),
            )
            self.assertEqual(
                (workspace / "plan" / "goal_measurement.py").read_text(encoding="utf-8"),
                measurement_draft.read_text(encoding="utf-8"),
            )

    def test_goal_file_update_restores_first_file_when_second_write_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            _init_workspace(workspace, force=False)
            goals_path = workspace / "plan" / "goals.md"
            measurement_path = workspace / "plan" / "goal_measurement.py"
            original_goals = goals_path.read_text(encoding="utf-8")
            original_measurement = measurement_path.read_text(encoding="utf-8")
            failed = False

            def fail_second_write(path: Path, content: str) -> None:
                nonlocal failed
                if path == measurement_path and not failed:
                    failed = True
                    raise OSError("injected measurement write failure")
                storage_write_text(path, content)

            with (
                patch("gradient_ascent.cli.write_text", side_effect=fail_second_write),
                self.assertRaisesRegex(OSError, "injected measurement write failure"),
            ):
                _install_goal_files(
                    workspace,
                    goals="# New goals\n",
                    measurement="def build_progress(context):\n    return {}\n",
                )

            self.assertEqual(goals_path.read_text(encoding="utf-8"), original_goals)
            self.assertEqual(
                measurement_path.read_text(encoding="utf-8"),
                original_measurement,
            )


if __name__ == "__main__":
    unittest.main()
