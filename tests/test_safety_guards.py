import gzip
import http.client
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Thread
from unittest.mock import patch
from zipfile import ZipFile

from gradient_ascent.cli import _init_workspace
from gradient_ascent.training_center_server import (
    _is_loopback_host,
    _origin_matches_host,
    _remove_transient_loopback_proxies,
    _sync_steps,
    _workspace_instance_id,
    make_training_center_handler,
    serve_training_center,
)
from gradient_ascent.workspace import purge_workspace_data
from gradient_ascent.workspace_lock import workspace_lock


REPO_ROOT = Path(__file__).resolve().parents[1]


class SafetyGuardsTest(unittest.TestCase):
    def test_cli_exposes_local_imports_without_login_or_network_sync_commands(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "gradient_ascent.cli", "--help"],
            cwd=REPO_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stdout)
        for supported in (
            "import-strava-export",
            "import-activity-recording",
            "import-sync-manifest",
            "import-apple-health-export",
            "import-garmin-export",
            "import-calendar",
        ):
            self.assertIn(supported, result.stdout)

    def test_cli_output_override_refuses_repo_local_path(self) -> None:
        output_path = REPO_ROOT / f"calendar-private-test-{os.getpid()}.json"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "gradient_ascent.cli",
                "import-calendar",
                "examples/calendar/sample-training-calendar.csv",
                "--out",
                str(output_path),
            ],
            cwd=REPO_ROOT,
            env={
                **os.environ,
                "COACH_WORKSPACE_DIR": str(Path(tempfile.gettempdir()) / "gradient-ascent-workspace"),
                "PYTHONPATH": str(REPO_ROOT),
            },
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Refusing to write imported calendar", result.stdout)
        self.assertFalse(output_path.exists())

    def test_server_has_no_remote_bind_argument(self) -> None:
        import inspect

        parameters = tuple(inspect.signature(serve_training_center).parameters)
        self.assertEqual(parameters, ("data_dir", "port", "rebuild", "fallback_ports"))

    def test_training_center_reports_port_collision_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind(("127.0.0.1", 0))
                sock.listen(1)
                port = sock.getsockname()[1]
                with self.assertRaises(SystemExit) as exc:
                    serve_training_center(
                        Path(tmp),
                        port=port,
                        rebuild=False,
                        fallback_ports=0,
                    )
        self.assertIn(f"tried {port}", str(exc.exception))

    def test_training_center_health_identifies_workspace(self) -> None:
        from http.server import ThreadingHTTPServer

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            expected_workspace_id = _workspace_instance_id(data_dir)
            handler = make_training_center_handler(data_dir)
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1])
                conn.request("GET", "/api/health")
                response = conn.getresponse()
                payload = json.loads(response.read())
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["service"], "gradient-ascent-training-center")
        self.assertEqual(payload["workspace_id"], expected_workspace_id)
        self.assertEqual(response.getheader("cross-origin-resource-policy"), "same-origin")
        self.assertEqual(response.getheader("x-content-type-options"), "nosniff")

    def test_training_center_gzips_large_static_assets(self) -> None:
        from http.server import ThreadingHTTPServer

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            derived_dir = data_dir / "derived"
            derived_dir.mkdir()
            source = b"window.__TEST_DATA__ = " + (b'"ride"' * 10_000) + b";\n"
            (derived_dir / "training_center_data.js").write_bytes(source)
            handler = make_training_center_handler(data_dir)
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1])
                conn.request(
                    "GET",
                    "/training_center_data.js",
                    headers={"Accept-Encoding": "gzip"},
                )
                response = conn.getresponse()
                body = response.read()
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("content-encoding"), "gzip")
        self.assertEqual(response.getheader("vary"), "Accept-Encoding")
        self.assertEqual(gzip.decompress(body), source)
        self.assertLess(len(body), len(source) // 5)

    def test_every_request_requires_a_loopback_host_header(self) -> None:
        from http.server import ThreadingHTTPServer

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            handler = make_training_center_handler(data_dir)
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]

                missing = http.client.HTTPConnection("127.0.0.1", port)
                missing.putrequest("GET", "/api/health", skip_host=True)
                missing.endheaders()
                missing_response = missing.getresponse()
                missing_response.read()

                rebound = http.client.HTTPConnection("127.0.0.1", port)
                rebound.putrequest("GET", "/api/health", skip_host=True)
                rebound.putheader("Host", "attacker.example")
                rebound.endheaders()
                rebound_response = rebound.getresponse()
                rebound_response.read()

                valid = http.client.HTTPConnection("127.0.0.1", port)
                valid.request("GET", "/api/health", headers={"Host": f"localhost:{port}"})
                valid_response = valid.getresponse()
                valid_response.read()
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

        self.assertEqual(missing_response.status, 403)
        self.assertEqual(rebound_response.status, 403)
        self.assertEqual(valid_response.status, 200)

    def test_loopback_and_origin_checks_are_strict(self) -> None:
        self.assertTrue(_is_loopback_host("127.0.0.1"))
        self.assertTrue(_is_loopback_host("localhost"))
        self.assertTrue(_is_loopback_host("[::1]:8787"))
        self.assertFalse(_is_loopback_host("0.0.0.0"))
        self.assertFalse(_is_loopback_host("localhost.example"))
        self.assertFalse(_is_loopback_host("127.0.0.1.example"))
        self.assertFalse(_is_loopback_host("[::1]example"))
        self.assertFalse(_is_loopback_host("localhost:not-a-port"))
        self.assertTrue(
            _origin_matches_host("http://127.0.0.1:8787/training_center.html", "127.0.0.1:8787")
        )
        self.assertFalse(
            _origin_matches_host("http://example.invalid:8787/", "127.0.0.1:8787")
        )

    def test_launch_scoped_loopback_proxy_is_removed_by_default(self) -> None:
        env = {
            "HTTP_PROXY": "http://127.0.0.1:61275",
            "HTTPS_PROXY": "http://127.0.0.1:61275",
            "NO_PROXY": "localhost,127.0.0.1",
        }
        self.assertTrue(_remove_transient_loopback_proxies(env))
        self.assertEqual(env, {"NO_PROXY": "localhost,127.0.0.1"})

    def test_dashboard_refresh_runs_only_the_local_workspace_rebuild(self) -> None:
        workspace = Path("/tmp/gradient-ascent-workspace")
        self.assertEqual(
            _sync_steps(workspace),
            [
                (
                    "Workspace rebuild",
                    [
                        sys.executable,
                        "-m",
                        "gradient_ascent.refresh",
                        "--data-dir",
                        str(workspace),
                    ],
                    False,
                )
            ],
        )
        guarded_command = _sync_steps(workspace, (12, 34))[0][1]
        self.assertEqual(
            guarded_command[-4:],
            [
                "--expected-workspace-device",
                "12",
                "--expected-workspace-inode",
                "34",
            ],
        )

    def test_writes_require_loopback_host_origin_and_ephemeral_token(self) -> None:
        from http.server import ThreadingHTTPServer

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            handler = make_training_center_handler(data_dir)
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            port = server.server_address[1]
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host = f"127.0.0.1:{port}"
                conn = http.client.HTTPConnection("127.0.0.1", port)
                conn.request("GET", "/api/daily-notes", headers={"Host": host})
                response = conn.getresponse()
                token = json.loads(response.read())["write_token"]

                body = json.dumps({"note": "token-protected note"})
                conn.request(
                    "PUT",
                    "/api/daily-notes/2026-04-13",
                    body=body,
                    headers={
                        "Host": host,
                        "Origin": f"http://{host}",
                        "Content-Type": "application/json",
                        "x-coach-write-token": token,
                    },
                )
                response = conn.getresponse()
                response.read()
                self.assertEqual(response.status, 200)

                conn.request(
                    "PUT",
                    "/api/daily-notes/2026-04-14",
                    body=body,
                    headers={"Host": host, "Origin": f"http://{host}", "Content-Type": "application/json"},
                )
                response = conn.getresponse()
                response.read()
                self.assertEqual(response.status, 403)

                conn.request("GET", "/api/connections", headers={"Host": host})
                response = conn.getresponse()
                payload = json.loads(response.read())
                self.assertEqual([item["key"] for item in payload["providers"]], ["strava", "apple_health", "garmin"])

                conn.request(
                    "PUT",
                    "/api/connections/apple_health",
                    body=json.dumps({"fields": {"export_path": "/tmp/apple-health-export"}}),
                    headers={
                        "Host": host,
                        "Origin": f"http://{host}",
                        "Content-Type": "application/json",
                        "x-coach-write-token": token,
                    },
                )
                response = conn.getresponse()
                payload = json.loads(response.read())
                self.assertEqual(response.status, 200)
                self.assertTrue(payload["provider"]["configured_fields"]["export_path"])
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_daily_note_write_waits_for_workspace_snapshot_lock(self) -> None:
        from http.server import ThreadingHTTPServer

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "workspace"
            _init_workspace(data_dir, force=False)
            handler = make_training_center_handler(data_dir)
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            port = server.server_address[1]
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host = f"127.0.0.1:{port}"
                conn = http.client.HTTPConnection("127.0.0.1", port)
                conn.request("GET", "/api/daily-notes", headers={"Host": host})
                token = json.loads(conn.getresponse().read())["write_token"]

                def write_note() -> int:
                    request = http.client.HTTPConnection("127.0.0.1", port)
                    request.request(
                        "PUT",
                        "/api/daily-notes/2026-04-13",
                        body=json.dumps({"note": "serialized note"}),
                        headers={
                            "Host": host,
                            "Origin": f"http://{host}",
                            "Content-Type": "application/json",
                            "x-coach-write-token": token,
                        },
                    )
                    response = request.getresponse()
                    response.read()
                    request.close()
                    return response.status

                with ThreadPoolExecutor(max_workers=1) as executor:
                    with workspace_lock(data_dir):
                        future = executor.submit(write_note)
                        time.sleep(0.1)
                        self.assertFalse(future.done())
                    self.assertEqual(future.result(timeout=5), 200)
                payload = json.loads(
                    (data_dir / "plan" / "daily_notes.json").read_text(encoding="utf-8")
                )
                self.assertEqual(payload["notes"]["2026-04-13"]["note"], "serialized note")
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_live_server_cannot_recreate_a_purged_workspace(self) -> None:
        from http.server import ThreadingHTTPServer

        recording = b"""<?xml version="1.0" encoding="UTF-8"?>
<gpx xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>
<trkpt lat="37.0" lon="-122.0"><time>2026-05-02T08:00:00Z</time></trkpt>
</trkseg></trk></gpx>"""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            _init_workspace(workspace, force=False)
            old_workspace_id = _workspace_instance_id(workspace)
            handler = make_training_center_handler(workspace)
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            port = server.server_address[1]
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host = f"127.0.0.1:{port}"
                conn = http.client.HTTPConnection("127.0.0.1", port)
                conn.request("GET", "/api/connections", headers={"Host": host})
                token = json.loads(conn.getresponse().read())["write_token"]
                headers = {
                    "Host": host,
                    "Origin": f"http://{host}",
                    "Content-Type": "application/json",
                    "x-coach-write-token": token,
                }
                purge_workspace_data(
                    workspace,
                    confirmation=str(workspace.resolve()),
                )
                self.assertFalse(workspace.exists())

                conn.request(
                    "PUT",
                    "/api/connections/apple_health",
                    body=json.dumps({"fields": {"export_path": "/tmp/export"}}),
                    headers=headers,
                )
                response = conn.getresponse()
                response.read()
                self.assertEqual(response.status, 403)
                self.assertFalse(workspace.exists())

                conn.request(
                    "PUT",
                    "/api/daily-notes/2026-04-13",
                    body=json.dumps({"note": "must not return"}),
                    headers=headers,
                )
                response = conn.getresponse()
                response.read()
                self.assertEqual(response.status, 403)
                self.assertFalse(workspace.exists())

                upload_headers = {
                    **headers,
                    "Content-Type": "application/gpx+xml",
                    "x-coach-upload-name": "purged.gpx",
                }
                conn.request(
                    "POST",
                    "/api/activity-recordings",
                    body=recording,
                    headers=upload_headers,
                )
                response = conn.getresponse()
                response.read()
                self.assertEqual(response.status, 403)
                self.assertFalse(workspace.exists())

                _init_workspace(workspace, force=False)
                self.assertNotEqual(_workspace_instance_id(workspace), old_workspace_id)
                conn.request(
                    "PUT",
                    "/api/daily-notes/2026-04-13",
                    body=json.dumps({"note": "old server must stay invalid"}),
                    headers=headers,
                )
                response = conn.getresponse()
                response.read()
                self.assertEqual(response.status, 403)
                notes = json.loads(
                    (workspace / "plan" / "daily_notes.json").read_text(encoding="utf-8")
                )
                self.assertEqual(notes["notes"], {})
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_live_server_rejects_old_token_when_recreated_inode_is_reused(self) -> None:
        from http.server import ThreadingHTTPServer

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            _init_workspace(workspace, force=False)
            resolved_workspace = workspace.resolve()
            original_inode = workspace.stat().st_ino
            original_stat = Path.stat

            def reused_inode(path: Path, *args, **kwargs):
                result = original_stat(path, *args, **kwargs)
                if path in (workspace, resolved_workspace):
                    values = list(result)
                    values[stat.ST_INO] = original_inode
                    return os.stat_result(values)
                return result

            with patch.object(Path, "stat", reused_inode):
                old_workspace_id = _workspace_instance_id(workspace)
                handler = make_training_center_handler(workspace)
                server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
                port = server.server_address[1]
                thread = Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    host = f"127.0.0.1:{port}"
                    conn = http.client.HTTPConnection("127.0.0.1", port)
                    conn.request("GET", "/api/connections", headers={"Host": host})
                    token = json.loads(conn.getresponse().read())["write_token"]
                    purge_workspace_data(workspace, confirmation=str(workspace.resolve()))
                    _init_workspace(workspace, force=False)

                    conn.request(
                        "PUT",
                        "/api/daily-notes/2026-04-13",
                        body=json.dumps({"note": "stale server must not write"}),
                        headers={
                            "Host": host,
                            "Origin": f"http://{host}",
                            "Content-Type": "application/json",
                            "x-coach-write-token": token,
                        },
                    )
                    response = conn.getresponse()
                    response.read()

                    self.assertEqual(response.status, 403)
                    self.assertNotEqual(_workspace_instance_id(workspace), old_workspace_id)
                    notes = json.loads(
                        (workspace / "plan" / "daily_notes.json").read_text(encoding="utf-8")
                    )
                    self.assertEqual(notes["notes"], {})
                finally:
                    server.shutdown()
                    thread.join(timeout=5)
                    server.server_close()

    def test_strava_archive_upload_requires_token_and_stays_private(self) -> None:
        from http.server import ThreadingHTTPServer

        activities_csv = (
            "Activity ID,Activity Date,Activity Name,Activity Type,Distance\n"
            '123,"May 1, 2026, 8:00:00 AM",Morning Ride,Ride,30.5\n'
        )
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            archive_path = data_dir / "source.zip"
            with ZipFile(archive_path, "w") as archive:
                archive.writestr("export/activities.csv", activities_csv)
            archive_bytes = archive_path.read_bytes()
            handler = make_training_center_handler(data_dir)
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            port = server.server_address[1]
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host = f"127.0.0.1:{port}"
                conn = http.client.HTTPConnection("127.0.0.1", port)
                conn.request("GET", "/api/connections", headers={"Host": host})
                token = json.loads(conn.getresponse().read())["write_token"]

                headers = {
                    "Host": host,
                    "Origin": f"http://{host}",
                    "Content-Type": "application/zip",
                    "x-coach-upload-name": "strava-export.zip",
                }
                conn.request("POST", "/api/connections/strava/archive", body=archive_bytes, headers=headers)
                response = conn.getresponse()
                response.read()
                self.assertEqual(response.status, 403)

                headers["x-coach-write-token"] = token
                conn.request("POST", "/api/connections/strava/archive", body=archive_bytes, headers=headers)
                response = conn.getresponse()
                payload = json.loads(response.read())
                self.assertEqual(response.status, 200)
                self.assertEqual(payload["import"]["created"], 1)
                uploads = list((data_dir / "imports" / "strava-export").glob("*.zip"))
                self.assertEqual(len(uploads), 1)
                self.assertEqual(stat.S_IMODE(uploads[0].stat().st_mode), 0o600)
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_activity_recording_upload_requires_token_and_stays_private(self) -> None:
        from http.server import ThreadingHTTPServer

        recording = b"""<?xml version="1.0" encoding="UTF-8"?>
<gpx xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>
<trkpt lat="37.0" lon="-122.0"><time>2026-05-02T08:00:00Z</time></trkpt>
<trkpt lat="37.001" lon="-122.001"><time>2026-05-02T08:01:00Z</time></trkpt>
</trkseg></trk></gpx>"""
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            summary_path = data_dir / "derived" / "post_sync_summary.json"
            summary_path.parent.mkdir(parents=True)
            summary_path.write_text(
                json.dumps(
                    {
                        "generated_at": "2026-05-01T00:00:00+00:00",
                        "sources": {"strava": {"count": 7}},
                        "imports": {"garmin": {"days_written": 2}},
                        "canonical": {"activities": 0, "resolved_activities": 0, "recovery": 0},
                    }
                ),
                encoding="utf-8",
            )
            handler = make_training_center_handler(data_dir)
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            port = server.server_address[1]
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host = f"127.0.0.1:{port}"
                conn = http.client.HTTPConnection("127.0.0.1", port)
                conn.request("GET", "/api/connections", headers={"Host": host})
                token = json.loads(conn.getresponse().read())["write_token"]
                headers = {
                    "Host": host,
                    "Origin": f"http://{host}",
                    "Content-Type": "application/gpx+xml",
                    "x-coach-upload-name": "morning-ride.gpx",
                }

                conn.request("POST", "/api/activity-recordings", body=recording, headers=headers)
                response = conn.getresponse()
                response.read()
                self.assertEqual(response.status, 403)

                headers["x-coach-write-token"] = token
                conn.request("POST", "/api/activity-recordings", body=recording, headers=headers)
                response = conn.getresponse()
                payload = json.loads(response.read())
                self.assertEqual(response.status, 200)
                self.assertTrue(payload["import"]["created"])
                self.assertEqual(payload["canonical"]["resolved_activities"], 1)
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
                self.assertEqual(summary["canonical"]["activities"], 1)
                self.assertEqual(summary["canonical"]["resolved_activities"], 1)
                self.assertEqual(summary["sources"]["strava"]["count"], 7)
                self.assertEqual(summary["imports"]["garmin"]["days_written"], 2)
                dashboard_data = (
                    (data_dir / "derived" / "training_center_data.js")
                    .read_text(encoding="utf-8")
                    .removeprefix("window.__COACH_TRAINING_CENTER_DATA__ = ")
                    .removesuffix(";\n")
                )
                self.assertEqual(json.loads(dashboard_data)["postSyncSummary"]["canonical"]["activities"], 1)
                uploads = list((data_dir / "imports" / "activity-recordings").glob("*.gpx"))
                self.assertEqual(len(uploads), 1)
                self.assertEqual(stat.S_IMODE(uploads[0].stat().st_mode), 0o600)

                conn.request("POST", "/api/activity-recordings", body=recording, headers=headers)
                response = conn.getresponse()
                repeated = json.loads(response.read())
                self.assertEqual(response.status, 200)
                self.assertFalse(repeated["import"]["created"])
                self.assertTrue(repeated["upload"]["deduplicated"])
                self.assertEqual(
                    json.loads(summary_path.read_text(encoding="utf-8"))["canonical"]["activities"],
                    1,
                )
                self.assertEqual(
                    len(list((data_dir / "imports" / "activity-recordings").glob("*.gpx"))),
                    1,
                )
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()


if __name__ == "__main__":
    unittest.main()
