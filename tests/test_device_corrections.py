from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gradient_ascent.canonical import canonical_activity_records
from gradient_ascent.device_corrections import apply_temperature_correction, load_temperature_corrections


class DeviceCorrectionsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rule = {
            "id": "example-head-unit-warm-bias",
            "device_name": "Example Head Unit",
            "start_date": "2025-01-15",
            "min_raw_temp_c": 22,
            "offset_c": -5.5,
            "source_device_serial": "EXAMPLE-123",
        }

    def test_applies_matching_temperature_offset_and_preserves_raw_value(self) -> None:
        activity = {
            "id": 123,
            "device_name": "Example Head Unit",
            "start_date_local": "2026-06-12T07:39:43Z",
            "average_temp": 29,
        }

        corrected = apply_temperature_correction(activity, [self.rule])

        self.assertEqual(corrected["average_temp_raw"], 29.0)
        self.assertEqual(corrected["average_temp"], 23.5)
        self.assertEqual(
            corrected["temperature_correction"],
            {
                "id": "example-head-unit-warm-bias",
                "min_raw_temp_c": 22.0,
                "offset_c": -5.5,
                "source_device_serial": "EXAMPLE-123",
            },
        )
        self.assertNotIn("average_temp_raw", activity)
        self.assertEqual(activity["average_temp"], 29)

    def test_ignores_older_and_nonmatching_device_rides(self) -> None:
        old_device_ride = {
            "device_name": "Example Head Unit",
            "start_date_local": "2025-01-14T07:00:00Z",
            "average_temp": 20,
        }
        garmin = {
            "device_name": "Garmin Edge 510",
            "start_date_local": "2026-06-12T07:00:00Z",
            "average_temp": 20,
        }

        self.assertEqual(
            apply_temperature_correction(old_device_ride, [self.rule]),
            old_device_ride,
        )
        self.assertEqual(apply_temperature_correction(garmin, [self.rule]), garmin)

    def test_ignores_matching_rides_below_the_calibrated_raw_temperature(self) -> None:
        cool_device_ride = {
            "device_name": "Example Head Unit",
            "start_date_local": "2026-01-22T07:00:00Z",
            "average_temp": 12,
        }

        self.assertEqual(
            apply_temperature_correction(cool_device_ride, [self.rule]),
            cool_device_ride,
        )

    def test_workspace_configuration_feeds_canonical_activity_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            (data_dir / "plan").mkdir()
            (data_dir / "strava").mkdir()
            (data_dir / "plan" / "device_corrections.json").write_text(
                json.dumps({"version": 1, "temperature": [self.rule]}),
                encoding="utf-8",
            )
            (data_dir / "strava" / "activities.json").write_text(
                json.dumps(
                    {
                        "123": {
                            "id": 123,
                            "name": "Ride",
                            "sport_type": "Ride",
                            "device_name": "Example Head Unit",
                            "start_date": "2026-06-12T14:39:43Z",
                            "start_date_local": "2026-06-12T07:39:43Z",
                            "moving_time": 3600,
                            "elapsed_time": 3600,
                            "distance": 30000,
                            "average_temp": 29,
                        }
                    }
                ),
                encoding="utf-8",
            )

            rules = load_temperature_corrections(data_dir)
            canonical = canonical_activity_records(data_dir)

        self.assertEqual(rules, [self.rule])
        self.assertEqual(canonical[0]["average_temp_c"], 23.5)
        self.assertEqual(canonical[0]["average_temp_c_raw"], 29.0)
        self.assertEqual(canonical[0]["raw"]["average_temp"], 29)


if __name__ == "__main__":
    unittest.main()
