import base64
import gzip
import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from unittest import mock
from zipfile import ZipFile

from gradient_ascent.connections import provider_summary
from gradient_ascent.recordings import import_activity_recording
from gradient_ascent.strava import import_strava_export


ACTIVITIES_CSV = """Activity ID,Activity Date,Activity Name,Activity Type,Activity Description,Elapsed Time,Moving Time,Distance,Elevation Gain,Average Heart Rate,Max Heart Rate,Average Watts,Weighted Average Power,Kilojoules,Filename
123,"May 1, 2026, 8:00:00 AM",Morning Ride,Ride,private note,3700,3600,30500.0,450,151,182,205,221,740,activities/123.fit.gz
"""

FULL_MONTH_ACTIVITIES_CSV = """Activity ID,Activity Date,Activity Name,Activity Type,Moving Time,Distance
456,"June 15, 2026, 8:10:00 AM",Morning Commute,Ride,1800,9400.0
"""

TCX_ACTIVITY = """<?xml version="1.0" encoding="UTF-8"?>
<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"
  xmlns:ns3="http://www.garmin.com/xmlschemas/ActivityExtension/v2">
  <Activities><Activity Sport="Biking"><Id>2026-05-01T08:00:00Z</Id>
    <Lap StartTime="2026-05-01T08:00:00Z">
      <TotalTimeSeconds>120</TotalTimeSeconds><DistanceMeters>1000</DistanceMeters>
      <AverageHeartRateBpm><Value>140</Value></AverageHeartRateBpm>
      <MaximumHeartRateBpm><Value>150</Value></MaximumHeartRateBpm>
      <Extensions><ns3:LX><ns3:AvgSpeed>8.33</ns3:AvgSpeed><ns3:AvgWatts>220</ns3:AvgWatts><ns3:MaxWatts>240</ns3:MaxWatts></ns3:LX></Extensions>
      <Track>
        <Trackpoint><Time>2026-05-01T08:00:00Z</Time><Position><LatitudeDegrees>37.0</LatitudeDegrees><LongitudeDegrees>-122.0</LongitudeDegrees></Position><AltitudeMeters>10</AltitudeMeters><DistanceMeters>0</DistanceMeters><HeartRateBpm><Value>130</Value></HeartRateBpm><Cadence>80</Cadence><Extensions><ns3:TPX><ns3:Speed>8</ns3:Speed><ns3:Watts>200</ns3:Watts></ns3:TPX></Extensions></Trackpoint>
        <Trackpoint><Time>2026-05-01T08:01:00Z</Time><Position><LatitudeDegrees>37.001</LatitudeDegrees><LongitudeDegrees>-122.001</LongitudeDegrees></Position><AltitudeMeters>15</AltitudeMeters><DistanceMeters>500</DistanceMeters><HeartRateBpm><Value>140</Value></HeartRateBpm><Cadence>85</Cadence><Extensions><ns3:TPX><ns3:Speed>8.5</ns3:Speed><ns3:Watts>220</ns3:Watts></ns3:TPX></Extensions></Trackpoint>
        <Trackpoint><Time>2026-05-01T08:02:00Z</Time><Position><LatitudeDegrees>37.002</LatitudeDegrees><LongitudeDegrees>-122.002</LongitudeDegrees></Position><AltitudeMeters>20</AltitudeMeters><DistanceMeters>1000</DistanceMeters><HeartRateBpm><Value>150</Value></HeartRateBpm><Cadence>90</Cadence><Extensions><ns3:TPX><ns3:Speed>9</ns3:Speed><ns3:Watts>240</ns3:Watts></ns3:TPX></Extensions></Trackpoint>
      </Track>
    </Lap>
  </Activity></Activities>
</TrainingCenterDatabase>
"""

