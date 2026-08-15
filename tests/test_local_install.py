import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = REPO_ROOT / "scripts" / "install-local-plugin.sh"


class LocalInstallTest(unittest.TestCase):
    def test_three_fresh_local_registrations_use_this_source_and_reach_blank_onboarding(self) -> None:
        for iteration in range(1, 4):
            with self.subTest(iteration=iteration), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                home = root / "home"
                codex_home = root / "codex-home"
                marketplace = root / "marketplace"
                workspace = root / "workspace"
                temp_dir = root / "tmp"
                home.mkdir()
                temp_dir.mkdir()
                env = {
                    "HOME": str(home),
                    "CODEX_HOME": str(codex_home),
                    "CODEX_CONFIG_FILE": str(codex_home / "config.toml"),
                    "GRADIENT_ASCENT_MARKETPLACE_ROOT": str(marketplace),
                    "COACH_WORKSPACE_DIR": str(workspace),
                    "TMPDIR": str(temp_dir),
                    "TMP": str(temp_dir),
                    "TEMP": str(temp_dir),
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONNOUSERSITE": "1",
                    "PYTHONPATH": str(REPO_ROOT),
                    "GRADIENT_ASCENT_SKIP_CLI_REGISTRATION": "1",
                    # Excludes the real Codex binary so registration remains confined
                    # to the isolated files written by the installer.
                    "PATH": "/usr/bin:/bin",
                }

                installed = subprocess.run(
                    [str(INSTALLER)],
                    cwd=REPO_ROOT,
                    env=env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=30,
                    check=False,
                )
                self.assertEqual(installed.returncode, 0, installed.stderr or installed.stdout)

                plugin_link = marketplace / "plugins" / "gradient-ascent"
                self.assertTrue(plugin_link.is_dir())
                self.assertFalse(plugin_link.is_symlink())
                self.assertTrue((plugin_link / ".codex-plugin" / "plugin.json").is_file())
                self.assertTrue((plugin_link / "skills" / "gradient-ascent" / "SKILL.md").is_file())
                activity_setup = (
                    plugin_link / "skills" / "coach-setup-activities" / "SKILL.md"
                ).read_text(encoding="utf-8")
                self.assertIn(
                    "Never ask for provider credentials",
                    activity_setup,
                )
                self.assertIn("strava/streams/<activity_id>.json", activity_setup)
                self.assertNotIn("does not currently convert", activity_setup)
                for excluded in (".git", ".venv", "gradient_ascent", "tests", "examples"):
                    self.assertFalse((plugin_link / excluded).exists(), excluded)
                marketplace_payload = json.loads(
                    (marketplace / ".agents" / "plugins" / "marketplace.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(marketplace_payload["plugins"][0]["name"], "gradient-ascent")
                self.assertNotIn(
                    "authentication",
                    marketplace_payload["plugins"][0]["policy"],
                )
                self.assertIn(
                    '[plugins."gradient-ascent@local"]',
                    (codex_home / "config.toml").read_text(encoding="utf-8"),
                )

                imported = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        "from pathlib import Path; import gradient_ascent; "
                        "print(Path(gradient_ascent.__file__).resolve())",
                    ],
                    cwd=root,
                    env=env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=30,
                    check=False,
                )
                self.assertEqual(imported.returncode, 0, imported.stderr)
                self.assertEqual(Path(imported.stdout.strip()).parents[1], REPO_ROOT)

                initialized = subprocess.run(
                    [sys.executable, "-m", "gradient_ascent.cli", "init-workspace", str(workspace)],
                    cwd=root,
                    env=env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=30,
                    check=False,
                )
                self.assertEqual(initialized.returncode, 0, initialized.stderr or initialized.stdout)
                status = subprocess.run(
                    [sys.executable, "-m", "gradient_ascent.cli", "onboarding-status", "--json"],
                    cwd=workspace,
                    env=env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=30,
                    check=False,
                )
                self.assertEqual(status.returncode, 0, status.stderr or status.stdout)
                payload = json.loads(status.stdout)
                self.assertEqual(payload["current_step"], "profile")
                self.assertEqual(
                    payload["summary"],
                    {"activities": 0, "events": 0, "planned_weeks": 0},
                )
                self.assertFalse((workspace / ".git").exists())

    def test_installer_runs_plugin_add_and_verifies_enabled_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            codex_home = root / "codex-home"
            marketplace = root / "marketplace"
            fake_codex = root / "fake-codex"
            command_log = root / "codex-commands.log"
            home.mkdir()
            fake_codex.write_text(
                "#!/bin/sh\n"
                'echo "$*" >> "$FAKE_CODEX_LOG"\n'
                'if [ "$1" = "--version" ]; then echo "codex-cli test"; exit 0; fi\n'
                'if [ "$1" = "plugin" ] && [ "$2" = "list" ]; then\n'
                '  echo "gradient-ascent@local  installed, enabled, version 0.1.0"\n'
                "fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            fake_codex.chmod(0o755)
            env = {
                "HOME": str(home),
                "CODEX_HOME": str(codex_home),
                "CODEX_CONFIG_FILE": str(codex_home / "config.toml"),
                "GRADIENT_ASCENT_MARKETPLACE_ROOT": str(marketplace),
                "GRADIENT_ASCENT_CODEX_BIN": str(fake_codex),
                "FAKE_CODEX_LOG": str(command_log),
                "PATH": "/usr/bin:/bin",
            }

            installed = subprocess.run(
                [str(INSTALLER)],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )

            self.assertEqual(installed.returncode, 0, installed.stderr or installed.stdout)
            commands = command_log.read_text(encoding="utf-8")
            self.assertIn("plugin marketplace add", commands)
            self.assertIn("plugin add gradient-ascent@local", commands)
            self.assertIn("plugin list", commands)
            self.assertIn("installed and enabled through codex CLI", installed.stdout)

    def test_installer_verifies_matching_cache_when_cli_list_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            codex_home = root / "codex-home"
            marketplace = root / "marketplace"
            fake_codex = root / "fake-codex"
            home.mkdir()
            manifest = json.loads(
                (REPO_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
            )
            cache_root = (
                codex_home
                / "plugins"
                / "cache"
                / "local"
                / "gradient-ascent"
                / manifest["version"]
            )
            cache_root.mkdir(parents=True)
            for directory in (".codex-plugin", "skills"):
                shutil.copytree(REPO_ROOT / directory, cache_root / directory)
            for filename in ("LICENSE", "README.md"):
                shutil.copy2(REPO_ROOT / filename, cache_root / filename)
            fake_codex.write_text(
                "#!/bin/sh\n"
                'if [ "$1" = "--version" ]; then echo "codex-cli test"; exit 0; fi\n'
                'if [ "$1" = "plugin" ] && [ "$2" = "marketplace" ]; then exit 0; fi\n'
                'echo "unrelated marketplace snapshot failed" >&2\n'
                "exit 1\n",
                encoding="utf-8",
            )
            fake_codex.chmod(0o755)
            env = {
                "HOME": str(home),
                "CODEX_HOME": str(codex_home),
                "CODEX_CONFIG_FILE": str(codex_home / "config.toml"),
                "GRADIENT_ASCENT_MARKETPLACE_ROOT": str(marketplace),
                "GRADIENT_ASCENT_CODEX_BIN": str(fake_codex),
                "GRADIENT_ASCENT_SKIP_CLI_REGISTRATION": "1",
                "PATH": "/usr/bin:/bin",
            }

            installed = subprocess.run(
                [str(INSTALLER)],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )

            self.assertEqual(installed.returncode, 0, installed.stderr or installed.stdout)
            self.assertIn("cache matches current bundle", installed.stdout)
            self.assertNotIn("manual verification needed", installed.stdout)

    def test_installer_fails_honestly_when_cli_registration_cannot_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            codex_home = root / "codex-home"
            marketplace = root / "marketplace"
            fake_codex = root / "fake-codex"
            home.mkdir()
            fake_codex.write_text(
                "#!/bin/sh\n"
                'if [ "$1" = "--version" ]; then echo "codex-cli test"; exit 0; fi\n'
                'echo "registration unavailable on this host" >&2\n'
                "exit 1\n",
                encoding="utf-8",
            )
            fake_codex.chmod(0o755)
            env = {
                "HOME": str(home),
                "CODEX_HOME": str(codex_home),
                "CODEX_CONFIG_FILE": str(codex_home / "config.toml"),
                "GRADIENT_ASCENT_MARKETPLACE_ROOT": str(marketplace),
                "GRADIENT_ASCENT_CODEX_BIN": str(fake_codex),
                "PATH": "/usr/bin:/bin",
            }

            installed = subprocess.run(
                [str(INSTALLER)],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )

            self.assertNotEqual(installed.returncode, 0)
            self.assertIn("registration unavailable on this host", installed.stderr)
            self.assertNotIn("Installed gradient-ascent for Codex", installed.stdout)
            self.assertTrue((marketplace / "plugins" / "gradient-ascent").is_dir())
            self.assertTrue((codex_home / "config.toml").is_file())


if __name__ == "__main__":
    unittest.main()
