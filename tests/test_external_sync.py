import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from gradient_ascent.external_sync import (
    MAX_MANIFEST_BYTES,
    import_sync_manifest,
    load_external_sync_manifests,
)
from gradient_ascent.workspace_lock import workspace_lock


def _manifest() -> dict:
    return {
        "version": 1,
        "provider": {"id": "ride-service", "label": "Ride Service"},
        "synced_at": "2026-05-01T15:00:00Z",
        "activities": [
            {
                "id": "ride-1",
                "name": "Morning ride",
                "sport_type": "Ride",
                "date": "2026-05-01",
                "start_date_local": "2026-05-01T08:00:00-07:00",
                "moving_time_s": 3600,
                "distance_m": 25000,
            }
        ],
        "recovery": [
            {
                "id": "daily-1",
                "date": "2026-05-01",
                "resting_hr": 49,
                "hrv_ms": 61,
                "sleep_duration_s": 27000,
            }
        ],
    }


class ExternalSyncTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.source = self.root / "manifest.json"

    def _write_manifest(self, manifest: dict | None = None) -> None:
        self.source.write_text(json.dumps(manifest or _manifest()), encoding="utf-8")

    def test_import_writes_private_manifest_and_truthful_provider_provenance(self) -> None:
        self._write_manifest()
        (self.workspace / ".gitignore").write_text("canonical/\n", encoding="utf-8")

        result = import_sync_manifest(self.workspace, self.source)
        destination = self.workspace / "integrations" / "ride-service" / "manifest.json"
        stored = json.loads(destination.read_text(encoding="utf-8"))

        self.assertEqual(result["provider"], "ride-service")
        self.assertEqual(result["label"], "Ride Service")
        self.assertEqual(result["activities"], 1)
        self.assertEqual(result["recovery"], 1)
        self.assertEqual(result["path"], str(destination))
        self.assertEqual(stored["activities"][0]["id"], "ride-1")
        self.assertEqual(
            stored["activities"][0]["source"],
            {"provider": "ride-service", "record_id": "ride-1", "confidence": "high"},
        )
        self.assertEqual(stored["recovery"][0]["source"]["provider"], "ride-service")
        self.assertEqual(stored["synced_at"], "2026-05-01T15:00:00Z")
        self.assertIn("integrations/", (self.workspace / ".gitignore").read_text())
        self.assertEqual(load_external_sync_manifests(self.workspace), [stored])

    def test_missing_sync_timestamp_receives_utc_default(self) -> None:
        manifest = _manifest()
        manifest.pop("synced_at")
        self._write_manifest(manifest)

        result = import_sync_manifest(self.workspace, self.source)

        self.assertIn("synced_at", result)
        self.assertTrue(result["synced_at"].endswith("+00:00"))

    def test_rejects_unknown_versions_and_fields_without_writing(self) -> None:
        for mutation in (
            lambda item: item.update(version=2),
            lambda item: item.update(version=True),
            lambda item: item.update(version=1.0),
            lambda item: item.update(unexpected=True),
            lambda item: item["provider"].update(unexpected=True),
            lambda item: item["activities"][0].update(unexpected=True),
            lambda item: item["recovery"][0].update(unexpected=True),
        ):
            with self.subTest(mutation=mutation):
                manifest = _manifest()
                mutation(manifest)
                self._write_manifest(manifest)
                with self.assertRaises(ValueError):
                    import_sync_manifest(self.workspace, self.source)
                self.assertFalse((self.workspace / "integrations").exists())

    def test_rejects_credentials_at_every_level(self) -> None:
        for target, key in (
            ("root", "access_token"),
            ("provider", "client_secret"),
            ("activity", "password"),
            ("recovery", "api_key"),
            ("source", "cookie"),
            ("provider", "client_id"),
        ):
            with self.subTest(target=target, key=key):
                manifest = _manifest()
                destination = {
                    "root": manifest,
                    "provider": manifest["provider"],
                    "activity": manifest["activities"][0],
                    "recovery": manifest["recovery"][0],
                    "source": manifest["activities"][0].setdefault(
                        "source", {"provider": "ride-service"}
                    ),
                }[target]
                destination[key] = "sensitive-value"
                self._write_manifest(manifest)
                with self.assertRaisesRegex(ValueError, "credential"):
                    import_sync_manifest(self.workspace, self.source)

    def test_rejects_provider_and_record_identifier_traversal(self) -> None:
        for provider_id in ("../escape", "../../outside", "/tmp/escape", "Upper", "a/b"):
            with self.subTest(provider_id=provider_id):
                manifest = _manifest()
                manifest["provider"]["id"] = provider_id
                self._write_manifest(manifest)
                with self.assertRaisesRegex(ValueError, "provider"):
                    import_sync_manifest(self.workspace, self.source)

        for record_id in ("other:ride-1", "../escape", "", True):
            with self.subTest(record_id=record_id):
                manifest = _manifest()
                manifest["activities"][0]["id"] = record_id
                self._write_manifest(manifest)
                with self.assertRaisesRegex(ValueError, "record id"):
                    import_sync_manifest(self.workspace, self.source)

    def test_rejects_reserved_first_party_provider_identifiers(self) -> None:
        for provider_id in ("strava", "recording", "recordings", "apple_health", "garmin"):
            with self.subTest(provider_id=provider_id):
                manifest = _manifest()
                manifest["provider"]["id"] = provider_id
                self._write_manifest(manifest)
                with self.assertRaisesRegex(ValueError, "reserved"):
                    import_sync_manifest(self.workspace, self.source)

    def test_rejects_provider_provenance_mismatch(self) -> None:
        manifest = _manifest()
        manifest["activities"][0]["source"] = {
            "provider": "different-provider",
            "record_id": "ride-1",
        }
        self._write_manifest(manifest)

        with self.assertRaisesRegex(ValueError, "provider provenance"):
            import_sync_manifest(self.workspace, self.source)

    def test_rejects_record_provenance_mismatch(self) -> None:
        manifest = _manifest()
        manifest["recovery"][0]["source"] = {
            "provider": "ride-service",
            "record_id": "different-record",
        }
        self._write_manifest(manifest)

        with self.assertRaisesRegex(ValueError, "record provenance"):
            import_sync_manifest(self.workspace, self.source)

    def test_rejects_invalid_record_types_dates_and_nonfinite_values(self) -> None:
        for mutation in (
            lambda item: item.update(activities={}),
            lambda item: item.update(recovery="nope"),
            lambda item: item["activities"][0].update(moving_time_s=True),
            lambda item: item["activities"][0].update(distance_m=float("nan")),
            lambda item: item["activities"][0].update(start_date_local="yesterday"),
            lambda item: item["recovery"][0].update(date="2026-99-99"),
            lambda item: item.update(synced_at="not-a-timestamp"),
        ):
            with self.subTest(mutation=mutation):
                manifest = _manifest()
                mutation(manifest)
                self._write_manifest(manifest)
                with self.assertRaises(ValueError):
                    import_sync_manifest(self.workspace, self.source)

    def test_rejects_symlinked_input_files(self) -> None:
        actual = self.root / "actual.json"
        actual.write_text(json.dumps(_manifest()), encoding="utf-8")
        self.source.symlink_to(actual)

        with self.assertRaises(ValueError):
            import_sync_manifest(self.workspace, self.source)

    def test_rejects_non_regular_and_oversized_input_files(self) -> None:
        self.source.mkdir()
        with self.assertRaisesRegex(ValueError, "regular"):
            import_sync_manifest(self.workspace, self.source)
        self.source.rmdir()

        self.source.write_text("{}", encoding="utf-8")
        with patch("gradient_ascent.external_sync.MAX_MANIFEST_BYTES", 1):
            with self.assertRaisesRegex(ValueError, "maximum"):
                import_sync_manifest(self.workspace, self.source)
        self.assertGreater(MAX_MANIFEST_BYTES, 1024)

    def test_rejects_symlinked_workspace_destinations(self) -> None:
        self._write_manifest()
        outside = self.root / "outside"
        outside.mkdir()
        (self.workspace / "integrations").symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(ValueError, "symbolic link"):
            import_sync_manifest(self.workspace, self.source)
        self.assertEqual(list(outside.iterdir()), [])

    def test_rejects_symlinked_provider_directory_and_manifest_destination(self) -> None:
        self._write_manifest()
        outside = self.root / "outside"
        outside.mkdir()
        integrations = self.workspace / "integrations"
        integrations.mkdir()
        (integrations / "ride-service").symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(ValueError, "symbolic link"):
            import_sync_manifest(self.workspace, self.source)
        (integrations / "ride-service").unlink()
        provider_dir = integrations / "ride-service"
        provider_dir.mkdir()
        victim = outside / "victim.json"
        victim.write_text("unchanged", encoding="utf-8")
        (provider_dir / "manifest.json").symlink_to(victim)

        with self.assertRaisesRegex(ValueError, "symbolic link"):
            import_sync_manifest(self.workspace, self.source)
        self.assertEqual(victim.read_text(), "unchanged")

    def test_rejects_symlinked_workspace_gitignore(self) -> None:
        self._write_manifest()
        victim = self.root / "victim"
        victim.write_text("unchanged", encoding="utf-8")
        (self.workspace / ".gitignore").symlink_to(victim)

        with self.assertRaisesRegex(ValueError, "symbolic link"):
            import_sync_manifest(self.workspace, self.source)
        self.assertEqual(victim.read_text(), "unchanged")

    def test_failed_atomic_write_preserves_previous_manifest(self) -> None:
        self._write_manifest()
        import_sync_manifest(self.workspace, self.source)
        destination = self.workspace / "integrations" / "ride-service" / "manifest.json"
        previous = destination.read_text(encoding="utf-8")
        changed = _manifest()
        changed["activities"][0]["name"] = "Updated ride"
        self._write_manifest(changed)

        with patch("gradient_ascent.storage.json.dump", side_effect=RuntimeError("interrupted")):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                import_sync_manifest(self.workspace, self.source)

        self.assertEqual(destination.read_text(encoding="utf-8"), previous)
        self.assertEqual(os.listdir(destination.parent), ["manifest.json"])

    def test_loader_handles_empty_workspace_and_rejects_tampered_manifests(self) -> None:
        self.assertEqual(load_external_sync_manifests(self.workspace), [])
        self._write_manifest()
        import_sync_manifest(self.workspace, self.source)
        destination = self.workspace / "integrations" / "ride-service" / "manifest.json"
        tampered = json.loads(destination.read_text())
        tampered["provider"]["id"] = "different-provider"
        destination.write_text(json.dumps(tampered), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "provider"):
            load_external_sync_manifests(self.workspace)

    def test_unsupported_platform_preserves_workspaces_without_active_integrations(self) -> None:
        unsupported = RuntimeError("This platform cannot safely open local sync directories.")
        with patch("gradient_ascent.external_sync._directory_flags", side_effect=unsupported):
            self.assertEqual(load_external_sync_manifests(self.workspace), [])
            (self.workspace / "integrations").mkdir()
            self.assertEqual(load_external_sync_manifests(self.workspace), [])

    def test_unsupported_platform_rejects_active_integrations_and_imports(self) -> None:
        unsupported = RuntimeError("This platform cannot safely open local sync directories.")
        (self.workspace / "integrations").mkdir()
        (self.workspace / "integrations" / "ride-service").mkdir()
        self._write_manifest()

        with patch("gradient_ascent.external_sync._directory_flags", side_effect=unsupported):
            with self.assertRaisesRegex(RuntimeError, "cannot safely"):
                load_external_sync_manifests(self.workspace)
            with patch("gradient_ascent.external_sync._read_manifest") as manifest_reader:
                with self.assertRaisesRegex(RuntimeError, "cannot safely"):
                    import_sync_manifest(self.workspace, self.source)
            manifest_reader.assert_not_called()

    def test_rejects_duplicate_json_fields(self) -> None:
        self.source.write_text(
            '{"version":1,"version":1,"provider":{"id":"ride-service","label":"Ride"},'
            '"activities":[],"recovery":[]}',
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "duplicate"):
            import_sync_manifest(self.workspace, self.source)

    def test_rejects_duplicate_provider_local_record_ids(self) -> None:
        for collection in ("activities", "recovery"):
            with self.subTest(collection=collection):
                manifest = _manifest()
                manifest[collection].append(dict(manifest[collection][0]))
                self._write_manifest(manifest)
                with self.assertRaisesRegex(ValueError, "duplicate record ids"):
                    import_sync_manifest(self.workspace, self.source)

    def test_direct_import_waits_for_an_existing_workspace_writer(self) -> None:
        self._write_manifest()
        manifest_read = threading.Event()
        write_started = threading.Event()
        from gradient_ascent import external_sync

        original_read = external_sync._read_manifest
        original_write = external_sync._atomic_write_manifest

        def observed_read(*args, **kwargs):
            result = original_read(*args, **kwargs)
            manifest_read.set()
            return result

        def observed_write(*args, **kwargs):
            write_started.set()
            return original_write(*args, **kwargs)

        with (
            patch("gradient_ascent.external_sync._read_manifest", side_effect=observed_read),
            patch(
                "gradient_ascent.external_sync._atomic_write_manifest", side_effect=observed_write
            ),
        ):
            with ThreadPoolExecutor(max_workers=1) as executor:
                with workspace_lock(self.workspace):
                    future = executor.submit(import_sync_manifest, self.workspace, self.source)
                    self.assertTrue(manifest_read.wait(timeout=5))
                    self.assertFalse(write_started.wait(timeout=0.2))
                    self.assertFalse(future.done())
                    self.assertFalse((self.workspace / "integrations").exists())
                result = future.result(timeout=5)

        self.assertEqual(result["activities"], 1)


if __name__ == "__main__":
    unittest.main()
