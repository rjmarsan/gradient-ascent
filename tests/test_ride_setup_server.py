import http.client
import json
import tempfile
import time
import unittest
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from unittest.mock import Mock, patch

from gradient_ascent.training_center_server import make_training_center_handler


@contextmanager
def local_server(workspace):
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_training_center_handler(workspace))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def request(port, method, path, body=None, *, token=None, origin=None, host=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Host": host or f"127.0.0.1:{port}"}
    if token:
        headers["x-coach-write-token"] = token
    if origin:
        headers["Origin"] = origin
    if body is not None:
        body = json.dumps(body)
        headers["Content-Type"] = "application/json"
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    raw = response.read()
    connection.close()
    try:
        value = json.loads(raw)
    except ValueError:
        value = {}
    return response.status, value


def wait_for(port, token, predicate):
    for _ in range(150):
        status, value = request(port, "GET", "/api/connections/ridewithgps/setup", token=token)
        if status == 200 and predicate(value):
            return value
        time.sleep(0.01)
    raise AssertionError("Setup did not reach the expected state")


class RideSetupServerTest(unittest.TestCase):
    def test_setup_requires_same_origin_token_and_exposes_only_clickable_vendor_url(self):
        from gradient_ascent import ride_setup

        entered = Event()
        url = "https://ridewithgps.com/oauth/authorize?state=synthetic"

        def connect(_workspace, **kwargs):
            kwargs["on_authorization_url"](url)
            entered.set()
            kwargs["cancel"].wait(3)
            raise ride_setup.RideConnectionError("Ride with GPS sign-in was cancelled.")

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(ride_setup, "connect_ride", side_effect=connect) as connector,
        ):
            with local_server(Path(tmp)) as port:
                _, initial = request(port, "GET", "/api/connections")
                token = initial["write_token"]
                endpoint = "/api/connections/ridewithgps/setup"
                self.assertEqual(request(port, "POST", endpoint, {"action": "connect"})[0], 403)
                self.assertEqual(
                    request(
                        port,
                        "POST",
                        endpoint,
                        {"action": "connect"},
                        token=token,
                        origin="https://evil.invalid",
                    )[0],
                    403,
                )
                self.assertEqual(
                    request(
                        port,
                        "POST",
                        endpoint,
                        {"action": "connect"},
                        token=token,
                        host="evil.invalid",
                    )[0],
                    403,
                )
                self.assertEqual(request(port, "GET", endpoint)[0], 403)
                self.assertEqual(
                    request(
                        port,
                        "POST",
                        endpoint,
                        {"action": "connect", "api_key": "NEVER_ACCEPT"},
                        token=token,
                    )[0],
                    400,
                )
                connector.assert_not_called()
                status, _ = request(
                    port, "POST", endpoint, {"action": "connect", "install": True}, token=token
                )
                self.assertEqual(status, 202)
                self.assertTrue(entered.wait(2))
                waiting = wait_for(port, token, lambda value: value.get("authorization_url") == url)
                self.assertNotIn(tmp, json.dumps(waiting))
                self.assertEqual(
                    request(port, "POST", endpoint, {"action": "cancel"}, token=token)[0], 202
                )
                completed = wait_for(port, token, lambda value: not value.get("running"))
                self.assertEqual(completed["status"], "cancelled")
                self.assertIsNone(completed["authorization_url"])
                connector.assert_called_once()

    def test_replaced_workspace_cannot_start_setup(self):
        from gradient_ascent import ride_setup

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            with (
                patch.object(ride_setup, "connect_ride") as connector,
                local_server(workspace) as port,
            ):
                _, initial = request(port, "GET", "/api/connections")
                workspace.rename(Path(tmp) / "previous")
                workspace.mkdir()
                status, _ = request(
                    port,
                    "POST",
                    "/api/connections/ridewithgps/setup",
                    {"action": "connect", "install": True},
                    token=initial["write_token"],
                )
                self.assertEqual(status, 403)
                connector.assert_not_called()

    def test_configured_sync_command_keeps_private_paths_out_of_visible_status(self):
        from gradient_ascent import training_center_server as dashboard

        result = Mock(
            returncode=0,
            stdout=json.dumps(
                {
                    "activities": 2,
                    "activity_candidates": 3,
                    "recovery": 1,
                    "provider_sync": {
                        "ridewithgps": {
                            "provider": "ridewithgps",
                            "status": "not_configured",
                            "external_access": False,
                        }
                    },
                }
            ),
        )
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(dashboard, "_ride_is_configured", return_value=True),
            patch.object(dashboard.subprocess, "run", return_value=result) as run,
        ):
            workspace = Path(tmp)
            with local_server(workspace) as port:
                _, initial = request(port, "GET", "/api/sync")
                request(port, "POST", "/api/sync", token=initial["write_token"])
                for _ in range(150):
                    _, status = request(port, "GET", "/api/sync")
                    if not status.get("running"):
                        break
                    time.sleep(0.01)
            self.assertTrue(status["ok"])
            self.assertIn("gradient_ascent.configured_refresh", run.call_args.args[0])
            self.assertNotIn(tmp, json.dumps(status))
            self.assertEqual(len(status["steps"]), 1)


if __name__ == "__main__":
    unittest.main()
