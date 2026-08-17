import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from gradient_ascent.workspace_lock import workspace_identity


def sync_summary(**changes):
    return {
        "provider": "ridewithgps",
        "mode": "recent",
        "pages": 1,
        "listed": 2,
        "eligible": 2,
        "imported": 1,
        "updated": 0,
        "existing": 1,
        "skipped": 0,
        "streams": 1,
        "laps": 1,
        "complete": True,
        "next_page": None,
        "next_offset": 0,
        "has_more": False,
        **changes,
    }


class RideConnectionTest(unittest.TestCase):
    def test_default_is_offline_and_status_contains_no_local_paths(self):
        from gradient_ascent import ride_connection as ride

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            with (
                patch.object(ride, "find_ride_cli", side_effect=ride.RideCLIError("missing")),
                patch.object(ride, "RideCLI") as client,
            ):
                status = ride.ride_status(workspace)
                result = ride.sync_configured_ride(workspace)

            self.assertFalse(status["enabled"])
            self.assertFalse(status["installed"])
            self.assertEqual(result["status"], "not_configured")
            self.assertFalse(result["external_access"])
            self.assertNotIn(tmp, json.dumps(status))

            client.assert_not_called()
            self.assertFalse((workspace / ride.SETTINGS_PATH).exists())

    def test_connect_reuses_vendor_login_and_persists_only_nonsecret_settings(self):
        from gradient_ascent import ride_connection as ride

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            executable = workspace / "vendor" / "ride"
            auth_dir = workspace / "vendor-auth"
            client = Mock()
            client.get_json.return_value = {
                "user": {"id": 123, "name": "PRIVATE RIDER", "email": "private@example.invalid"}
            }
            with (
                patch.object(ride, "find_ride_cli", return_value=executable),
                patch.object(ride, "RideCLI", return_value=client),
            ):
                status = ride.connect_ride(workspace, executable=executable, config_dir=auth_dir)

            client.login.assert_not_called()
            client.get_json.assert_called_once_with(ride.CURRENT_USER_PATH)
            settings = json.loads((workspace / ride.SETTINGS_PATH).read_text())
            self.assertTrue(settings["enabled"])
            self.assertEqual(settings["executable"], str(executable))
            self.assertEqual(settings["config_dir"], str(auth_dir))
            self.assertTrue(status["enabled"])
            self.assertEqual((workspace / ride.SETTINGS_PATH).stat().st_mode & 0o777, 0o600)
            ignored = (workspace / ".gitignore").read_text().splitlines()
            self.assertIn(".runtime/", ignored)
            self.assertIn("connections/ridewithgps.json", ignored)
            for forbidden in (
                "PRIVATE RIDER",
                "private@example.invalid",
                "access_token",
                "refresh_token",
            ):
                self.assertNotIn(forbidden, json.dumps(settings))
                self.assertNotIn(forbidden, json.dumps(status))
            self.assertNotIn(tmp, json.dumps(status))

    def test_switching_vendor_account_cannot_mix_athlete_histories(self):
        from gradient_ascent import ride_connection as ride

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            executable = workspace / "vendor" / "ride"
            client = Mock()
            client.get_json.return_value = {"user": {"id": 123}}
            with (
                patch.object(ride, "find_ride_cli", return_value=executable),
                patch.object(ride, "RideCLI", return_value=client),
            ):
                ride.connect_ride(workspace, executable=executable)
                before = ride.load_ride_settings(workspace)["account_fingerprint"]
                client.get_json.return_value = {"user": {"id": 456, "name": "OTHER RIDER"}}
                with patch.object(ride, "sync_ridewithgps") as importer:
                    with self.assertRaisesRegex(ride.RideConnectionError, "account") as error:
                        ride.sync_configured_ride(workspace)
                    importer.assert_not_called()
                with self.assertRaisesRegex(ride.RideConnectionError, "account"):
                    ride.connect_ride(workspace, executable=executable)
            self.assertNotIn("OTHER RIDER", str(error.exception))
            self.assertEqual(ride.load_ride_settings(workspace)["account_fingerprint"], before)

    def test_install_requires_explicit_consent_and_login_exposes_only_the_link(self):
        from gradient_ascent import ride_connection as ride

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            executable = workspace / ".runtime" / "tools" / "ride" / "0.1.0" / "ride"
            url = "https://ridewithgps.com/oauth/authorize?synthetic=1"
            client = Mock()
            client.get_json.side_effect = [ride.RideCLIError("not signed in"), {"user": {"id": 1}}]
            client.login.side_effect = lambda callback, **_kwargs: callback(url)
            links = []
            with (
                patch.object(ride, "find_ride_cli", side_effect=ride.RideCLIError("missing")),
                patch.object(ride, "install_ride_cli", return_value=executable) as installer,
                patch.object(ride, "RideCLI", return_value=client),
            ):
                with self.assertRaises(ride.RideConnectionError):
                    ride.connect_ride(workspace)
                installer.assert_not_called()
                status = ride.connect_ride(
                    workspace, install=True, on_authorization_url=links.append
                )

            installer.assert_called_once_with(executable, confirmed=True)
            self.assertEqual(links, [url])
            self.assertTrue(status["enabled"])
            self.assertNotIn("authorization_url", json.dumps(status))

    def test_disable_never_logs_out_or_removes_vendor_session(self):
        from gradient_ascent import ride_connection as ride

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            executable = workspace / "vendor" / "ride"
            client = Mock()
            client.get_json.return_value = {"user": {"id": 1}}
            with (
                patch.object(ride, "find_ride_cli", return_value=executable),
                patch.object(ride, "RideCLI", return_value=client),
            ):
                ride.connect_ride(workspace, executable=executable)
                client.reset_mock()
                status = ride.disable_ride(workspace)
                result = ride.sync_configured_ride(workspace)
            self.assertFalse(status["enabled"])
            self.assertEqual(result["status"], "not_configured")
            client.assert_not_called()
            self.assertEqual(ride.load_ride_settings(workspace)["executable"], str(executable))

    def test_replaced_workspace_fails_before_provider_or_installer(self):
        from gradient_ascent import ride_connection as ride

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            identity = workspace_identity(workspace)
            workspace.rename(Path(tmp) / "previous")
            workspace.mkdir()
            with (
                patch.object(ride, "RideCLI") as client,
                patch.object(ride, "install_ride_cli") as installer,
            ):
                with self.assertRaises(RuntimeError):
                    ride.connect_ride(workspace, install=True, expected_identity=identity)
            client.assert_not_called()
            installer.assert_not_called()

    def test_workspace_replaced_during_login_receives_no_settings_or_followup_request(self):
        from gradient_ascent import ride_connection as ride

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            executable = Path(tmp) / "vendor" / "ride"
            client = Mock()
            client.get_json.side_effect = [ride.RideCLIError("not signed in"), {"user": {"id": 1}}]

            def replace_workspace(*_args, **_kwargs):
                workspace.rename(Path(tmp) / "previous")
                workspace.mkdir()

            client.login.side_effect = replace_workspace
            with (
                patch.object(ride, "find_ride_cli", return_value=executable),
                patch.object(ride, "RideCLI", return_value=client),
            ):
                with self.assertRaises(RuntimeError):
                    ride.connect_ride(
                        workspace, executable=executable, on_authorization_url=lambda _url: None
                    )
            self.assertEqual(client.get_json.call_count, 1)
            self.assertFalse((workspace / ride.SETTINGS_PATH).exists())

    def test_workspace_replaced_during_account_check_is_not_imported_or_rebuilt(self):
        from gradient_ascent import configured_refresh, ride_connection as ride

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            executable = Path(tmp) / "vendor" / "ride"
            client = Mock()
            client.get_json.return_value = {"user": {"id": 1}}
            with (
                patch.object(ride, "find_ride_cli", return_value=executable),
                patch.object(ride, "RideCLI", return_value=client),
            ):
                ride.connect_ride(workspace, executable=executable)

                def replace_workspace(*_args, **_kwargs):
                    workspace.rename(Path(tmp) / "previous")
                    workspace.mkdir()
                    return {"user": {"id": 1}}

                client.get_json.side_effect = replace_workspace
                with (
                    patch.object(ride, "sync_ridewithgps") as importer,
                    patch.object(configured_refresh, "refresh_workspace") as rebuild,
                ):
                    with self.assertRaises(RuntimeError):
                        configured_refresh.refresh_configured_workspace(workspace)
                    importer.assert_not_called()
                    rebuild.assert_not_called()
            self.assertFalse((workspace / ride.SETTINGS_PATH).exists())

    def test_failed_account_request_after_replacement_does_not_start_login(self):
        from gradient_ascent import ride_connection as ride

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            executable = Path(tmp) / "vendor" / "ride"
            client = Mock()

            def replace_then_fail(*_args, **_kwargs):
                workspace.rename(Path(tmp) / "previous")
                workspace.mkdir()
                raise ride.RideCLIError("not signed in")

            client.get_json.side_effect = replace_then_fail
            with (
                patch.object(ride, "find_ride_cli", return_value=executable),
                patch.object(ride, "RideCLI", return_value=client),
            ):
                with self.assertRaises(RuntimeError):
                    ride.connect_ride(
                        workspace, executable=executable, on_authorization_url=lambda _url: None
                    )
            client.login.assert_not_called()
            self.assertFalse((workspace / ride.SETTINGS_PATH).exists())

    def test_enabled_refresh_syncs_before_exactly_one_local_rebuild(self):
        from gradient_ascent import configured_refresh, ride_connection as ride

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            executable = workspace / "vendor" / "ride"
            client = Mock()
            client.get_json.return_value = {"user": {"id": 1}}
            events = []
            with (
                patch.object(ride, "find_ride_cli", return_value=executable),
                patch.object(ride, "RideCLI", return_value=client),
            ):
                ride.connect_ride(workspace, executable=executable)
                with (
                    patch.object(
                        ride,
                        "sync_ridewithgps",
                        side_effect=lambda *_args, **_kwargs: events.append("ride")
                        or sync_summary(),
                    ),
                    patch.object(
                        configured_refresh,
                        "refresh_workspace",
                        side_effect=lambda *_args, **_kwargs: events.append("rebuild")
                        or {
                            "canonical": {"activities": 2, "resolved_activities": 2, "recovery": 0}
                        },
                    ) as rebuild,
                ):
                    result = configured_refresh.refresh_configured_workspace(workspace)

            self.assertEqual(events, ["ride", "rebuild"])
            rebuild.assert_called_once()
            self.assertTrue(result["provider_sync"]["ridewithgps"]["external_access"])
            self.assertEqual(result["provider_sync"]["ridewithgps"]["imported"], 1)
            self.assertNotIn(tmp, json.dumps(result["provider_sync"]))

    def test_failed_or_malformed_provider_result_never_rebuilds_or_exposes_raw_output(self):
        from gradient_ascent import configured_refresh, ride_connection as ride

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            executable = workspace / "vendor" / "ride"
            client = Mock()
            client.get_json.return_value = {"user": {"id": 1}}
            with (
                patch.object(ride, "find_ride_cli", return_value=executable),
                patch.object(ride, "RideCLI", return_value=client),
            ):
                ride.connect_ride(workspace, executable=executable)
                for response in (sync_summary(raw_gps="PRIVATE GPS"), sync_summary(imported=True)):
                    with (
                        self.subTest(response=response),
                        patch.object(ride, "sync_ridewithgps", return_value=response),
                        patch.object(configured_refresh, "refresh_workspace") as rebuild,
                    ):
                        with self.assertRaises(ride.RideConnectionError) as error:
                            configured_refresh.refresh_configured_workspace(workspace)
                        self.assertNotIn("PRIVATE GPS", str(error.exception))
                        rebuild.assert_not_called()


if __name__ == "__main__":
    unittest.main()
