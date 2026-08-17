from __future__ import annotations

import hashlib
import json
import stat
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock


def trip(identifier=101, *, departure="2026-08-15T09:00:00-07:00", updated="1", watts=200):
    start = int(datetime.fromisoformat(departure).timestamp())
    return {
        "id": identifier,
        "name": "Synthetic private ride",
        "departed_at": departure,
        "time_zone": "America/Los_Angeles",
        "updated_at": f"2026-08-16T12:00:0{updated}Z",
        "fit_sport": 2,
        "activity_type": "cycling",
        "distance": 1000,
        "duration": 120,
        "device": "Synthetic device",
        "track_points": [
            {
                "t": start,
                "x": -122.0,
                "y": 37.0,
                "d": 0,
                "e": 10,
                "s": 36,
                "h": 130,
                "c": 80,
                "p": watts,
            },
            {
                "t": start + 60,
                "x": -122.001,
                "y": 37.001,
                "d": 500,
                "e": 15,
                "s": 36,
                "h": 140,
                "c": 85,
                "p": watts + 10,
                "lap": True,
            },
            {
                "t": start + 120,
                "x": -122.002,
                "y": 37.002,
                "d": 1000,
                "e": 20,
                "s": 36,
                "h": 150,
                "c": 90,
                "p": watts + 20,
            },
        ],
    }


def listing(records, next_page=None):
    return {
        "trips": [
            {key: value for key, value in record.items() if key != "track_points"}
            for record in records
        ],
        "meta": {
            "pagination": {
                "next_page_url": None
                if next_page is None
                else f"https://ridewithgps.com/api/v1/trips.json?page={next_page}&page_size=20"
            }
        },
    }


