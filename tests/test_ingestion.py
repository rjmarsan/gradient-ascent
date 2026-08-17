import json
import tempfile
import unittest
from datetime import date, timedelta
from itertools import permutations
from pathlib import Path
from unittest import mock

from gradient_ascent.apple_health import import_apple_health_export
from gradient_ascent.canonical import (
    build_canonical_files,
    canonical_recovery_records,
    resolve_activity_records,
)
from gradient_ascent.garmin import import_garmin_export
from gradient_ascent.insights import _meaningful_ride_fields, build_insights


class IngestionTest(unittest.TestCase):
    def test_external_sync_manifest_preserves_provider_activity_and_recovery(self) -> None:
        from gradient_ascent.cli import _init_workspace
        from gradient_ascent.external_sync import import_sync_manifest

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "workspace"
            _init_workspace(data_dir, force=False)
            manifest_path = root / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "provider": {"id": "ride-service", "label": "Ride Service"},
                        "activities": [
                            {
                                "id": "morning-ride",
                                "name": "Morning Ride",
                                "date": "2026-08-14",
                                "start_date_local": "2026-08-14T08:00:00",
                                "sport_type": "Ride",
                                "moving_time_s": 3600,
                                "distance_m": 25000,
                                "average_watts": 205,
                            }
                        ],
                        "recovery": [
                            {
                                "id": "morning-recovery",
                                "date": "2026-08-14",
                                "resting_hr": 48,
                                "hrv_ms": 62,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            import_sync_manifest(data_dir, manifest_path)

            result = build_canonical_files(data_dir)
            activities = json.loads((data_dir / "canonical" / "activities.json").read_text())
            recovery = json.loads((data_dir / "canonical" / "recovery.json").read_text())

        self.assertEqual(result["activities"], 1)
        self.assertEqual(result["recovery"], 1)
        self.assertEqual(activities[0]["id"], "ride-service:morning-ride")
        self.assertEqual(activities[0]["source"]["provider"], "ride-service")
        self.assertEqual(activities[0]["distance_m"], 25000)
        self.assertEqual(recovery[0]["id"], "ride-service:morning-recovery")
        self.assertEqual(recovery[0]["source"]["provider"], "ride-service")
        self.assertEqual(recovery[0]["resting_hr"], 48)

    def test_common_cycling_activity_types_count_as_rides(self) -> None:
        for sport_type in (
            "Ride",
            "Virtual Ride",
            "Gravel Ride",
            "Mountain Bike Ride",
            "Cycling",
            "HKWorkoutActivityTypeCycling",
        ):
            with self.subTest(sport_type=sport_type):
                meaningful, reason = _meaningful_ride_fields(
                    sport_type,
                    3600,
                    500,
                    None,
                    None,
                )
                self.assertTrue(meaningful)
                self.assertIsNone(reason)

    def test_activity_history_builds_observation_week_without_a_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "workspace"
            output_dir = data_dir / "derived"
            (data_dir / "strava").mkdir(parents=True)
            (data_dir / "plan").mkdir()
            (data_dir / "plan" / "weeks.json").write_text("[]", encoding="utf-8")
            (data_dir / "strava" / "activities.json").write_text(
                json.dumps(
                    {
                        "123": {
                            "id": 123,
                            "name": "Morning Ride",
                            "sport_type": "Ride",
                            "start_date_local": "2026-06-15T08:10:00",
                            "moving_time": 3600,
                            "distance": 25000,
                        }
                    }
                ),
                encoding="utf-8",
            )

            build_insights(data_dir, None, output_dir)
            weekly = json.loads((output_dir / "weekly.json").read_text())

        self.assertEqual(len(weekly), 1)
        self.assertEqual(weekly[0]["start_date"], "2026-06-15")
        self.assertEqual(weekly[0]["plan"]["source"], "activity_history")
        self.assertEqual(weekly[0]["activity_ids"], [123])

    def test_apple_health_export_normalizes_workouts_and_recovery(self) -> None:
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData>
  <Record type="HKQuantityTypeIdentifierRestingHeartRate" startDate="2026-05-01 08:00:00 -0700" endDate="2026-05-01 08:00:00 -0700" value="49"/>
  <Record type="HKQuantityTypeIdentifierHeartRateVariabilitySDNN" startDate="2026-05-01 08:05:00 -0700" endDate="2026-05-01 08:05:00 -0700" value="61"/>
  <Record type="HKCategoryTypeIdentifierSleepAnalysis" startDate="2026-05-01 00:00:00 -0700" endDate="2026-05-01 07:30:00 -0700" value="HKCategoryValueSleepAnalysisAsleepCore"/>
  <Workout workoutActivityType="HKWorkoutActivityTypeCycling" uuid="abc" startDate="2026-05-01 09:00:00 -0700" endDate="2026-05-01 10:00:00 -0700" duration="60" durationUnit="min" totalDistance="25" totalDistanceUnit="km" totalEnergyBurned="500" totalEnergyBurnedUnit="kcal"/>
</HealthData>
"""
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "workspace"
            export_path = Path(tmp) / "export.xml"
            export_path.write_text(xml, encoding="utf-8")
            result = import_apple_health_export(data_dir, export_path)
            workouts = json.loads((data_dir / "apple_health" / "workouts.json").read_text())
            recovery = json.loads((data_dir / "apple_health" / "recovery.json").read_text())

        self.assertEqual(result["workouts"], 1)
        self.assertEqual(workouts[0]["duration_s"], 3600.0)
        self.assertEqual(workouts[0]["distance_m"], 25000.0)
        self.assertEqual(workouts[0]["energy_kj"], 2092.0)
        self.assertEqual(recovery[0]["resting_hr"], 49.0)
        self.assertEqual(recovery[0]["hrv_ms"], 61.0)
        self.assertEqual(recovery[0]["sleep_duration_s"], 27000.0)

    def test_garmin_connect_export_imports_without_login_or_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "workspace"
            connect = root / "export" / "DI_CONNECT"
            aggregator = connect / "DI-Connect-Aggregator"
            wellness = connect / "DI-Connect-Wellness"
            aggregator.mkdir(parents=True)
            wellness.mkdir()
            (aggregator / "UDSFile_202605.json").write_text(
                json.dumps(
                    [
                        {
                            "calendarDate": "2026-05-01",
                            "currentDayRestingHeartRate": 48,
                            "allDayStress": {
                                "aggregatorList": [
                                    {"type": "TOTAL", "averageStressLevel": 22}
                                ]
                            },
                        }
                    ]
                ),
                encoding="utf-8",
            )
            (wellness / "202605_sleepData.json").write_text(
                json.dumps(
                    [
                        {
                            "calendarDate": "2026-05-01",
                            "sleepStartTimestampGMT": "2026-05-01T06:00:00+00:00",
                            "sleepEndTimestampGMT": "2026-05-01T13:00:00+00:00",
                            "deepSleepSeconds": 3600,
                            "lightSleepSeconds": 14400,
                            "remSleepSeconds": 3600,
                        }
                    ]
                ),
                encoding="utf-8",
            )

            result = import_garmin_export(data_dir, root / "export")
            recovery = canonical_recovery_records(data_dir)

        self.assertEqual(result.days_written, 1)
        self.assertEqual(recovery[0]["source"]["provider"], "garmin")
        self.assertEqual(recovery[0]["resting_hr"], 48.0)
        self.assertEqual(recovery[0]["sleep_duration_s"], 21600.0)

    def test_daily_insights_choose_a_primary_recovery_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            output_dir = data_dir / "derived"
            (data_dir / "strava").mkdir()
            (data_dir / "garmin").mkdir()
            (data_dir / "strava" / "activities.json").write_text("{}", encoding="utf-8")
            (data_dir / "garmin" / "2026-05-01.json").write_text(
                json.dumps(
                    {
                        "heartrate": {"restingHeartRate": 48},
                        "stress": {"avgStressLevel": 22},
                        "sleep": {"dailySleepDTO": {"sleepTimeSeconds": 27000}},
                    }
                ),
                encoding="utf-8",
            )

            build_insights(data_dir, None, output_dir)
            daily = json.loads((output_dir / "daily.json").read_text())

        self.assertEqual(daily[0]["primary_recovery"]["source"]["provider"], "garmin")
        self.assertEqual(daily[0]["primary_recovery"]["resting_hr"], 48.0)
        self.assertNotIn("huge_raw_blob", json.dumps(daily[0]))

    def test_duplicate_activities_prefer_the_strava_record(self) -> None:
        records = [
            {
                "id": "strava:1",
                "date": "2026-05-01",
                "sport_type": "Ride",
                "start_date_local": "2026-05-01T08:00:00",
                "moving_time_s": 3600,
                "distance_m": 30000,
                "source": {"provider": "strava"},
            },
            {
                "id": "apple_health:abc",
                "date": "2026-05-01",
                "sport_type": "HKWorkoutActivityTypeCycling",
                "start_date_local": "2026-05-01T08:05:00",
                "moving_time_s": 3550,
                "distance_m": 29800,
                "source": {"provider": "apple_health"},
            },
        ]

        resolved, links = resolve_activity_records(records)

        self.assertEqual([item["id"] for item in resolved], ["strava:1"])
        self.assertEqual(links[0]["duplicate_count"], 1)

    def test_companion_duplicate_does_not_replace_an_official_strava_ride(self) -> None:
        records = [
            {
                "id": "ride-service:external-1",
                "date": "2026-05-01",
                "sport_type": "Ride",
                "start_date_local": "2026-05-01T08:01:00",
                "moving_time_s": 3610,
                "distance_m": 30100,
                "average_watts": 215,
                "source": {"provider": "ride-service"},
            },
            {
                "id": "strava:1",
                "date": "2026-05-01",
                "sport_type": "Ride",
                "start_date_local": "2026-05-01T08:00:00",
                "moving_time_s": 3600,
                "distance_m": 30000,
                "source": {"provider": "strava"},
            },
        ]

        resolved, links = resolve_activity_records(records)

        self.assertEqual([item["id"] for item in resolved], ["strava:1"])
        self.assertEqual(links[0]["duplicate_count"], 1)
        self.assertCountEqual(
            links[0]["candidate_ids"],
            ["strava:1", "ride-service:external-1"],
        )

    def test_nearby_activities_from_the_same_provider_remain_distinct(self) -> None:
        records = [
            {
                "id": "strava:1",
                "date": "2026-05-01",
                "sport_type": "Ride",
                "start_date_local": "2026-05-01T08:00:00",
                "moving_time_s": 3600,
                "distance_m": 30000,
                "source": {"provider": "strava"},
            },
            {
                "id": "strava:2",
                "date": "2026-05-01",
                "sport_type": "Ride",
                "start_date_local": "2026-05-01T08:05:00",
                "moving_time_s": 3550,
                "distance_m": 29800,
                "source": {"provider": "strava"},
            },
        ]

        resolved, links = resolve_activity_records(records)

        self.assertEqual([item["id"] for item in resolved], ["strava:1", "strava:2"])
        self.assertEqual([item["duplicate_count"] for item in links], [0, 0])

    def test_cross_provider_recording_cannot_bridge_distinct_same_provider_rides(self) -> None:
        first = {
            "id": "strava:first",
            "date": "2026-05-01",
            "sport_type": "Ride",
            "start_date_local": "2026-05-01T08:00:00",
            "moving_time_s": 3600,
            "distance_m": 30000,
            "source": {"provider": "strava"},
        }
        second = {
            **first,
            "id": "strava:second",
            "start_date_local": "2026-05-01T08:05:00",
            "moving_time_s": 3550,
            "distance_m": 29800,
        }
        recording = {
            **first,
            "id": "recording:middle",
            "start_date_local": "2026-05-01T08:02:30",
            "moving_time_s": 3575,
            "distance_m": 29900,
            "source": {"provider": "recording"},
        }

        for ordered in permutations((first, recording, second)):
            with self.subTest(order=tuple(record["id"] for record in ordered)):
                resolved, links = resolve_activity_records(list(ordered))

                self.assertEqual(
                    [record["id"] for record in resolved],
                    ["strava:first", "strava:second"],
                )
                self.assertEqual(sum(link["duplicate_count"] for link in links), 1)
                self.assertEqual(
                    sum("recording:middle" in link["candidate_ids"] for link in links),
                    1,
                )
                self.assertTrue(
                    all(
                        len(
                            {
                                identifier
                                for identifier in link["candidate_ids"]
                                if identifier.startswith("strava:")
                            }
                        )
                        == 1
                        for link in links
                    )
                )

    def test_cross_provider_recording_preserves_strict_same_provider_duplicates(self) -> None:
        first = {
            "id": "strava:first",
            "date": "2026-05-01",
            "sport_type": "Ride",
            "start_date_local": "2026-05-01T08:00:00",
            "moving_time_s": 3600,
            "distance_m": 30000,
            "source": {"provider": "strava"},
        }
        duplicate = {
            **first,
            "id": "strava:duplicate",
            "start_date_local": "2026-05-01T08:00:03",
            "moving_time_s": 3610,
            "distance_m": 30100,
        }
        recording = {
            **first,
            "id": "recording:same",
            "source": {"provider": "recording"},
        }

        for ordered in permutations((first, duplicate, recording)):
            with self.subTest(order=tuple(record["id"] for record in ordered)):
                resolved, links = resolve_activity_records(list(ordered))

                self.assertEqual(len(resolved), 1)
                self.assertTrue(resolved[0]["id"].startswith("strava:"))
                self.assertEqual(links[0]["duplicate_count"], 2)
                self.assertCountEqual(
                    links[0]["candidate_ids"],
                    ["strava:first", "strava:duplicate", "recording:same"],
                )

    def test_same_provider_nearly_identical_recordings_are_deduplicated(self) -> None:
        records = [
            {
                "id": "strava:101",
                "date": "2026-08-13",
                "sport_type": "Ride",
                "start_date_local": "2026-08-13T09:36:38Z",
                "moving_time_s": 5428,
                "distance_m": 45407.2,
                "average_watts": 210,
                "source": {"provider": "strava"},
            },
            {
                "id": "strava:102",
                "date": "2026-08-13",
                "sport_type": "Ride",
                "start_date_local": "2026-08-13T09:36:41Z",
                "moving_time_s": 5440,
                "distance_m": 45264.1,
                "average_watts": 210,
                "source": {"provider": "strava"},
            },
        ]

        resolved, links = resolve_activity_records(records)

        self.assertEqual([item["id"] for item in resolved], ["strava:101"])
        self.assertEqual(links[0]["primary_id"], "strava:101")
        self.assertEqual(links[0]["candidate_ids"], ["strava:101", "strava:102"])
        self.assertEqual(links[0]["duplicate_count"], 1)

    def test_same_provider_duplicate_requires_all_positive_finite_fingerprints(self) -> None:
        base = {
            "id": "strava:101",
            "date": "2026-08-13",
            "sport_type": "Ride",
            "start_date_local": "2026-08-13T09:36:38Z",
            "moving_time_s": 5428,
            "distance_m": 45407.2,
            "source": {"provider": "strava"},
        }
        changes = (
            {"start_date_local": None},
            {"start_date_local": "invalid"},
            {"start_date_local": "2026-08-13T09:37:09Z"},
            {"moving_time_s": None},
            {"moving_time_s": 0},
            {"moving_time_s": -1},
            {"moving_time_s": True},
            {"moving_time_s": float("nan")},
            {"moving_time_s": float("inf")},
            {"moving_time_s": 5500},
            {"distance_m": None},
            {"distance_m": 0},
            {"distance_m": -1},
            {"distance_m": True},
            {"distance_m": float("nan")},
            {"distance_m": float("inf")},
            {"distance_m": 46000},
            {"sport_type": "VirtualRide"},
        )

        for change in changes:
            with self.subTest(change=change):
                candidate = {
                    **base,
                    "id": "strava:102",
                    "start_date_local": "2026-08-13T09:36:41Z",
                    **change,
                }
                resolved, links = resolve_activity_records([base, candidate])

                self.assertEqual(len(resolved), 2)
                self.assertEqual([item["duplicate_count"] for item in links], [0, 0])

    def test_activity_deduplication_compares_only_same_date_candidates(self) -> None:
        from gradient_ascent.canonical import _is_duplicate_activity

        records = [
            {
                "id": f"strava:{index}",
                "date": (date(2020, 1, 1) + timedelta(days=index // 2)).isoformat(),
                "sport_type": "Ride",
                "source": {"provider": "strava"},
            }
            for index in range(200)
        ]
        with mock.patch(
            "gradient_ascent.canonical._is_duplicate_activity",
            wraps=_is_duplicate_activity,
        ) as is_duplicate:
            resolved, links = resolve_activity_records(records)

        self.assertEqual(len(resolved), 200)
        self.assertEqual(len(links), 200)
        self.assertLessEqual(is_duplicate.call_count, 100)

    def test_date_buckets_preserve_cross_provider_order_and_missing_dates(self) -> None:
        def activity(identifier: str, activity_date: str | None) -> dict:
            provider = identifier.split(":", 1)[0]
            return {
                "id": identifier,
                "date": activity_date,
                "sport_type": "Ride",
                "start_date_local": (
                    f"{activity_date}T{'10' if identifier.endswith('separate') else '08'}:00:00"
                    if activity_date is not None
                    else None
                ),
                "moving_time_s": 3600,
                "distance_m": 25000,
                "source": {"provider": provider},
            }

        records = [
            activity("apple_health:later", "2026-05-02"),
            activity("strava:earlier", "2026-05-01"),
            activity("strava:later", "2026-05-02"),
            activity("ride-service:earlier", "2026-05-01"),
            activity("strava:separate", "2026-05-01"),
            activity("apple_health:missing", None),
            activity("strava:missing", None),
        ]

        resolved, links = resolve_activity_records(records)

        self.assertEqual(
            links,
            [
                {
                    "primary_id": "strava:later",
                    "candidate_ids": ["apple_health:later", "strava:later"],
                    "duplicate_count": 1,
                },
                {
                    "primary_id": "strava:earlier",
                    "candidate_ids": ["strava:earlier", "ride-service:earlier"],
                    "duplicate_count": 1,
                },
                {
                    "primary_id": "strava:separate",
                    "candidate_ids": ["strava:separate"],
                    "duplicate_count": 0,
                },
                {
                    "primary_id": "strava:missing",
                    "candidate_ids": ["apple_health:missing", "strava:missing"],
                    "duplicate_count": 1,
                },
            ],
        )
        self.assertCountEqual(
            [record["id"] for record in resolved],
            ["strava:earlier", "strava:later", "strava:missing", "strava:separate"],
        )

    def test_unhashable_activity_dates_keep_original_deduplication_behavior(self) -> None:
        records = [
            {
                "id": "apple_health:list-date",
                "date": ["2026-05-01"],
                "sport_type": "Ride",
                "source": {"provider": "apple_health"},
            },
            {
                "id": "strava:list-date",
                "date": ["2026-05-01"],
                "sport_type": "Ride",
                "source": {"provider": "strava"},
            },
            {
                "id": "strava:other-list-date",
                "date": ["2026-05-02"],
                "sport_type": "Ride",
                "source": {"provider": "strava"},
            },
        ]

        resolved, links = resolve_activity_records(records)

        self.assertEqual(
            [record["id"] for record in resolved],
            ["strava:list-date", "strava:other-list-date"],
        )
        self.assertEqual(
            links[0]["candidate_ids"],
            ["apple_health:list-date", "strava:list-date"],
        )

    def test_canonical_build_writes_all_supported_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            (data_dir / "strava").mkdir()
            (data_dir / "strava" / "activities.json").write_text("{}", encoding="utf-8")
            result = build_canonical_files(data_dir)

            self.assertEqual(result["activities"], 0)
            for filename in (
                "activities.json",
                "resolved_activities.json",
                "activity_links.json",
                "recovery.json",
                "planned_workouts.json",
            ):
                self.assertTrue((data_dir / "canonical" / filename).is_file())


if __name__ == "__main__":
    unittest.main()