GPX_ACTIVITY = """<?xml version="1.0" encoding="UTF-8"?>
<gpx xmlns="http://www.topografix.com/GPX/1/1" xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1">
  <trk><name>Archive ride</name><trkseg>
    <trkpt lat="37.0" lon="-122.0"><ele>10</ele><time>2026-05-02T08:00:00Z</time><extensions><gpxtpx:TrackPointExtension><gpxtpx:hr>120</gpxtpx:hr><gpxtpx:cad>75</gpxtpx:cad><gpxtpx:atemp>18</gpxtpx:atemp></gpxtpx:TrackPointExtension><power>180</power></extensions></trkpt>
    <trkpt lat="37.001" lon="-122.001"><ele>12</ele><time>2026-05-02T08:01:00Z</time><extensions><gpxtpx:TrackPointExtension><gpxtpx:hr>130</gpxtpx:hr><gpxtpx:cad>80</gpxtpx:cad><gpxtpx:atemp>19</gpxtpx:atemp></gpxtpx:TrackPointExtension><power>200</power></extensions></trkpt>
    <trkpt lat="37.002" lon="-122.002"><ele>14</ele><time>2026-05-02T08:02:00Z</time><extensions><gpxtpx:TrackPointExtension><gpxtpx:hr>140</gpxtpx:hr><gpxtpx:cad>85</gpxtpx:cad><gpxtpx:atemp>20</gpxtpx:atemp></gpxtpx:TrackPointExtension><power>220</power></extensions></trkpt>
  </trkseg></trk>
</gpx>
"""

# Sample from fitdecode's MIT-licensed test fixtures. It contains fourteen GPS
# records and one lap. The complete upstream notice is preserved in
# tests/fixtures/fitdecode-LICENSE.txt.
FIT_ACTIVITY = base64.b64decode(
    "DBBkAPUCAAAuRklUQAABAAAFAwSMBASGAQKEAgKEAAEAAH////8p5gcSAA8AAQRAAAEAMQIAAoQBAQJAAAEAMQEAAoQAAPBBAAEAFQX9BIYDBIYAAQABAQAEAQJBAAEAFQX9BIYDAQAAAQABAQAEAQIBKeYHEgAAAABCAAEAFAb9BIYABIUBBIUFBIYCAoQGAoQCKeYHEh2FYS7L+7SXAAAAAg8zAAACKeYHEx2FYS7L+7SYAAAAAg8zAAACKeYHFB2FYS7L+7SYAAAAAg8zAAACKeYHFR2FYTnL+7SCAAAAFQ8zAAACKeYHFh2FYUDL+7R5AAAAHA8zAAACKeYHFx2FYUbL+7RyAAAAIw8zAAACKeYHGB2FYUrL+7RsAAAAKQ8zAAACKeYHGR2FYXfL+7QUAAAAcg8zAAACKeYHGh2FYY3L+7O0AAAAuQ8zAFwCKeYHGx2FYa7L+7M8AAABEw8zAJgCKeYHHB2FYczL+7LXAAABXw8zANECKeYHHR2FYarL+7J5AAABpg8zAQYCKeYHHh2FYV/L+7KNAAAB7Q8zATMCKeYHHx2FYRLL+7JXAAACPQ8zAXABKeYHHwAABABDAAEAExT9BIYCBIYDBIUEBIUFBIUGBIUHBIYIBIYJBIb+AoQLAoQMAoQNAoQOAoQVAoQWAoQAAQABAQAYAQAZAQADKeYHoynmBxIdhWEuy/u0lx2FYRLL+7JXAAA1tQAANbUAAAI9AAAAAAAAAaEBcAAAAAAJAQcBQQABABUF/QSGAwSGAAEAAQEABAECASnmB6MAAAABCAkBRAABABIV/QSGAgSGAwSFBASFBwSGCASGCQSG/gKECwKEDQKEDgKEDwKEFgKEFwKEGQKEGgKEAAEAAQEABQEABgEAHAEABCnmB6Mp5gcSHYVhLsv7tJcAADW1AAA1tQAAAj0AAAAAAAABoQFwAAAAAAAAAAEJAQEAAEUAAQAiB/0EhgAEhgUEhgEChAIBAAMBAAQBAAUp5gejAAA1tSnlz2MAAQAaAdWh"
)


def _stream_data(data_dir: Path, activity_id: str, stream_type: str):
    payload = json.loads((data_dir / "strava" / "streams" / f"{activity_id}.json").read_text())
    return next(stream["data"] for stream in payload["streams"] if stream["type"] == stream_type)


