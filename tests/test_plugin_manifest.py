import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class PluginManifestTest(unittest.TestCase):
    def test_repo_gitignore_covers_private_data_workspace(self) -> None:
        ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("private-data/", ignored)

    def test_manifest_and_python_package_share_gradient_ascent_identity(self) -> None:
        manifest = json.loads((REPO_ROOT / ".codex-plugin" / "plugin.json").read_text())
        project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        setup_prompt = (REPO_ROOT / "SETUP_PROMPT.md").read_text(encoding="utf-8")

        self.assertEqual(manifest["name"], "gradient-ascent")
        self.assertEqual(manifest["interface"]["displayName"], "Gradient Ascent")
        self.assertEqual(project["project"]["name"], "gradient-ascent")
        self.assertRegex(manifest["version"], r"^\d+\.\d+\.\d+$")
        self.assertEqual(manifest["version"], project["project"]["version"])
        self.assertIn("gradient-ascent", project["project"]["scripts"])
        self.assertIn("Gradient Ascent", setup_prompt)

    def test_manifest_has_no_connector_or_placeholder_contact(self) -> None:
        manifest = json.loads((REPO_ROOT / ".codex-plugin" / "plugin.json").read_text())

        self.assertNotIn("mcpServers", manifest)
        self.assertNotIn("authentication", json.dumps(manifest).lower())
        self.assertNotIn(".invalid", json.dumps(manifest).lower())
        self.assertEqual(manifest["author"], {"name": "Gradient Ascent Contributors"})

    def test_manifest_discovers_optional_official_ride_cli_and_local_imports(self) -> None:
        manifest = json.loads((REPO_ROOT / ".codex-plugin" / "plugin.json").read_text())
        interface = manifest["interface"]

        self.assertIn("ridewithgps", manifest["keywords"])
        for description in (
            manifest["description"],
            interface["shortDescription"],
            interface["longDescription"],
        ):
            with self.subTest(description=description):
                self.assertIn("Ride with GPS", description)
                self.assertIn("optional", description.lower())
        self.assertIn("official ride CLI", interface["longDescription"])
        self.assertIn("Strava and Garmin account archives", interface["longDescription"])
        self.assertIn("does not imply vendor endorsement", interface["longDescription"])
        activity_prompt = next(
            prompt for prompt in interface["defaultPrompt"] if "$coach-setup-activities" in prompt
        )
        self.assertIn("official ride CLI", activity_prompt)
        self.assertIn("Strava archive", activity_prompt)

    def test_hash_locked_install_files_are_documented(self) -> None:
        for filename in ("requirements.lock", "requirements-build.lock"):
            lock_path = REPO_ROOT / filename
            self.assertTrue(lock_path.is_file(), filename)
            lock_text = lock_path.read_text(encoding="utf-8")
            self.assertIn("--hash=sha256:", lock_text)
            self.assertIn("==", lock_text)

        install = (REPO_ROOT / "INSTALL.md").read_text(encoding="utf-8")
        self.assertIn("--require-hashes -r requirements-build.lock", install)
        self.assertIn("--require-hashes -r requirements.lock", install)
        self.assertIn("--no-build-isolation --no-deps -e .", install)

    def test_setup_prompt_uses_checkout_venv_and_hands_off_after_plugin_install(self) -> None:
        setup_prompt = (REPO_ROOT / "SETUP_PROMPT.md").read_text(encoding="utf-8")

        self.assertIn(
            ".venv/bin/python -m pip install --require-hashes -r requirements-build.lock",
            setup_prompt,
        )
        self.assertIn(
            ".venv/bin/python -m pip install --require-hashes -r requirements.lock",
            setup_prompt,
        )
        self.assertIn(
            ".venv/bin/python -m pip install --no-build-isolation --no-deps -e .",
            setup_prompt,
        )
        self.assertIn("with venv support", setup_prompt)
        self.assertNotIn("with `python -m pip", setup_prompt)
        self.assertIn("paste this same prompt into a new Codex task", setup_prompt)

    def test_public_docs_cover_fresh_clone_and_remote_file_boundaries(self) -> None:
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        install = (REPO_ROOT / "INSTALL.md").read_text(encoding="utf-8")

        self.assertIn("git clone https://github.com/rjmarsan/gradient-ascent.git", readme)
        self.assertIn("devbox port-forward <devbox-name> --ports 8787:8787", install)
        self.assertIn(
            "devbox upload <devbox-name> ./sample-ride.fit "
            "/tmp/gradient-ascent-inputs/sample-ride.fit --mkdir",
            install,
        )
        self.assertIn("Paths such as `~/Downloads` refer to the remote host", install)
        self.assertIn("Use synthetic data for a clean-room remote test", install)
        self.assertIn("python3-venv", install)

    def test_public_docs_explain_optional_provider_neutral_companion_sync(self) -> None:
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        install = (REPO_ROOT / "INSTALL.md").read_text(encoding="utf-8")

        for filename, contents in (("README.md", readme), ("INSTALL.md", install)):
            with self.subTest(filename=filename):
                self.assertIn("import-sync-manifest", contents)
                self.assertIn("versioned", contents.lower())
                self.assertIn("optional", contents.lower())
                self.assertIn("credentials", contents.lower())

        self.assertIn("provider-neutral", readme)
        self.assertIn("separate companion", readme.lower())
        self.assertIn("unofficial connectors", readme.lower())

        manifest_section = readme.split("### Optional companion sync", 1)[1]
        manifest_example = manifest_section.split("```json\n", 1)[1].split("\n```", 1)[0]
        manifest = json.loads(manifest_example)
        self.assertEqual(manifest["version"], 1)
        self.assertEqual(manifest["provider"]["id"], "ride-service")
        self.assertEqual(manifest["activities"][0]["sport_type"], "Ride")
        self.assertEqual(manifest["recovery"], [])

    def test_setup_and_refresh_skills_keep_companion_sync_local_and_optional(self) -> None:
        setup_prompt = (REPO_ROOT / "SETUP_PROMPT.md").read_text(encoding="utf-8")
        setup_skill = (
            REPO_ROOT / "skills" / "gradient-ascent" / "SKILL.md"
        ).read_text(encoding="utf-8")
        refresh_skill = (
            REPO_ROOT / "skills" / "coach-sync-refresh" / "SKILL.md"
        ).read_text(encoding="utf-8")

        self.assertIn("optional companion", setup_prompt.lower())
        self.assertIn("local sync manifest", setup_prompt.lower())
        for filename, contents in (
            ("skills/gradient-ascent/SKILL.md", setup_skill),
            ("skills/coach-sync-refresh/SKILL.md", refresh_skill),
        ):
            with self.subTest(filename=filename):
                self.assertIn("import-sync-manifest", contents)
                self.assertIn("optional", contents.lower())
                self.assertIn("credentials", contents.lower())

        self.assertIn("onboarding-choice activities external_sync", setup_skill)

    def test_copied_fit_fixture_preserves_upstream_license_notice(self) -> None:
        notice = (REPO_ROOT / "tests" / "fixtures" / "fitdecode-LICENSE.txt").read_text(
            encoding="utf-8"
        )
        self.assertIn("Copyright (c) 2018-present Jean-Charles Lefebvre", notice)
        self.assertIn("Permission is hereby granted, free of charge", notice)
        self.assertIn('THE SOFTWARE IS PROVIDED "AS IS"', notice)

    def test_advisor_skill_uses_neutral_lenses_and_no_raw_payloads(self) -> None:
        advice = (REPO_ROOT / "skills" / "coach-advice" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("strava/streams/<id>.json", advice)
        self.assertNotIn("strava/laps/<id>.json", advice)
        self.assertNotIn("strava/details/<id>.json", advice)
        self.assertIn("professional cyclist lens", advice.lower())
        self.assertIn("endurance coach lens", advice.lower())
        self.assertIn("exercise physiology and recovery lens", advice.lower())
        self.assertIn("do not impersonate", advice.lower())

    def test_advisor_context_works_with_isolated_standard_library_python(self) -> None:
        advice = (REPO_ROOT / "skills" / "coach-advice" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        command = advice.split("```bash\n", 1)[1].split("\n```", 1)[0]
        script = command.split("\n", 1)[1].rsplit("\nPY", 1)[0]

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "athlete-workspace"
            (workspace / ".codex" / "bin").mkdir(parents=True)
            (workspace / ".codex" / "bin" / "gradient-ascent").write_text(
                "#!/bin/sh\n", encoding="utf-8"
            )
            (workspace / "derived").mkdir()
            (workspace / "plan").mkdir()
            (workspace / "derived" / "post_sync_summary.json").write_text(
                json.dumps({"activities": 3}), encoding="utf-8"
            )
            (workspace / "derived" / "weekly.json").write_text(
                json.dumps(
                    [{"start_date": "2026-08-10", "totals": {"activity_count": 3}}]
                ),
                encoding="utf-8",
            )
            (workspace / "plan" / "goals.md").write_text(
                "# Build cycling consistency\n", encoding="utf-8"
            )
            (workspace / ".env").write_text(
                "SECRET=never-display-me\n", encoding="utf-8"
            )
            env = os.environ.copy()
            env.pop("COACH_WORKSPACE_DIR", None)
            env.pop("PYTHONPATH", None)

            result = subprocess.run(
                [sys.executable, "-I", "-S", "-c", script],
                cwd=workspace,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(command.startswith("python3 - <<'PY'"))
        self.assertIn("## derived/post_sync_summary.json", result.stdout)
        self.assertIn("Build cycling consistency", result.stdout)
        self.assertIn("2026-08-10", result.stdout)
        self.assertNotIn(str(workspace), result.stdout)
        self.assertNotIn("never-display-me", result.stdout)

    def test_advisor_context_refuses_a_directory_without_a_workspace_launcher(self) -> None:
        advice = (REPO_ROOT / "skills" / "coach-advice" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        command = advice.split("```bash\n", 1)[1].split("\n```", 1)[0]
        script = command.split("\n", 1)[1].rsplit("\nPY", 1)[0]

        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env.pop("COACH_WORKSPACE_DIR", None)
            env.pop("PYTHONPATH", None)
            result = subprocess.run(
                [sys.executable, "-I", "-S", "-c", script],
                cwd=tmp,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("initialized private Gradient Ascent workspace", result.stderr)


if __name__ == "__main__":
    unittest.main()
