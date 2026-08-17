import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gradient_ascent import cli
from gradient_ascent.config import Config


class RideCommandsTest(unittest.TestCase):
    def test_setup_prints_user_clickable_link_and_delegates_without_secrets(self):
        from gradient_ascent import ride_connection

        url = "https://ridewithgps.com/oauth/authorize?state=synthetic"

        def connect(_workspace, **kwargs):
            kwargs["on_authorization_url"](url)
            self.assertTrue(kwargs["install"])
            self.assertTrue(kwargs["force_login"])
            return {"enabled": True, "status": "connected"}

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(cli, "load_config", return_value=Config(Path(tmp))),
            patch.object(ride_connection, "connect_ride", side_effect=connect),
            patch("sys.argv", ["gradient-ascent", "ride", "setup", "--install", "--reauth"]),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            cli.main()
        self.assertIn(url, output.getvalue())
        self.assertIn('"enabled":true', output.getvalue())

    def test_history_requires_explicit_mode_and_is_resumable(self):
        from gradient_ascent import configured_refresh, ride_connection

        result = {
            "canonical": {"activities": 3, "resolved_activities": 2, "recovery": 1},
            "provider_sync": {
                "ridewithgps": {
                    "provider": "ridewithgps",
                    "status": "not_configured",
                    "external_access": False,
                }
            },
        }
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(cli, "load_config", return_value=Config(Path(tmp))),
            patch.object(ride_connection, "load_ride_settings", return_value={"enabled": True}),
            patch.object(
                configured_refresh, "refresh_configured_workspace", return_value=result
            ) as refresh,
            patch(
                "sys.argv", ["gradient-ascent", "ride", "sync", "--history", "--restart-history"]
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            cli.main()
            self.assertTrue(refresh.call_args.kwargs["ride_history"])
            self.assertTrue(refresh.call_args.kwargs["restart_history"])
        with (
            patch("sys.argv", ["gradient-ascent", "ride", "sync", "--restart-history"]),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            cli._parse_args()

    def test_refresh_has_an_explicit_offline_escape_hatch(self):
        from gradient_ascent import configured_refresh

        result = {
            "canonical": {"activities": 0, "resolved_activities": 0, "recovery": 0},
            "provider_sync": {
                "ridewithgps": {
                    "provider": "ridewithgps",
                    "status": "local_only",
                    "external_access": False,
                }
            },
        }
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(cli, "load_config", return_value=Config(Path(tmp))),
            patch.object(
                configured_refresh, "refresh_configured_workspace", return_value=result
            ) as refresh,
            patch("sys.argv", ["gradient-ascent", "refresh", "--local-only"]),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            cli.main()
        self.assertTrue(refresh.call_args.kwargs["local_only"])


if __name__ == "__main__":
    unittest.main()
