from __future__ import annotations

import http.client
import io
import json
import tempfile
import unittest
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import patch

from gradient_ascent.training_center_server import make_training_center_handler
from tests.test_plan_export import make_plan


class PlanExportServerTest(unittest.TestCase):
    def test_dashboard_has_an_explicit_plan_only_download_flow(self) -> None:
        from gradient_ascent.training_center import HTML_TEMPLATE

        for expected in (
            'id="export-planned-schedule"',
            'id="plan-export-dialog"',
            'id="plan-export-start"',
            'id="plan-export-end"',
            'const PLAN_EXPORT_API = "./api/plan/export"',
            'headers: apiHeaders({ "content-type": "application/json", "accept": "application/octet-stream" })',
            "Only explicitly defined intervals become device workouts",
        ):
            self.assertTrue(expected in HTML_TEMPLATE, expected)

    def test_download_requires_same_origin_token_and_returns_only_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "athlete"
            make_plan(root)
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_training_center_handler(root))
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host = f"127.0.0.1:{server.server_address[1]}"
            conn = http.client.HTTPConnection(host)
            try:
                conn.request("GET", "/api/sync")
                token = json.loads(conn.getresponse().read())["write_token"]
                body = json.dumps({"format": "zip", "start": "2026-08-17", "end": "2026-08-23"})
                conn.request(
                    "POST",
                    "/api/plan/export",
                    body=body,
                    headers={"Content-Type": "application/json"},
                )
                response = conn.getresponse()
                response.read()
                self.assertEqual(response.status, 403)
                headers = {
                    "Content-Type": "application/json",
                    "x-coach-write-token": token,
                    "Origin": "https://evil.invalid",
                }
                conn.request("POST", "/api/plan/export", body=body, headers=headers)
                response = conn.getresponse()
                response.read()
                self.assertEqual(response.status, 403)
                headers["Origin"] = f"http://{host}"
                with patch("subprocess.Popen", side_effect=AssertionError("offline export")):
                    conn.request("POST", "/api/plan/export", body=body, headers=headers)
                    response = conn.getresponse()
                    data = response.read()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.getheader("Content-Type"), "application/zip")
                self.assertIn("attachment; filename=", response.getheader("Content-Disposition"))
                self.assertEqual(response.getheader("X-Gradient-Ascent-Plan-Entries"), "3")
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    self.assertIn("schedule.ics", archive.namelist())
                self.assertFalse((root / "exports").exists())
                for invalid in (
                    {"format": "zip", "path": "/tmp/leak"},
                    {"format": "exe"},
                    {"format": "fit"},
                    {"start": True},
                ):
                    conn.request(
                        "POST", "/api/plan/export", body=json.dumps(invalid), headers=headers
                    )
                    response = conn.getresponse()
                    response.read()
                    self.assertEqual(response.status, 400)
                root.rename(Path(tmp) / "old-athlete")
                make_plan(root)
                conn.request("POST", "/api/plan/export", body=body, headers=headers)
                response = conn.getresponse()
                response.read()
                self.assertEqual(response.status, 403)
            finally:
                conn.close()
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