class RideWithGPSTest(unittest.TestCase):
    def setUp(self):
        from gradient_ascent.cli import _init_data_dir

        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir(mode=0o700)
        _init_data_dir(self.workspace)
        self.today = date(2026, 8, 16)

    def sync(self, client, **options):
        from gradient_ascent.ridewithgps import sync_ridewithgps
        from gradient_ascent.workspace_lock import workspace_lock

        with workspace_lock(self.workspace):
            return sync_ridewithgps(
                self.workspace, client, today=self.today, page_size=20, **options
            )

    def index(self):
        return json.loads((self.workspace / "recordings" / "activities.json").read_text())

    def test_paginated_recent_sync_preserves_full_ride_and_does_not_refresh(self):
        first, second = trip(101), trip(102, departure="2026-08-16T09:00:00-07:00")
        client = mock.Mock(
            side_effect=[listing([first], 2), {"trip": first}, listing([second]), {"trip": second}]
        )
        with mock.patch("gradient_ascent.refresh.refresh_workspace") as refresh:
            result = self.sync(client)
        refresh.assert_not_called()
        self.assertEqual(
            client.call_args_list,
            [
                mock.call("/api/v1/trips.json", {"page": 1, "page_size": 20}),
                mock.call("/api/v1/trips/101.json", {}),
                mock.call("/api/v1/trips.json", {"page": 2, "page_size": 20}),
                mock.call("/api/v1/trips/102.json", {}),
            ],
        )
        self.assertEqual(result["imported"], 2)
        self.assertEqual(result["pages"], 2)
        self.assertTrue(result["complete"])
        self.assertNotIn("Synthetic private", json.dumps(result))
        self.assertNotIn(str(self.workspace), json.dumps(result))
        record = next(item for item in self.index().values() if item["source_activity_id"] == "101")
        self.assertEqual(record["name"], first["name"])
        self.assertEqual(record["source_provider"], "ridewithgps")
        self.assertEqual(record["start_date_local"], "2026-08-15T09:00:00-07:00")
        streams = json.loads(
            (self.workspace / "recordings" / "streams" / f"{record['id']}.json").read_text()
        )
        values = {item["type"]: item["data"] for item in streams["streams"]}
        self.assertEqual(values["watts"], [200, 210, 220])
        self.assertEqual(values["latlng"][0], [37.0, -122.0])
        self.assertEqual(values["velocity_smooth"], [10.0] * 3)
        laps = json.loads(
            (self.workspace / "recordings" / "laps" / f"{record['id']}.json").read_text()
        )["laps"]
        self.assertEqual(sum(item["elapsed_time"] for item in laps), 120)
        self.assertEqual(sum(item["distance"] for item in laps), 1000)
        for path in (self.workspace / "integrations" / "ridewithgps").rglob("*"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700 if path.is_dir() else 0o600)

    def test_documented_ebike_taxonomy_imports_without_admitting_other_sports(self):
        specifications = [
            (101, 21, 47, "e_biking:mountain", "EMountainBikeRide"),
            (102, 21, 7, "e_biking:road", "EBikeRide"),
            (103, 21, 0, "e_biking:generic", "EBikeRide"),
            (104, 2, 47, "cycling:mountain", "EMountainBikeRide"),
            (105, None, None, "cycling:gravel", "Ride"),
            (106, 21, 8, "unknown:generic", "EMountainBikeRide"),
            (107, None, None, "e_biking:generic", "EBikeRide"),
        ]
        included = [
            {**trip(identifier), "fit_sport": sport, "fit_sub_sport": sub, "activity_type": kind}
            for identifier, sport, sub, kind, _ in specifications
        ]
        excluded = [
            {**trip(201), "fit_sport": 1, "activity_type": "running:trail"},
            {**trip(202), "fit_sport": 11, "activity_type": "walking:hiking"},
            {**trip(203), "fit_sport": 22, "activity_type": "motorcycling:generic"},
            {**trip(204), "fit_sport": 21, "activity_type": "running:generic"},
            {**trip(205), "fit_sport": True, "activity_type": "unknown:generic"},
        ]
        details = {f"/api/v1/trips/{row['id']}.json": row for row in included}
        client = mock.Mock(
            side_effect=lambda path, params: listing(included + excluded)
            if path == "/api/v1/trips.json"
            else {"trip": details[path]}
        )
        result = self.sync(client)
        self.assertEqual((result["imported"], result["skipped"]), (len(included), len(excluded)))
        records = {row["source_activity_id"]: row for row in self.index().values()}
        for identifier, sport, sub, kind, expected in specifications:
            with self.subTest(identifier=identifier):
                row = records[str(identifier)]
                self.assertEqual(row["sport_type"], expected)
                self.assertEqual(row["type"], "EBikeRide" if expected != "Ride" else "Ride")
                self.assertEqual(row["source_activity_type"], kind)
                if sport is not None:
                    self.assertEqual(row["source_fit_sport"], sport)
                if sub is not None:
                    self.assertEqual(row["source_fit_sub_sport"], sub)
        self.assertEqual(client.call_count, 1 + len(included))

    def test_cached_ebike_taxonomy_correction_keeps_original_recording_id(self):
        from gradient_ascent.recordings import import_activity_recording

        original = trip()
        self.sync(mock.Mock(side_effect=[listing([original]), {"trip": original}]))
        old_id = next(iter(self.index()))
        corrected = {
            **original,
            "fit_sport": 21,
            "fit_sub_sport": 47,
            "activity_type": "e_biking:mountain",
        }
        client = mock.Mock(return_value=listing([corrected]))
        self.assertEqual(self.sync(client)["existing"], 1)
        client.assert_called_once()
        self.assertEqual(set(self.index()), {old_id})
        self.assertEqual(self.index()[old_id]["sport_type"], "EMountainBikeRide")
        metadata = json.loads(
            (self.workspace / "integrations" / "ridewithgps" / "files" / "101.json").read_text()
        )
        self.assertEqual(metadata["source_fit_sub_sport"], 47)
        raw = (
            self.workspace
            / "integrations"
            / "ridewithgps"
            / "files"
            / f"101-{old_id.removeprefix('recording-')}.tcx"
        )
        import_activity_recording(self.workspace, raw)
        self.assertEqual(self.index()[old_id]["sport_type"], "EMountainBikeRide")
        self.assertEqual(self.sync(mock.Mock(return_value=listing([corrected])))["existing"], 1)
        self.assertEqual(set(self.index()), {old_id})
        self.assertEqual(self.index()[old_id]["source_fit_sub_sport"], 47)

    def test_cached_sport_correction_removes_stale_electric_taxonomy(self):
        from gradient_ascent.recordings import import_activity_recording

        original = {
            **trip(),
            "fit_sport": 21,
            "fit_sub_sport": 47,
            "activity_type": "e_biking:mountain",
        }
        self.sync(mock.Mock(side_effect=[listing([original]), {"trip": original}]))
        record_id = next(iter(self.index()))
        corrected = {
            **original,
            "fit_sport": None,
            "fit_sub_sport": None,
            "activity_type": "cycling:generic",
        }
        self.sync(mock.Mock(return_value=listing([corrected])))
        row = self.index()[record_id]
        self.assertEqual(row["sport_type"], "Ride")
        self.assertNotIn("source_fit_sport", row)
        self.assertNotIn("source_fit_sub_sport", row)
        files = self.workspace / "integrations" / "ridewithgps" / "files"
        metadata = json.loads((files / "101.json").read_text())
        self.assertNotIn("source_fit_sport", metadata)
        self.assertNotIn("source_fit_sub_sport", metadata)
        import_activity_recording(
            self.workspace, files / f"101-{record_id.removeprefix('recording-')}.tcx"
        )
        self.assertEqual(self.index()[record_id]["sport_type"], "Ride")

    def test_repeat_skips_details_and_edited_trip_replaces_only_its_old_index(self):
        original = trip()
        self.sync(mock.Mock(side_effect=[listing([original]), {"trip": original}]))
        old_id = next(iter(self.index()))
        repeat = mock.Mock(return_value=listing([original]))
        self.assertEqual(self.sync(repeat)["existing"], 1)
        repeat.assert_called_once()
        edited = trip(updated="2", watts=250)
        self.assertEqual(
            self.sync(mock.Mock(side_effect=[listing([edited]), {"trip": edited}]))["updated"], 1
        )
        self.assertEqual(len(self.index()), 1)
        self.assertNotIn(old_id, self.index())
        self.assertTrue((self.workspace / "recordings" / "streams" / f"{old_id}.json").is_file())
        self.assertTrue((self.workspace / "recordings" / "laps" / f"{old_id}.json").is_file())

    def test_cached_sync_preserves_custom_title_but_accepts_provider_renames(self):
        original = trip()
        self.sync(mock.Mock(side_effect=[listing([original]), {"trip": original}]))
        record_id = next(iter(self.index()))
        renamed = {**original, "name": "Upstream rename"}
        self.sync(mock.Mock(return_value=listing([renamed])))
        self.assertEqual(self.index()[record_id]["name"], renamed["name"])
        records = self.index()
        records[record_id]["name"] = "My authored title"
        (self.workspace / "recordings" / "activities.json").write_text(json.dumps(records))
        renamed_again = {**renamed, "name": "Another upstream rename"}
        self.sync(mock.Mock(return_value=listing([renamed_again])))
        self.assertEqual(self.index()[record_id]["name"], "My authored title")
        metadata = json.loads(
            (self.workspace / "integrations" / "ridewithgps" / "files" / "101.json").read_text()
        )
        self.assertEqual(metadata["last_provider_name"], renamed_again["name"])

    def test_edited_recording_preserves_custom_title_and_tracks_upstream_name(self):
        original, other = trip(101), trip(102)
        self.sync(
            mock.Mock(side_effect=[listing([original, other]), {"trip": original}, {"trip": other}])
        )
        records = self.index()
        original_id = next(
            key for key, row in records.items() if row["source_activity_id"] == "101"
        )
        other_id = next(key for key, row in records.items() if row["source_activity_id"] == "102")
        records[original_id]["name"] = "My authored title"
        (self.workspace / "recordings" / "activities.json").write_text(json.dumps(records))
        edited = {**trip(101, updated="2", watts=250), "name": "Provider edited title"}
        other_edited = {**trip(102, updated="2", watts=260), "name": "Other provider title"}
        self.sync(
            mock.Mock(
                side_effect=[
                    listing([edited, other_edited]),
                    {"trip": edited},
                    {"trip": other_edited},
                ]
            )
        )
        records = self.index()
        self.assertNotIn(original_id, records)
        self.assertNotIn(other_id, records)
        names = {row["source_activity_id"]: row["name"] for row in records.values()}
        self.assertEqual(names, {"101": "My authored title", "102": "Other provider title"})

    def test_adopted_metadata_without_provider_name_preserves_existing_title(self):
        original = trip()
        self.sync(mock.Mock(side_effect=[listing([original]), {"trip": original}]))
        metadata_path = self.workspace / "integrations" / "ridewithgps" / "files" / "101.json"
        metadata = json.loads(metadata_path.read_text())
        metadata.pop("last_provider_name", None)
        metadata_path.write_text(json.dumps(metadata))
        records = self.index()
        record_id = next(iter(records))
        records[record_id].pop("source_provider_name", None)
        records[record_id]["name"] = "Pre-migration authored title"
        (self.workspace / "recordings" / "activities.json").write_text(json.dumps(records))
        self.sync(mock.Mock(return_value=listing([original])))
        self.assertEqual(self.index()[record_id]["name"], "Pre-migration authored title")
        edited = {**trip(updated="2", watts=250), "name": "Provider edited title"}
        self.sync(mock.Mock(side_effect=[listing([edited]), {"trip": edited}]))
        self.assertEqual(next(iter(self.index().values()))["name"], "Pre-migration authored title")

    def test_failed_later_trip_does_not_advance_cached_title_history(self):
        original = trip()
        self.sync(mock.Mock(side_effect=[listing([original]), {"trip": original}]))
        renamed = {**original, "name": "Upstream rename"}
        invalid = {**trip(102), "time_zone": "Invalid/Zone"}
        with self.assertRaisesRegex(ValueError, "time zone"):
            self.sync(mock.Mock(return_value=listing([renamed, invalid])))
        self.assertEqual(next(iter(self.index().values()))["name"], original["name"])
        self.sync(mock.Mock(return_value=listing([renamed])))
        self.assertEqual(next(iter(self.index().values()))["name"], renamed["name"])

    def test_failed_later_trip_preserves_edited_recording_authored_title_on_retry(self):
        original = trip()
        self.sync(mock.Mock(side_effect=[listing([original]), {"trip": original}]))
        records = self.index()
        old_id = next(iter(records))
        records[old_id]["name"] = "My authored title"
        (self.workspace / "recordings" / "activities.json").write_text(json.dumps(records))
        edited = {**trip(updated="2", watts=250), "name": "Provider edited title"}
        invalid = {**trip(102), "time_zone": "Invalid/Zone"}
        with self.assertRaisesRegex(ValueError, "time zone"):
            self.sync(mock.Mock(side_effect=[listing([edited, invalid]), {"trip": edited}]))
        self.assertEqual(self.index()[old_id]["name"], "My authored title")
        self.sync(mock.Mock(side_effect=[listing([edited]), {"trip": edited}]))
        self.assertEqual(len(self.index()), 1)
        self.assertEqual(next(iter(self.index().values()))["name"], "My authored title")

    def test_title_replay_after_index_commit_and_metadata_write_failure(self):
        from gradient_ascent import ridewithgps

        original = trip()
        self.sync(mock.Mock(side_effect=[listing([original]), {"trip": original}]))
        renamed = {**original, "name": "Upstream rename"}
        write_json = ridewithgps._write_json

        def fail_metadata(directory, name, value, limit):
            if name == "101.json":
                raise OSError("synthetic metadata failure")
            return write_json(directory, name, value, limit)

        with mock.patch.object(ridewithgps, "_write_json", side_effect=fail_metadata):
            with self.assertRaisesRegex(OSError, "synthetic"):
                self.sync(mock.Mock(return_value=listing([renamed])))
        self.assertEqual(next(iter(self.index().values()))["name"], renamed["name"])
        renamed_again = {**renamed, "name": "Second upstream rename"}
        self.sync(mock.Mock(return_value=listing([renamed_again])))
        self.assertEqual(next(iter(self.index().values()))["name"], renamed_again["name"])

    def test_second_edit_after_interrupted_metadata_recovers_current_owned_record(self):
        from gradient_ascent import ridewithgps

        original = trip()
        self.sync(mock.Mock(side_effect=[listing([original]), {"trip": original}]))
        records = self.index()
        records[next(iter(records))]["name"] = "My authored title"
        (self.workspace / "recordings" / "activities.json").write_text(json.dumps(records))
        first_edit = {**trip(updated="2", watts=250), "name": "First upstream edit"}
        write_json = ridewithgps._write_json

        def fail_metadata(directory, name, value, limit):
            if name == "101.json":
                raise OSError("synthetic metadata failure")
            return write_json(directory, name, value, limit)

        with mock.patch.object(ridewithgps, "_write_json", side_effect=fail_metadata):
            with self.assertRaisesRegex(OSError, "synthetic"):
                self.sync(mock.Mock(side_effect=[listing([first_edit]), {"trip": first_edit}]))
        interim_id = next(iter(self.index()))
        self.assertEqual(self.index()[interim_id]["name"], "My authored title")
        second_edit = {**trip(updated="3", watts=275), "name": "Second upstream edit"}
        self.sync(mock.Mock(side_effect=[listing([second_edit]), {"trip": second_edit}]))
        self.assertEqual(len(self.index()), 1)
        self.assertEqual(next(iter(self.index().values()))["name"], "My authored title")
        self.assertNotIn(interim_id, self.index())
        self.assertTrue(
            (self.workspace / "recordings" / "streams" / f"{interim_id}.json").is_file()
        )

    def test_conflicting_authored_titles_fail_closed_during_recovery(self):
        from gradient_ascent.ridewithgps import trip_to_tcx

        original = trip()
        self.sync(mock.Mock(side_effect=[listing([original]), {"trip": original}]))
        records = self.index()
        first_id = next(iter(records))
        records[first_id]["name"] = "First authored title"
        other_id = (
            "recording-" + hashlib.sha256(trip_to_tcx(trip(updated="2", watts=250))).hexdigest()
        )
        records[other_id] = {**records[first_id], "id": other_id, "name": "Second authored title"}
        (self.workspace / "recordings" / "activities.json").write_text(json.dumps(records))
        edited = trip(updated="3", watts=275)
        with self.assertRaisesRegex(ValueError, "conflicting locally authored titles"):
            self.sync(mock.Mock(side_effect=[listing([edited]), {"trip": edited}]))
        self.assertEqual(self.index(), records)

    def test_manual_edit_collision_cannot_discard_prior_authored_title(self):
        from gradient_ascent.recordings import import_activity_recording
        from gradient_ascent.ridewithgps import trip_to_tcx

        original = trip()
        self.sync(mock.Mock(side_effect=[listing([original]), {"trip": original}]))
        records = self.index()
        records[next(iter(records))]["name"] = "My authored title"
        (self.workspace / "recordings" / "activities.json").write_text(json.dumps(records))
        edited = trip(updated="2", watts=250)
        manual_path = self.root / "manual-future-edit.tcx"
        manual_path.write_bytes(trip_to_tcx(edited))
        import_activity_recording(self.workspace, manual_path)
        before = self.index()
        with self.assertRaisesRegex(ValueError, "locally authored title"):
            self.sync(mock.Mock(side_effect=[listing([edited]), {"trip": edited}]))
        self.assertEqual(self.index(), before)

    def test_full_history_checkpoint_resumes_after_completed_page(self):
        first = trip(101, departure="2020-01-01T09:00:00-08:00")
        second = trip(102, departure="2019-01-01T09:00:00-08:00")
        result = self.sync(
            mock.Mock(side_effect=[listing([first], 2), {"trip": first}]),
            full_history=True,
            max_pages=1,
        )
        self.assertFalse(result["complete"])
        self.assertEqual(result["next_page"], 2)
        second_client = mock.Mock(side_effect=[listing([second]), {"trip": second}])
        result = self.sync(second_client, full_history=True, max_pages=1)
        self.assertEqual(
            second_client.call_args_list[0],
            mock.call("/api/v1/trips.json", {"page": 2, "page_size": 20}),
        )
        self.assertTrue(result["complete"])
        self.assertEqual(len(self.index()), 2)

    def test_failed_page_is_replayed_without_losing_completed_history(self):
        first = trip(101, departure="2020-01-01T09:00:00-08:00")
        client = mock.Mock(
            side_effect=[
                listing([first], 2),
                {"trip": first},
                RuntimeError("synthetic transport failure"),
            ]
        )
        with self.assertRaisesRegex(RuntimeError, "synthetic"):
            self.sync(client, full_history=True)
        resumed = mock.Mock(return_value=listing([]))
        self.sync(resumed, full_history=True)
        self.assertEqual(
            resumed.call_args_list[0], mock.call("/api/v1/trips.json", {"page": 2, "page_size": 20})
        )
        self.assertEqual(len(self.index()), 1)

    def test_local_date_dst_and_existing_strava_overlap(self):
        from gradient_ascent.canonical import canonical_activity_records, resolve_activity_records

        record = trip(departure="2026-08-15T23:30:00-07:00")
        (self.workspace / "strava" / "activities.json").write_text(
            json.dumps(
                {
                    "9": {
                        "id": 9,
                        "sport_type": "Ride",
                        "start_date": "2026-08-16T06:30:02Z",
                        "start_date_local": "2026-08-15T23:30:02Z",
                        "moving_time": 120,
                        "distance": 1000,
                    }
                }
            )
        )
        self.today = date(2026, 8, 15)
        self.sync(mock.Mock(side_effect=[listing([record]), {"trip": record}]), days=1)
        resolved, _ = resolve_activity_records(canonical_activity_records(self.workspace))
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]["source"]["provider"], "strava")

    def test_rejects_unsafe_paths_limits_and_untrusted_pagination(self):
        from gradient_ascent.ridewithgps import sync_ridewithgps

        for options in ({"days": True}, {"max_pages": 0}, {"page_size": 201}):
            with self.assertRaises(ValueError):
                sync_ridewithgps(self.workspace, mock.Mock(), **options)
        outside = self.root / "outside"
        outside.mkdir()
        (self.workspace / "integrations").mkdir(exist_ok=True)
        (self.workspace / "integrations" / "ridewithgps").symlink_to(
            outside, target_is_directory=True
        )
        with self.assertRaisesRegex(ValueError, "symbolic"):
            self.sync(mock.Mock())
        self.assertEqual(list(outside.iterdir()), [])

    def test_bad_detail_and_malformed_timezone_do_not_create_index_entries(self):
        record = trip()
        wrong = trip(999)
        with self.assertRaisesRegex(ValueError, "identifier"):
            self.sync(mock.Mock(side_effect=[listing([record]), {"trip": wrong}]))
        record["time_zone"] = "Invalid/Private"
        with self.assertRaisesRegex(ValueError, "time zone"):
            self.sync(mock.Mock(return_value=listing([record])))
        self.assertEqual(self.index(), {})

    def test_tcx_bytes_are_deterministic(self):
        from gradient_ascent.ridewithgps import trip_to_tcx

        payload = trip_to_tcx(trip())
        self.assertEqual(payload, trip_to_tcx(trip()))
        self.assertEqual(len(hashlib.sha256(payload).hexdigest()), 64)

    def test_distinct_provider_ids_with_identical_telemetry_keep_their_identity(self):
        first, second = trip(101), trip(102)
        client = mock.Mock(
            side_effect=[listing([first, second]), {"trip": first}, {"trip": second}]
        )
        self.sync(client)
        self.assertEqual(
            {record["source_activity_id"] for record in self.index().values()},
            {"101", "102"},
        )
        repeat = mock.Mock(return_value=listing([first, second]))
        self.assertEqual(self.sync(repeat)["existing"], 2)
        repeat.assert_called_once()

    def test_manual_identical_recording_is_never_claimed_or_retired(self):
        from gradient_ascent.recordings import import_activity_recording
        from gradient_ascent.ridewithgps import trip_to_tcx

        original = trip()
        source = self.root / "my-personal-title.tcx"
        source.write_bytes(trip_to_tcx(original))
        manual = import_activity_recording(self.workspace, source)["activity"]
        original_index = self.index()[manual["id"]]
        self.sync(mock.Mock(side_effect=[listing([original]), {"trip": original}]))
        self.assertEqual(self.index()[manual["id"]], original_index)
        repeat = mock.Mock(return_value=listing([original]))
        self.assertEqual(self.sync(repeat)["existing"], 1)
        repeat.assert_called_once()
        edited = trip(updated="2", watts=250)
        self.sync(mock.Mock(side_effect=[listing([edited]), {"trip": edited}]))
        self.assertEqual(self.index()[manual["id"]], original_index)
        self.assertEqual(len(self.index()), 2)

    def test_backfill_byte_limit_checkpoints_mid_page_and_resumes(self):
        from gradient_ascent.ridewithgps import trip_to_tcx

        first, second = trip(101), trip(102)
        client = mock.Mock(
            side_effect=[listing([first, second]), {"trip": first}, {"trip": second}]
        )
        with mock.patch("gradient_ascent.ridewithgps.MAX_SYNC_BYTES", len(trip_to_tcx(first)) + 1):
            result = self.sync(client, full_history=True)
        self.assertFalse(result["complete"])
        self.assertEqual((result["next_page"], result["next_offset"]), (1, 1))
        resumed = mock.Mock(side_effect=[listing([first, second]), {"trip": second}])
        self.assertTrue(self.sync(resumed, full_history=True)["complete"])
        self.assertEqual(len(self.index()), 2)

    def test_malformed_or_cross_origin_pagination_is_rejected_before_details(self):
        for meta in (
            ["invalid"],
            {
                "pagination": {
                    "next_page_url": "https://example.invalid/api/v1/trips.json?page=2&page_size=20"
                }
            },
        ):
            with self.subTest(meta_type=type(meta).__name__):
                client = mock.Mock(return_value={"trips": [trip()], "meta": meta})
                with self.assertRaisesRegex(ValueError, "pagination"):
                    self.sync(client)
                client.assert_called_once()
        self.assertEqual(self.index(), {})

    def test_generic_reimport_preserves_provider_provenance_and_local_start(self):
        from gradient_ascent.recordings import import_activity_recording

        original = trip()
        self.sync(mock.Mock(side_effect=[listing([original]), {"trip": original}]))
        old_id, before = next(iter(self.index().items()))
        source = (
            self.workspace
            / "integrations"
            / "ridewithgps"
            / "files"
            / f"101-{old_id.removeprefix('recording-')}.tcx"
        )
        import_activity_recording(self.workspace, source, filename="manual-copy.tcx")
        after = self.index()[old_id]
        for key in (
            "source_provider",
            "source_provider_name",
            "source_activity_id",
            "source_url",
            "start_date_local",
        ):
            self.assertEqual(after.get(key), before[key])
        edited = trip(updated="2", watts=250)
        self.sync(mock.Mock(side_effect=[listing([edited]), {"trip": edited}]))
        self.assertNotIn(old_id, self.index())
        self.assertEqual(len(self.index()), 1)
