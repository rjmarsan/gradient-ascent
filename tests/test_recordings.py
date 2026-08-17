import hashlib
import json
import tempfile
import unittest
from pathlib import Path


TCX = b"""<?xml version="1.0"?><TrainingCenterDatabase><Activities><Activity Sport="Biking"><Id>2026-03-01T10:00:00Z</Id><Lap StartTime="2026-03-01T10:00:00Z"><TotalTimeSeconds>60</TotalTimeSeconds><DistanceMeters>500</DistanceMeters><Track><Trackpoint><Time>2026-03-01T10:00:00Z</Time><DistanceMeters>0</DistanceMeters></Trackpoint><Trackpoint><Time>2026-03-01T10:01:00Z</Time><DistanceMeters>500</DistanceMeters></Trackpoint></Track></Lap></Activity></Activities></TrainingCenterDatabase>"""


class RecordingPreparationTest(unittest.TestCase):
    def test_exact_reimport_preserves_only_valid_rwgps_cycling_classification(self):
        from gradient_ascent.recordings import import_activity_recording

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "ride.tcx"
            source.write_bytes(TCX)
            workspace = root / "workspace"
            record = import_activity_recording(workspace, source)["activity"]
            index_path = workspace / "recordings" / "activities.json"
            record.update(
                source_provider="ridewithgps",
                source_activity_id="101",
                source_activity_type="e_biking:mountain",
                source_fit_sport=21,
                source_fit_sub_sport=47,
                sport_type="EMountainBikeRide",
                type="EBikeRide",
            )
            index_path.write_text(json.dumps({record["id"]: record}))
            repeated = import_activity_recording(workspace, source)["activity"]
            for key in (
                "sport_type",
                "type",
                "source_activity_type",
                "source_fit_sport",
                "source_fit_sub_sport",
            ):
                self.assertEqual(repeated[key], record[key])
            record.update(
                source_activity_type="running:trail",
                source_fit_sport=True,
                source_fit_sub_sport="47",
            )
            index_path.write_text(json.dumps({record["id"]: record}))
            rejected = import_activity_recording(workspace, source)["activity"]
            self.assertEqual(rejected["sport_type"], "Ride")
            self.assertNotIn("source_fit_sport", rejected)

    def test_preparation_is_readonly_and_matches_existing_import(self):
        from gradient_ascent.recordings import import_activity_recording, prepare_activity_recording

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "ride.tcx"
            source.write_bytes(TCX)
            prepared = prepare_activity_recording(source, filename="A Ride.tcx")
            self.assertEqual(set(root.iterdir()), {source})
            self.assertEqual(
                prepared["activity"]["id"], f"recording-{hashlib.sha256(TCX).hexdigest()}"
            )
            workspace = root / "workspace"
            result = import_activity_recording(workspace, source, filename="A Ride.tcx")
            self.assertEqual(result["activity"], prepared["activity"])
            self.assertEqual(result["stream_count"], prepared["stream_count"])
            self.assertEqual(result["lap_count"], prepared["lap_count"])
            index = json.loads((workspace / "recordings" / "activities.json").read_text())
            index[result["activity"]["id"]]["name"] = "User title"
            (workspace / "recordings" / "activities.json").write_text(json.dumps(index))
            repeated = import_activity_recording(workspace, source, filename="New title.tcx")
            self.assertFalse(repeated["created"])
            self.assertEqual(repeated["activity"]["name"], "User title")
            self.assertEqual(repeated["activity"]["recording_filename"], "A Ride.tcx")
