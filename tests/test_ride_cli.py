from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from gradient_ascent import ride_cli as ride


def authorization_url(**changes):
    params = {
        "response_type": "code",
        "client_id": "ride-cli",
        "redirect_uri": "http://127.0.0.1:43125/oauth/callback",
        "code_challenge": "a" * 43,
        "code_challenge_method": "S256",
        "state": "b" * 32,
    }
    params.update(changes)
    return "https://ridewithgps.com/oauth/authorize?" + urlencode(params)


class RideCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.root.chmod(0o700)
        self.executable = self.root / "ride"
        self.auth = self.root / "auth"
        self.auth.mkdir(mode=0o700)

    def binary(self, source):
        data = (f"#!{sys.executable}\n" + source + "\n").encode()
        self.executable.write_bytes(data)
        self.executable.chmod(0o700)
        asset = ride.ReleaseAsset("ride-test", len(data), hashlib.sha256(data).hexdigest())
        self.asset_patch = patch.object(ride, "_platform_asset", return_value=asset)
        self.asset_patch.start()
        self.addCleanup(self.asset_patch.stop)
        return data

    def test_release_and_official_endpoint_constants(self):
        self.assertEqual(ride.RIDE_VERSION, "0.1.0")
        self.assertEqual(ride.RIDE_API_ORIGIN, "https://ridewithgps.com")
        self.assertEqual(ride.CURRENT_USER_PATH, "/api/v1/users/current")
        self.assertEqual(len(ride.RELEASE_ASSETS), 3)
        self.assertTrue(all(len(asset.sha256) == 64 for asset in ride.RELEASE_ASSETS.values()))

    def test_binary_validation_hash_permissions_and_revalidation(self):
        self.binary("print('{}')")
        self.assertEqual(ride.find_ride_cli(self.executable), self.executable)
        client = ride.RideCLI(self.executable)
        self.executable.chmod(0o720)
        with self.assertRaises(ride.RideCLIError):
            client.get_json(ride.CURRENT_USER_PATH)
        self.executable.chmod(0o700)
        self.executable.write_text("#!/bin/false\n")
        with self.assertRaises(ride.RideCLIError):
            ride.find_ride_cli(self.executable)

    def test_symlink_and_writable_parent_rejected(self):
        self.binary("print('{}')")
        link = self.root / "alias"
        link.symlink_to(self.executable)
        with self.assertRaises(ride.RideCLIError):
            ride.find_ride_cli(link)
        self.root.chmod(0o777)
        with self.assertRaises(ride.RideCLIError):
            ride.find_ride_cli(self.executable)

    def test_api_exact_args_environment_default_config_and_private_override(self):
        self.binary(
            "import json,os,sys; print(json.dumps({'args':sys.argv[1:],'env':{k:v for k,v in os.environ.items() if k.startswith('RIDE_')},'path':os.environ.get('PATH'),'proxy':os.environ.get('HTTPS_PROXY')}))"
        )
        env = {
            "RIDE_API_URL": "https://wrong.invalid",
            "RIDE_CONFIG_DIR": "/wrong",
            "RIDE_OAUTH_CLIENT_ID": "wrong",
            "HTTPS_PROXY": "http://proxy.invalid",
            "PATH": "/usr/bin:/bin",
        }
        with patch.dict(os.environ, env):
            result = ride.RideCLI(self.executable).get_json(
                "/api/v1/trips.json", {"page": 100000, "page_size": 200}
            )
            private = ride.RideCLI(self.executable, config_dir=self.auth).get_json(
                ride.CURRENT_USER_PATH
            )
        self.assertEqual(
            result["args"],
            [
                "api",
                "get",
                "/api/v1/trips.json",
                "--query",
                "page=100000",
                "--query",
                "page_size=200",
            ],
        )
        self.assertEqual(result["env"], {"RIDE_API_URL": ride.RIDE_API_ORIGIN})
        self.assertEqual(result["path"], "/usr/bin:/bin")
        self.assertEqual(result["proxy"], "http://proxy.invalid")
        self.assertEqual(private["env"]["RIDE_CONFIG_DIR"], str(self.auth))

    def test_api_rejects_nonallowlisted_request_before_child(self):
        self.binary("raise RuntimeError('must not run')")
        client = ride.RideCLI(self.executable)
        bad = [
            ("https://ridewithgps.com/api/v1/trips.json", {}),
            ("/api/v1/users/current.json", {}),
            ("/api/v1/trips/0.json", {}),
            ("/api/v1/trips/1.json?x=y", {}),
            ("/api/v1/trips/1.json", {"anything": 1}),
            ("/api/v1/trips.json", {"page": True, "page_size": 20}),
            ("/api/v1/trips.json", {"page": 100001, "page_size": 20}),
            ("/api/v1/trips.json", {"page": 1, "page_size": 201}),
        ]
        with patch.object(subprocess, "Popen", side_effect=AssertionError("child started")):
            for path, params in bad:
                with self.subTest(path=path, params=params), self.assertRaises(ride.RideCLIError):
                    client.get_json(path, params)

    def test_api_errors_are_bounded_and_redacted(self):
        self.binary(
            "import sys; print('SECRET-RIDER-RAW'); print('SECRET-TOKEN',file=sys.stderr); sys.exit(7)"
        )
        with self.assertRaises(ride.RideCLIError) as caught:
            ride.RideCLI(self.executable).get_json(ride.CURRENT_USER_PATH)
        self.assertNotIn("SECRET", str(caught.exception))
        with patch.object(ride, "MAX_RESPONSE_BYTES", 4), self.assertRaises(ride.RideCLIError):
            ride.RideCLI(self.executable).get_json(ride.CURRENT_USER_PATH)

    def test_login_exposes_only_valid_url_and_cannot_launch_browser(self):
        marker = self.root / "opened"
        helper = self.root / "open"
        helper.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
        helper.chmod(0o700)
        url = authorization_url()
        self.binary(
            "import os,subprocess,sys\ntry: subprocess.run(['open','wrong-profile'],check=False)\nexcept OSError: pass\nprint('private name email',flush=True)\nprint("
            + repr(url)
            + ",flush=True)\nprint('secret token',file=sys.stderr)"
        )
        links = []
        with patch.dict(
            os.environ, {"PATH": str(self.root) + os.pathsep + os.environ.get("PATH", "")}
        ):
            ride.RideCLI(self.executable, config_dir=self.auth).login(links.append)
        self.assertEqual(links, [url])
        self.assertFalse(marker.exists())

    def test_login_url_validation_rejects_secret_or_external_urls(self):
        valid = authorization_url()
        self.assertEqual(ride._authorization_url(valid), valid)
        for value in [
            valid.replace("ridewithgps.com", "evil.invalid"),
            authorization_url(code="secret"),
            authorization_url(code_challenge_method="plain"),
            authorization_url(redirect_uri="https://evil.invalid/callback"),
            authorization_url(client_id="another-app"),
            valid + "#secret",
        ]:
            self.assertIsNone(ride._authorization_url(value))

    def test_login_cancel_and_profile_lock(self):
        self.binary("import time; time.sleep(30)")
        client = ride.RideCLI(self.executable, config_dir=self.auth)
        cancel = threading.Event()
        cancel.set()
        start = time.monotonic()
        with self.assertRaisesRegex(ride.RideCLIError, "cancel"):
            client.login(lambda url: None, cancel=cancel)
        self.assertLess(time.monotonic() - start, 2)
        with ride._login_lock(self.auth):
            with self.assertRaisesRegex(ride.RideCLIError, "already in progress"):
                client.login(lambda url: None, timeout_seconds=1)

    def test_login_reauthentication_is_explicit(self):
        self.binary("print('{}')")
        client = ride.RideCLI(self.executable, config_dir=self.auth)
        with patch.object(ride, "_run_bounded", return_value=b"") as run:
            client.login(lambda url: None)
            self.assertEqual(run.call_args.args[0], [str(self.executable), "login", "--no-browser"])
            client.login(lambda url: None, reauth=True)
            self.assertEqual(
                run.call_args.args[0], [str(self.executable), "login", "--no-browser", "--reauth"]
            )
        for timeout in [True, float("nan"), float("inf"), 0, 301]:
            with self.subTest(timeout=timeout), self.assertRaises(ride.RideCLIError):
                client.login(lambda url: None, timeout_seconds=timeout)

    def test_install_requires_consent_and_verifies_before_publish(self):
        data = b"synthetic-vendor-binary"
        asset = ride.ReleaseAsset("ride-test", len(data), hashlib.sha256(data).hexdigest())
        destination = self.root / "installed" / "ride"
        with (
            patch.object(ride, "_platform_asset", return_value=asset),
            patch.object(ride, "_download_asset", return_value=io.BytesIO(data)) as download,
        ):
            with self.assertRaises(ride.RideCLIError):
                ride.install_ride_cli(destination)
            download.assert_not_called()
            self.assertFalse(destination.parent.exists())
            self.assertEqual(ride.install_ride_cli(destination, confirmed=True), destination)
        self.assertEqual(destination.read_bytes(), data)
        self.assertEqual(destination.stat().st_mode & 0o777, 0o700)

    def test_install_bad_hash_never_leaves_partial_destination(self):
        asset = ride.ReleaseAsset("ride-test", 4, "0" * 64)
        destination = self.root / "installed" / "ride"
        with (
            patch.object(ride, "_platform_asset", return_value=asset),
            patch.object(ride, "_download_asset", return_value=io.BytesIO(b"bad!")),
        ):
            with self.assertRaises(ride.RideCLIError):
                ride.install_ride_cli(destination, confirmed=True)
        self.assertFalse(destination.exists())
        self.assertEqual(list(destination.parent.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
