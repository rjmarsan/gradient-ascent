import json
import tempfile
import unittest
from pathlib import Path

from gradient_ascent.connections import (
    check_provider,
    connections_payload,
    connections_summary_payload,
    update_provider,
)


class ConnectionsTest(unittest.TestCase):
    def test_payload_exposes_only_supported_local_imports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = connections_payload(Path(tmp))

        self.assertEqual(
            [provider["key"] for provider in payload["providers"]],
            ["strava", "apple_health", "garmin"],
        )
        self.assertTrue(payload["providers"][0]["archive_upload_available"])
        self.assertEqual(payload["providers"][0]["input_mode"], "archive")
        self.assertNotIn("auth_mode", json.dumps(payload))
        self.assertEqual(payload["providers"][1]["fields"], [{"key": "export_path", "label": "Local export path"}])

    def test_compact_summary_stays_small_and_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = connections_summary_payload(Path(tmp))

        self.assertEqual(
            [item["key"] for item in payload["available"]],
            ["strava", "apple_health", "garmin"],
        )
        self.assertLess(len(json.dumps(payload, separators=(",", ":"))), 1800)
        self.assertNotIn("fields", json.dumps(payload))

    def test_existing_local_strava_history_is_imported_without_an_archive_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            strava_dir = data_dir / "strava"
            strava_dir.mkdir()
            (strava_dir / "activities.json").write_text(
                json.dumps({"ride-1": {"id": "ride-1", "name": "Local ride"}}),
                encoding="utf-8",
            )
            (strava_dir / "state.json").write_text(
                json.dumps({"last_sync": "2026-08-14T08:00:00Z", "activity_count": 1}),
                encoding="utf-8",
            )

            provider = connections_payload(data_dir)["providers"][0]
            compact = connections_summary_payload(data_dir)["available"][0]

        self.assertEqual(provider["status"], "imported")
        self.assertEqual(provider["issues"], [])
        self.assertFalse(provider["archive_imported"])
        self.assertIsNone(provider["last_import_at"])
        self.assertIn("Local ride history is available", provider["next_steps"][0])
        self.assertEqual(compact["status"], "imported")
        self.assertFalse(compact["archive_imported"])

    def test_empty_legacy_strava_history_still_requires_setup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            strava_dir = data_dir / "strava"
            strava_dir.mkdir()
            (strava_dir / "activities.json").write_text("{}\n", encoding="utf-8")
            (strava_dir / "state.json").write_text(
                json.dumps({"last_sync": "2026-08-14T08:00:00Z", "activity_count": 1}),
                encoding="utf-8",
            )

            provider = connections_payload(data_dir)["providers"][0]

        self.assertEqual(provider["status"], "needs_setup")
        self.assertFalse(provider["archive_imported"])

    def test_official_strava_archive_preserves_import_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            strava_dir = data_dir / "strava"
            strava_dir.mkdir()
            (strava_dir / "state.json").write_text(
                json.dumps({"archive_import": {"imported_at": "2026-08-14T08:00:00Z"}}),
                encoding="utf-8",
            )

            provider = connections_payload(data_dir)["providers"][0]

        self.assertEqual(provider["status"], "imported")
        self.assertTrue(provider["archive_imported"])
        self.assertEqual(provider["last_import_at"], "2026-08-14T08:00:00Z")
        self.assertEqual(
            provider["next_steps"],
            ["Upload a newer official archive when you want to refresh ride history."],
        )

    def test_imported_companion_appears_as_a_read_only_local_source(self) -> None:
        from gradient_ascent.external_sync import import_sync_manifest

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            manifest_path = data_dir / "provider.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "provider": {"id": "ride-service", "label": "Ride Service"},
                        "synced_at": "2026-08-14T08:30:00Z",
                        "activities": [
                            {
                                "id": "morning-ride",
                                "date": "2026-08-14",
                                "start_date_local": "2026-08-14T08:00:00",
                                "sport_type": "Ride",
                                "moving_time_s": 1800,
                                "distance_m": 12000,
                            }
                        ],
                        "recovery": [],
                    }
                ),
                encoding="utf-8",
            )
            import_sync_manifest(data_dir, manifest_path)

            payload = connections_payload(data_dir)
            compact = connections_summary_payload(data_dir)

        companion = payload["providers"][-1]
        self.assertEqual(companion["key"], "external:ride-service")
        self.assertEqual(companion["label"], "Ride Service")
        self.assertEqual(companion["input_mode"], "manifest")
        self.assertEqual(companion["fields"], [])
        self.assertFalse(companion["test_available"])
        self.assertEqual(companion["status"], "imported")
        self.assertEqual(companion["last_import_at"], "2026-08-14T08:30:00Z")
        self.assertEqual(compact["available"][-1]["key"], "external:ride-service")

    def test_local_path_configuration_writes_only_the_connection_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            export_path = data_dir / "apple-export"
            summary = update_provider(
                data_dir,
                "apple_health",
                fields={"export_path": str(export_path)},
            )
            config = json.loads((data_dir / "connections" / "config.json").read_text())

            self.assertTrue(summary["configured_fields"]["export_path"])
            self.assertEqual(
                [path.name for path in (data_dir / "connections").iterdir()],
                ["config.json"],
            )
            self.assertEqual(config["providers"]["apple_health"]["fields"], {"export_path": str(export_path)})

    def test_source_checks_validate_local_export_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            update_provider(
                data_dir,
                "apple_health",
                fields={"export_path": str(data_dir / "missing-apple-export")},
            )
            update_provider(
                data_dir,
                "garmin",
                fields={"export_path": str(data_dir / "missing-garmin-export")},
            )

            apple = check_provider(data_dir, "apple_health")
            garmin = check_provider(data_dir, "garmin")

        self.assertFalse(apple["ok"])
        self.assertEqual(apple["status"], "needs_attention")
        self.assertFalse(garmin["ok"])
        self.assertEqual(garmin["status"], "needs_attention")

    def test_unknown_provider_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(KeyError, "Unknown provider"):
                update_provider(Path(tmp), "unknown", fields={})


if __name__ == "__main__":
    unittest.main()
