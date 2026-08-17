import tempfile
import time
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch


def wait_until(predicate, timeout=3):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Setup worker did not reach its expected state")


class RideSetupTest(unittest.TestCase):
    def test_sign_in_link_is_temporary_and_duplicate_click_does_not_start_twice(self):
        from gradient_ascent import ride_setup

        release = Event()
        called = []
        url = "https://ridewithgps.com/oauth/authorize?state=synthetic"

        def connect(_workspace, **kwargs):
            called.append(kwargs)
            kwargs["on_authorization_url"](url)
            release.wait(2)
            return {"enabled": True, "status": "connected"}

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(ride_setup, "connect_ride", side_effect=connect),
        ):
            jobs = ride_setup.RideSetupJobs(Path(tmp), (1, 2))
            jobs.start("connect", install=True)
            wait_until(lambda: jobs.snapshot().get("authorization_url") == url)
            jobs.start("connect", install=True)
            self.assertEqual(len(called), 1)
            self.assertTrue(called[0]["install"])
            self.assertEqual(called[0]["expected_identity"], (1, 2))
            release.set()
            wait_until(lambda: not jobs.snapshot()["running"])
            result = jobs.snapshot()
        self.assertEqual(result["status"], "completed")
        self.assertIsNone(result["authorization_url"])
        self.assertNotIn(tmp, str(result))

    def test_cancel_clears_link_and_signals_vendor_process(self):
        from gradient_ascent import ride_setup

        entered = Event()

        def connect(_workspace, **kwargs):
            kwargs["on_authorization_url"](
                "https://ridewithgps.com/oauth/authorize?state=synthetic"
            )
            entered.set()
            kwargs["cancel"].wait(2)
            raise ride_setup.RideConnectionError("Ride with GPS sign-in was cancelled.")

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(ride_setup, "connect_ride", side_effect=connect),
        ):
            jobs = ride_setup.RideSetupJobs(Path(tmp), (1, 2))
            jobs.start("connect")
            self.assertTrue(entered.wait(2))
            self.assertIsNone(jobs.cancel()["authorization_url"])
            wait_until(lambda: not jobs.snapshot()["running"])
            self.assertEqual(jobs.snapshot()["status"], "cancelled")

    def test_unexpected_worker_error_is_sanitized(self):
        from gradient_ascent import ride_setup

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(
                ride_setup, "check_ride", side_effect=OSError("PRIVATE TOKEN /Users/private")
            ),
        ):
            jobs = ride_setup.RideSetupJobs(Path(tmp), (1, 2))
            jobs.start("check")
            wait_until(lambda: not jobs.snapshot()["running"])
            result = jobs.snapshot()
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("PRIVATE TOKEN", str(result))
        self.assertNotIn("/Users/", str(result))


if __name__ == "__main__":
    unittest.main()