class StravaExportImportTest(unittest.TestCase):
    def test_cli_imports_standalone_recording_before_dashboard_setup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            recording_path = root / "morning-ride.tcx"
            recording_path.write_text(TCX_ACTIVITY, encoding="utf-8")
            env = {
                **os.environ,
                "COACH_WORKSPACE_DIR": str(workspace),
                "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
            }
            initialized = subprocess.run(
                [sys.executable, "-m", "gradient_ascent.cli", "init-workspace", str(workspace)],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
            imported = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "gradient_ascent.cli",
                    "import-activity-recording",
                    str(recording_path),
                ],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
            activities = json.loads(
                (workspace / "recordings" / "activities.json").read_text(encoding="utf-8")
            )

        self.assertEqual(initialized.returncode, 0, initialized.stdout)
        self.assertEqual(imported.returncode, 0, imported.stdout)
        self.assertIn("Activity recording import complete", imported.stdout)
        self.assertEqual(len(activities), 1)

    def test_import_standalone_tcx_creates_a_deduplicated_local_activity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "workspace"
            recording_path = Path(tmp) / "morning-ride.tcx"
            recording_path.write_text(TCX_ACTIVITY, encoding="utf-8")

            result = import_activity_recording(data_dir, recording_path)
            repeated = import_activity_recording(data_dir, recording_path)
            activity_id = result["activity"]["id"]
            activities = json.loads(
                (data_dir / "recordings" / "activities.json").read_text()
            )
            streams = json.loads(
                (data_dir / "recordings" / "streams" / f"{activity_id}.json").read_text()
            )
            laps = json.loads(
                (data_dir / "recordings" / "laps" / f"{activity_id}.json").read_text()
            )

        self.assertTrue(result["created"])
        self.assertFalse(repeated["created"])
        self.assertTrue(activity_id.startswith("recording-"))
        self.assertEqual(activities[activity_id]["name"], "Morning Ride")
        self.assertEqual(activities[activity_id]["sport_type"], "Ride")
        self.assertEqual(activities[activity_id]["start_date"], "2026-05-01T08:00:00Z")
        self.assertEqual(activities[activity_id]["distance"], 1000.0)
        self.assertEqual(activities[activity_id]["average_watts"], 220.0)
        self.assertEqual(streams["source"], "local_recording")
        self.assertEqual(len(laps["laps"]), 1)

    def test_import_directory_parses_tcx_streams_and_laps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "workspace"
            export_dir = Path(tmp) / "strava-export"
            recordings_dir = export_dir / "activities"
            recordings_dir.mkdir(parents=True)
            (export_dir / "activities.csv").write_text(
                ACTIVITIES_CSV.replace("activities/123.fit.gz", "activities/123.tcx"),
                encoding="utf-8",
            )
            (recordings_dir / "123.tcx").write_text(TCX_ACTIVITY, encoding="utf-8")

            result = import_strava_export(data_dir, export_dir)
            laps = json.loads((data_dir / "strava" / "laps" / "123.json").read_text())
            time_data = _stream_data(data_dir, "123", "time")
            heartrate = _stream_data(data_dir, "123", "heartrate")
            watts = _stream_data(data_dir, "123", "watts")
            latlng = _stream_data(data_dir, "123", "latlng")

        self.assertEqual(result["recordings_parsed"], 1)
        self.assertEqual(result["recording_formats"], {"tcx": 1})
        self.assertEqual(time_data, [0.0, 60.0, 120.0])
        self.assertEqual(heartrate, [130.0, 140.0, 150.0])
        self.assertEqual(watts, [200.0, 220.0, 240.0])
        self.assertEqual(latlng[1], [37.001, -122.001])
        self.assertEqual(laps["laps"][0]["average_watts"], 220.0)
        self.assertEqual(laps["laps"][0]["average_heartrate"], 140.0)
        self.assertEqual(laps["laps"][0]["average_cadence"], 85.0)

    def test_import_nested_zip_parses_gzipped_gpx_streams(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "workspace"
            archive_path = Path(tmp) / "strava-export.zip"
            csv_payload = ACTIVITIES_CSV.replace(
                "activities/123.fit.gz", "activities/123.gpx.gz"
            )
            with ZipFile(archive_path, "w") as archive:
                archive.writestr("export_123/activities.csv", csv_payload)
                archive.writestr("export_123/activities/123.gpx.gz", gzip.compress(GPX_ACTIVITY.encode()))

            result = import_strava_export(data_dir, archive_path)
            watts = _stream_data(data_dir, "123", "watts")
            heartrate = _stream_data(data_dir, "123", "heartrate")
            temperature = _stream_data(data_dir, "123", "temp")
            distance = _stream_data(data_dir, "123", "distance")
            repeated = import_strava_export(data_dir, archive_path)

        self.assertEqual(result["recordings_parsed"], 1)
        self.assertEqual(result["recording_formats"], {"gpx": 1})
        self.assertEqual(watts, [180.0, 200.0, 220.0])
        self.assertEqual(heartrate, [120.0, 130.0, 140.0])
        self.assertEqual(temperature, [18.0, 19.0, 20.0])
        self.assertGreater(distance[-1], 200.0)
        self.assertEqual(repeated["recordings_skipped_existing"], 1)
        self.assertEqual(repeated["recordings_parsed"], 0)

    def test_import_nested_zip_parses_gzipped_fit_streams_and_laps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "workspace"
            archive_path = Path(tmp) / "strava-export.zip"
            with ZipFile(archive_path, "w") as archive:
                archive.writestr("export_123/activities.csv", ACTIVITIES_CSV)
                archive.writestr("export_123/activities/123.fit.gz", gzip.compress(FIT_ACTIVITY))

            result = import_strava_export(data_dir, archive_path)
            laps = json.loads((data_dir / "strava" / "laps" / "123.json").read_text())
            time_data = _stream_data(data_dir, "123", "time")
            latlng = _stream_data(data_dir, "123", "latlng")

        self.assertEqual(result["recordings_parsed"], 1)
        self.assertEqual(result["recording_formats"], {"fit": 1})
        self.assertEqual(len(time_data), 14)
        self.assertEqual(len(latlng), 14)
        self.assertEqual(len(laps["laps"]), 1)

    def test_large_archive_parses_recordings_in_bounded_parallel_workers(self) -> None:
        rows = [
            ("10", "activities/10.fit.gz"),
            ("11", "activities/11.tcx"),
            ("12", "activities/12.gpx.gz"),
            ("13", "activities/13.fit"),
            ("14", "activities/14.gpx"),
            ("15", "activities/missing.fit"),
            ("16", "activities/corrupt.fit"),
            ("17", "activities/unsupported.json"),
        ]
        csv_payload = "Activity ID,Activity Date,Activity Name,Activity Type,Filename\n" + "".join(
            f'{activity_id},"May 1, 2026, 8:00:00 AM",Ride {activity_id},Ride,{filename}\n'
            for activity_id, filename in rows
        )
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "workspace"
            archive_path = Path(tmp) / "strava-export.zip"
            with ZipFile(archive_path, "w") as archive:
                archive.writestr("export/activities.csv", csv_payload)
                archive.writestr("export/activities/10.fit.gz", gzip.compress(FIT_ACTIVITY))
                archive.writestr("export/activities/11.tcx", TCX_ACTIVITY)
                archive.writestr("export/activities/12.gpx.gz", gzip.compress(GPX_ACTIVITY.encode()))
                archive.writestr("export/activities/13.fit", FIT_ACTIVITY)
                archive.writestr("export/activities/14.gpx", GPX_ACTIVITY)
                archive.writestr("export/activities/corrupt.fit", b"not a FIT file")

            with (
                mock.patch("os.cpu_count", return_value=4),
                mock.patch(
                    "gradient_ascent.strava.ProcessPoolExecutor",
                    wraps=ProcessPoolExecutor,
                    create=True,
                ) as executor,
            ):
                result = import_strava_export(data_dir, archive_path)

            activities = json.loads((data_dir / "strava" / "activities.json").read_text())
            repeated = import_strava_export(data_dir, archive_path)

        executor.assert_called_once()
        self.assertEqual(executor.call_args.kwargs["max_workers"], 4)
        self.assertEqual(list(activities), [str(activity_id) for activity_id in range(10, 18)])
        self.assertEqual(result["rows"], 8)
        self.assertEqual(result["recordings_referenced"], 8)
        self.assertEqual(result["recordings_parsed"], 5)
        self.assertEqual(result["recordings_missing"], 1)
        self.assertEqual(result["recordings_failed"], 1)
        self.assertEqual(result["recordings_unsupported"], 1)
        self.assertEqual(result["streams_created"], 5)
        self.assertEqual(result["laps_created"], 3)
        self.assertEqual(result["recording_formats"], {"fit": 2, "tcx": 1, "gpx": 2})
        self.assertEqual(repeated["recordings_parsed"], 0)
        self.assertEqual(repeated["recordings_skipped_existing"], 5)
        self.assertEqual(repeated["recordings_missing"], 1)
        self.assertEqual(repeated["recordings_failed"], 1)

    def test_duplicate_activity_ids_preserve_serial_recording_deduplication(self) -> None:
        csv_payload = (
            "Activity ID,Activity Date,Activity Name,Activity Type,Filename\n"
            + "".join(
                f'{activity_id},"May 1, 2026, 8:00:00 AM",Ride,Ride,activities/{activity_id}.fit\n'
                for activity_id in (10, 11, 12, 13, 10)
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "workspace"
            archive_path = Path(tmp) / "strava-export.zip"
            with ZipFile(archive_path, "w") as archive:
                archive.writestr("activities.csv", csv_payload)
                for activity_id in (10, 11, 12, 13):
                    archive.writestr(f"activities/{activity_id}.fit", FIT_ACTIVITY)

            with mock.patch(
                "gradient_ascent.strava.ProcessPoolExecutor", create=True
            ) as executor:
                result = import_strava_export(data_dir, archive_path)

        executor.assert_not_called()
        self.assertEqual(result["recordings_parsed"], 4)
        self.assertEqual(result["recordings_skipped_existing"], 1)

    def test_parallel_unavailable_falls_back_to_serial_recording_import(self) -> None:
        csv_payload = "Activity ID,Activity Date,Activity Name,Activity Type,Filename\n" + "".join(
            f'{activity_id},"May 1, 2026, 8:00:00 AM",Ride,Ride,activities/{activity_id}.fit\n'
            for activity_id in range(10, 14)
        )
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "workspace"
            archive_path = Path(tmp) / "strava-export.zip"
            with ZipFile(archive_path, "w") as archive:
                archive.writestr("activities.csv", csv_payload)
                for activity_id in range(10, 14):
                    archive.writestr(f"activities/{activity_id}.fit", FIT_ACTIVITY)

            with (
                mock.patch("os.cpu_count", return_value=4),
                mock.patch(
                    "gradient_ascent.strava.ProcessPoolExecutor",
                    side_effect=OSError("process pools unavailable"),
                    create=True,
                ),
            ):
                result = import_strava_export(data_dir, archive_path)

        self.assertEqual(result["recordings_parsed"], 4)
        self.assertEqual(result["recording_formats"], {"fit": 4})

    def test_parallel_worker_start_failure_falls_back_before_writing_recordings(self) -> None:
        csv_payload = "Activity ID,Activity Date,Activity Name,Activity Type,Filename\n" + "".join(
            f'{activity_id},"May 1, 2026, 8:00:00 AM",Ride,Ride,activities/{activity_id}.fit\n'
            for activity_id in range(10, 14)
        )
        unavailable_executor = mock.MagicMock()
        unavailable_executor.submit.return_value.result.side_effect = BrokenProcessPool(
            "worker initialization failed"
        )
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "workspace"
            archive_path = Path(tmp) / "strava-export.zip"
            with ZipFile(archive_path, "w") as archive:
                archive.writestr("activities.csv", csv_payload)
                for activity_id in range(10, 14):
                    archive.writestr(f"activities/{activity_id}.fit", FIT_ACTIVITY)

            with (
                mock.patch("os.cpu_count", return_value=4),
                mock.patch(
                    "gradient_ascent.strava.ProcessPoolExecutor",
                    return_value=unavailable_executor,
                    create=True,
                ),
            ):
                result = import_strava_export(data_dir, archive_path)

        unavailable_executor.shutdown.assert_called_once_with(wait=True, cancel_futures=True)
        self.assertEqual(result["recordings_parsed"], 4)
        self.assertEqual(result["streams_created"], 4)

    def test_resume_preserves_stream_without_laps_or_completed_activity_index(self) -> None:
        previous_streams = {"streams": [{"type": "time", "data": [42]}], "source": "api"}
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "workspace"
            streams_dir = data_dir / "strava" / "streams"
            streams_dir.mkdir(parents=True)
            (streams_dir / "123.json").write_text(
                json.dumps(previous_streams), encoding="utf-8"
            )
            archive_path = Path(tmp) / "strava-export.zip"
            with ZipFile(archive_path, "w") as archive:
                archive.writestr(
                    "activities.csv",
                    ACTIVITIES_CSV.replace("activities/123.fit.gz", "activities/123.gpx"),
                )
                archive.writestr("activities/123.gpx", GPX_ACTIVITY)

            result = import_strava_export(data_dir, archive_path)
            streams = json.loads((streams_dir / "123.json").read_text())
            activity = json.loads((data_dir / "strava" / "activities.json").read_text())["123"]
            repeated = import_strava_export(data_dir, archive_path)

        self.assertEqual(streams, previous_streams)
        self.assertEqual(result["recordings_parsed"], 1)
        self.assertEqual(result["streams_created"], 0)
        self.assertEqual(result["laps_created"], 0)
        self.assertTrue(activity["archive_recording_parsed"])
        self.assertTrue(activity["archive_streams_available"])
        self.assertFalse(activity["archive_laps_available"])
        self.assertEqual(repeated["recordings_skipped_existing"], 1)

    def test_import_reports_missing_and_unsupported_recordings_without_failing_history(self) -> None:
        csv_payload = (
            "Activity ID,Activity Date,Activity Name,Activity Type,Filename\n"
            '1,"May 1, 2026, 8:00:00 AM",Missing,Ride,activities/missing.fit\n'
            '2,"May 2, 2026, 8:00:00 AM",Unsupported,Ride,activities/recording.json\n'
            '3,"May 3, 2026, 8:00:00 AM",Unsafe,Ride,../../outside.fit\n'
        )
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "workspace"
            export_dir = Path(tmp) / "strava-export"
            export_dir.mkdir()
            (export_dir / "activities.csv").write_text(csv_payload, encoding="utf-8")

            result = import_strava_export(data_dir, export_dir)
            activities = json.loads((data_dir / "strava" / "activities.json").read_text())

        self.assertEqual(len(activities), 3)
        self.assertEqual(result["recordings_missing"], 2)
        self.assertEqual(result["recordings_unsupported"], 1)
        self.assertEqual(result["recordings_failed"], 0)

    def test_import_reports_corrupt_recording_without_losing_activity_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "workspace"
            export_dir = Path(tmp) / "strava-export"
            recordings_dir = export_dir / "activities"
            recordings_dir.mkdir(parents=True)
            (export_dir / "activities.csv").write_text(ACTIVITIES_CSV, encoding="utf-8")
            (recordings_dir / "123.fit.gz").write_bytes(gzip.compress(b"not a FIT file"))

            result = import_strava_export(data_dir, export_dir)
            activities = json.loads((data_dir / "strava" / "activities.json").read_text())

        self.assertIn("123", activities)
        self.assertEqual(result["recordings_failed"], 1)
        self.assertEqual(result["recordings_parsed"], 0)

    def test_import_preserves_existing_richer_streams_and_laps(self) -> None:
        existing_streams = {"streams": [{"type": "time", "data": [99]}], "source": "api"}
        existing_laps = {"laps": [{"lap_index": 7}], "source": "api"}
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "workspace"
            (data_dir / "strava" / "streams").mkdir(parents=True)
            (data_dir / "strava" / "laps").mkdir(parents=True)
            (data_dir / "strava" / "streams" / "123.json").write_text(
                json.dumps(existing_streams), encoding="utf-8"
            )
            (data_dir / "strava" / "laps" / "123.json").write_text(
                json.dumps(existing_laps), encoding="utf-8"
            )
            export_dir = Path(tmp) / "strava-export"
            export_dir.mkdir()
            (export_dir / "activities.csv").write_text(ACTIVITIES_CSV, encoding="utf-8")

            result = import_strava_export(data_dir, export_dir)
            streams = json.loads((data_dir / "strava" / "streams" / "123.json").read_text())
            laps = json.loads((data_dir / "strava" / "laps" / "123.json").read_text())

        self.assertEqual(result["recordings_skipped_existing"], 1)
        self.assertEqual(streams, existing_streams)
        self.assertEqual(laps, existing_laps)

    def test_import_accepts_full_month_name_in_activity_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "workspace"
            export_dir = Path(tmp) / "strava-export"
            export_dir.mkdir()
            (export_dir / "activities.csv").write_text(
                FULL_MONTH_ACTIVITIES_CSV,
                encoding="utf-8",
            )

            import_strava_export(data_dir, export_dir)
            activities = json.loads((data_dir / "strava" / "activities.json").read_text())

        self.assertEqual(activities["456"]["start_date_local"], "2026-06-15T08:10:00")

    def test_import_directory_normalizes_archive_csv_without_storing_private_description(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "workspace"
            export_dir = Path(tmp) / "strava-export"
            (data_dir / "strava").mkdir(parents=True)
            export_dir.mkdir()
            (export_dir / "activities.csv").write_text(ACTIVITIES_CSV, encoding="utf-8")
            (data_dir / "strava" / "activities.json").write_text(
                json.dumps({"123": {"id": 123, "name": "API Name", "private": True}}),
                encoding="utf-8",
            )

            result = import_strava_export(data_dir, export_dir)
            activities = json.loads((data_dir / "strava" / "activities.json").read_text())
            state = json.loads((data_dir / "strava" / "state.json").read_text())
            summary = provider_summary(data_dir, "strava")

        self.assertEqual(result["rows"], 1)
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(activities["123"]["name"], "API Name")
        self.assertEqual(activities["123"]["moving_time"], 3600.0)
        self.assertEqual(activities["123"]["distance"], 30500.0)
        self.assertEqual(activities["123"]["average_heartrate"], 151.0)
        self.assertEqual(activities["123"]["weighted_average_watts"], 221.0)
        self.assertEqual(activities["123"]["source_archive_file"], "activities/123.fit.gz")
        self.assertEqual(activities["123"]["import_source"], "strava_bulk_export")
        self.assertNotIn("description", activities["123"])
        self.assertNotIn("private note", json.dumps(activities))
        self.assertEqual(state["archive_import"]["archive_name"], "strava-export")
        self.assertEqual(state["archive_import"]["activity_count"], 1)
        self.assertEqual(summary["status"], "imported")

    def test_import_zip_finds_nested_activities_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "workspace"
            archive_path = Path(tmp) / "strava-export.zip"
            with ZipFile(archive_path, "w") as archive:
                archive.writestr("export_123/activities.csv", ACTIVITIES_CSV)

            result = import_strava_export(data_dir, archive_path)
            activities = json.loads((data_dir / "strava" / "activities.json").read_text())

        self.assertEqual(result["activities_csv"], "export_123/activities.csv")
        self.assertEqual(result["created"], 1)
        self.assertEqual(activities["123"]["sport_type"], "Ride")
        self.assertEqual(activities["123"]["start_date_local"], "2026-05-01T08:00:00")

    def test_import_rejects_archive_without_activities_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "workspace"
            archive_path = Path(tmp) / "strava-export.zip"
            with ZipFile(archive_path, "w") as archive:
                archive.writestr("README.txt", "missing activities")

            with self.assertRaisesRegex(FileNotFoundError, "activities.csv"):
                import_strava_export(data_dir, archive_path)


if __name__ == "__main__":
    unittest.main()
