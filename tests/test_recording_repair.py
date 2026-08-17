import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gradient_ascent.recordings import import_activity_recording
from gradient_ascent.storage import write_json


def tcx():
    points = []
    for second in range(601):
        minute, sec = divmod(second, 60)
        hr = "<HeartRateBpm><Value>140</Value></HeartRateBpm>" if second % 2 == 0 else ""
        points.append(
            f"<Trackpoint><Time>2026-01-01T00:{minute:02}:{sec:02}Z</Time>{hr}"
            f"<Extensions><Watts>{100 if second % 2 == 0 else 300}</Watts></Extensions></Trackpoint>"
        )
    return (
        "<TrainingCenterDatabase><Activities><Activity Sport='Biking'><Lap><Track>"
        + "".join(points)
        + "</Track></Lap></Activity></Activities></TrainingCenterDatabase>"
    ).encode()


class RecordingRepairTest(unittest.TestCase):
    def test_original_retention_and_versioned_repair_preserve_user_fields_and_laps(self):
        from gradient_ascent.recording_repair import repair_recordings
        from gradient_ascent.activity_files import RECORDING_STREAM_VERSION
        from gradient_ascent.insights import build_insights

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            root.mkdir(mode=0o700)
            original = Path(tmp) / "ride.tcx"
            body = tcx()
            original.write_bytes(body)
            result = import_activity_recording(root, original)
            identifier = result["activity"]["id"]
            index_path = root / "recordings/activities.json"
            index = json.loads(index_path.read_text())
            index[identifier].update(
                name="Authored", source_moving_time=580, source_elapsed_time=600, notes="keep"
            )
            index[identifier].pop("recording_parser_version", None)
            write_json(index_path, index)
            stream_path = root / "recordings/streams" / f"{identifier}.json"
            write_json(
                stream_path,
                {
                    "streams": [
                        {"type": "time", "data": [0, 600]},
                        {"type": "watts", "data": [100, 100]},
                    ]
                },
            )
            write_json(root / "plan/athlete.json", {"ftp_w": 200})
            build_insights(root, None, root / "derived")
            self.assertIsNone(
                json.loads((root / "derived/activities.json").read_text())[0]["estimated_tss"]
            )
            laps_path = root / "recordings/laps" / f"{identifier}.json"
            laps_before = laps_path.read_bytes()
            retained = root / "recordings/files" / f"{identifier.removeprefix('recording-')}.tcx"
            self.assertEqual(retained.read_bytes(), body)
            self.assertEqual(retained.stat().st_mode & 0o777, 0o600)
            summary = repair_recordings(root)
            self.assertEqual(summary["repaired"], 1)
            repaired = json.loads(index_path.read_text())[identifier]
            self.assertEqual(repaired["recording_parser_version"], RECORDING_STREAM_VERSION)
            for key in ("name", "source_moving_time", "source_elapsed_time", "notes"):
                self.assertEqual(repaired[key], index[identifier][key])
            self.assertEqual(laps_path.read_bytes(), laps_before)
            self.assertEqual(retained.read_bytes(), body)
            streams = json.loads(stream_path.read_text())["streams"]
            self.assertEqual(len(next(x["data"] for x in streams if x["type"] == "watts")), 601)
            build_insights(root, None, root / "derived")
            self.assertEqual(
                json.loads((root / "derived/activities.json").read_text())[0]["estimated_tss"], 16.7
            )
            with mock.patch("gradient_ascent.recording_repair.parse_activity_recording") as parse:
                self.assertEqual(repair_recordings(root)["current"], 1)
                parse.assert_not_called()

    def test_missing_mismatched_and_symlink_originals_are_not_claimed_repaired(self):
        from gradient_ascent.recording_repair import repair_recordings

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            digest = hashlib.sha256(tcx()).hexdigest()
            identifier = "recording-" + digest
            row = {"id": identifier, "import_source": "local_recording", "recording_format": "tcx"}
            write_json(root / "recordings/activities.json", {identifier: row})
            before = (root / "recordings/activities.json").read_bytes()
            self.assertEqual(repair_recordings(root)["unavailable"], 1)
            files = root / "recordings/files"
            files.mkdir(exist_ok=True, mode=0o700)
            raw = files / f"{digest}.tcx"
            raw.write_bytes(b"wrong")
            self.assertEqual(repair_recordings(root)["errors"], 1)
            raw.unlink()
            elsewhere = root / "elsewhere.tcx"
            elsewhere.write_bytes(tcx())
            raw.symlink_to(elsewhere)
            self.assertEqual(repair_recordings(root)["errors"], 1)
            self.assertEqual((root / "recordings/activities.json").read_bytes(), before)

    def test_proven_legacy_lap_np_is_removed_but_marked_source_metric_wins(self):
        from gradient_ascent.recording_repair import merge_recording_metrics

        laps = [
            {"weighted_average_watts": 100, "moving_time": 600},
            {"weighted_average_watts": 300, "moving_time": 600},
        ]
        old = {"weighted_average_watts": 200, "estimated_tss": 77, "name": "keep"}
        parsed = {"summary": {}, "laps": {"laps": laps}}
        self.assertNotIn("weighted_average_watts", merge_recording_metrics(old, parsed, laps))
        marked = {**old, "weighted_average_watts_source": "device"}
        self.assertEqual(merge_recording_metrics(marked, parsed, laps), marked)
        self.assertEqual(merge_recording_metrics(old, parsed, []), old)
        session = {
            "summary": {
                "weighted_average_watts": 250,
                "weighted_average_watts_source": "fit_session",
                "estimated_tss": 90,
            },
            "laps": {"laps": laps},
        }
        merged = merge_recording_metrics(old, session, laps)
        self.assertEqual(merged["weighted_average_watts"], 250)
        self.assertEqual(merged["estimated_tss"], 77)

    def test_exact_reimport_preserves_marked_source_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            original = Path(tmp) / "ride.tcx"
            original.write_bytes(tcx())
            identifier = import_activity_recording(root, original)["activity"]["id"]
            path = root / "recordings/activities.json"
            index = json.loads(path.read_text())
            expected = {
                "weighted_average_watts": 250,
                "weighted_average_watts_source": "device",
                "estimated_tss": 77,
                "intensity_factor": 0.91,
                "timer_time": 600,
            }
            index[identifier].update(expected)
            write_json(path, index)
            result = import_activity_recording(root, original)
            self.assertFalse(result["created"])
            for key, value in expected.items():
                self.assertEqual(result["activity"][key], value)

    def test_invalid_originals_still_consume_the_per_refresh_byte_budget(self):
        from gradient_ascent import recording_repair

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = {}
            files = root / "recordings/files"
            files.mkdir(parents=True, mode=0o700)
            for letter in ("a", "b"):
                digest = letter * 64
                identifier = "recording-" + digest
                rows[identifier] = {
                    "id": identifier,
                    "import_source": "local_recording",
                    "recording_format": "tcx",
                }
                (files / f"{digest}.tcx").write_bytes(b"invalid")
            write_json(root / "recordings/activities.json", rows)
            with mock.patch.object(recording_repair, "MAX_REPAIR_BYTES", 7):
                result = recording_repair.repair_recordings(root)
            self.assertEqual(result["errors"], 1)
            self.assertEqual(result["unavailable"], 1)

    def test_explicit_private_file_in_shared_input_directory_still_imports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shared = root / "shared-input"
            shared.mkdir(mode=0o777)
            os.chmod(shared, 0o777)
            original = shared / "ride.tcx"
            original.write_bytes(tcx())
            os.chmod(original, 0o600)
            self.assertTrue(import_activity_recording(root / "workspace", original)["created"])

    def test_decoder_failure_is_aggregate_only_and_does_not_mark_repaired(self):
        from gradient_ascent import recording_repair

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            original = Path(tmp) / "ride.tcx"
            original.write_bytes(tcx())
            identifier = import_activity_recording(root, original)["activity"]["id"]
            path = root / "recordings/activities.json"
            index = json.loads(path.read_text())
            index[identifier].pop("recording_parser_version")
            write_json(path, index)
            before = path.read_bytes()
            with mock.patch.object(
                recording_repair,
                "parse_activity_recording",
                side_effect=RuntimeError("private decoder detail"),
            ):
                result = recording_repair.repair_recordings(root)
            self.assertEqual(
                result,
                {"repaired": 0, "current": 0, "unavailable": 0, "errors": 1, "unsupported": 0},
            )
            self.assertEqual(path.read_bytes(), before)

    def test_unsupported_secure_io_preserves_offline_import_and_reports_skip(self):
        from gradient_ascent import recording_repair
        from gradient_ascent.cli import _init_data_dir
        from gradient_ascent.refresh import refresh_workspace

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            original = Path(tmp) / "ride.tcx"
            original.write_bytes(tcx())
            _init_data_dir(root)
            with mock.patch.object(recording_repair, "_secure_files_supported", return_value=False):
                result = import_activity_recording(root, original)
                self.assertTrue(result["created"])
                repair = recording_repair.repair_recordings(root)
                self.assertEqual(repair["unsupported"], 1)
                self.assertEqual(repair["repaired"], 0)
                self.assertEqual(
                    refresh_workspace(root)["imports"]["recording_repair"]["unsupported"], 1
                )
            self.assertFalse((root / "recordings/files").exists())

    def test_optional_retention_limit_does_not_shrink_supported_import_size(self):
        from gradient_ascent import recording_repair

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            original = Path(tmp) / "ride.tcx"
            original.write_bytes(tcx())
            with mock.patch.object(recording_repair, "MAX_ORIGINAL_BYTES", 10):
                self.assertTrue(import_activity_recording(root, original)["created"])
            self.assertFalse((root / "recordings/files").exists())

    def test_replaced_workspace_during_parse_is_never_written(self):
        from gradient_ascent import recording_repair

        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            root = parent / "workspace"
            old = parent / "old-workspace"
            original = parent / "ride.tcx"
            original.write_bytes(tcx())
            identifier = import_activity_recording(root, original)["activity"]["id"]
            path = root / "recordings/activities.json"
            index = json.loads(path.read_text())
            index[identifier].pop("recording_parser_version")
            write_json(path, index)
            old_index = path.read_bytes()
            parse = recording_repair.parse_activity_recording

            def replace_root(*args):
                parsed = parse(*args)
                root.rename(old)
                root.mkdir(mode=0o700)
                write_json(root / "sentinel.json", {"replacement": True})
                return parsed

            with mock.patch.object(
                recording_repair, "parse_activity_recording", side_effect=replace_root
            ):
                with self.assertRaisesRegex(RuntimeError, "generation changed"):
                    recording_repair.repair_recordings(root)
            self.assertEqual(sorted(path.name for path in root.iterdir()), ["sentinel.json"])
            self.assertEqual((old / "recordings/activities.json").read_bytes(), old_index)

    def test_refresh_rechecks_captured_generation_before_derived_writes(self):
        from gradient_ascent import refresh
        from gradient_ascent.cli import _init_data_dir

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            old = Path(tmp) / "old-workspace"
            _init_data_dir(root)

            def replace_root(*args, **kwargs):
                root.rename(old)
                root.mkdir(mode=0o700)
                write_json(root / "sentinel.json", {"replacement": True})
                return {
                    "repaired": 0,
                    "current": 0,
                    "unavailable": 0,
                    "errors": 0,
                    "unsupported": 0,
                }

            with (
                mock.patch.object(refresh, "repair_recordings", side_effect=replace_root),
                mock.patch.object(refresh, "build_canonical_files") as canonical,
            ):
                with self.assertRaisesRegex(RuntimeError, "generation changed"):
                    refresh.refresh_workspace(root)
                canonical.assert_not_called()
            self.assertEqual(sorted(path.name for path in root.iterdir()), ["sentinel.json"])

    def test_write_after_identity_check_still_uses_pinned_original_root(self):
        from gradient_ascent import recording_repair

        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            root, old = parent / "workspace", parent / "old-workspace"
            original = parent / "ride.tcx"
            original.write_bytes(tcx())
            identifier = import_activity_recording(root, original)["activity"]["id"]
            path = root / "recordings/activities.json"
            index = json.loads(path.read_text())
            index[identifier].pop("recording_parser_version")
            write_json(path, index)
            before = path.read_bytes()
            write = recording_repair._write

            def replace_before_write(directory, name, body, limit):
                if name == f"{identifier}.json":
                    root.rename(old)
                    root.mkdir(mode=0o700)
                    write_json(root / "sentinel.json", {"replacement": True})
                return write(directory, name, body, limit)

            with mock.patch.object(recording_repair, "_write", side_effect=replace_before_write):
                with self.assertRaisesRegex(RuntimeError, "generation changed"):
                    recording_repair.repair_recordings(root)
            self.assertEqual(sorted(path.name for path in root.iterdir()), ["sentinel.json"])
            self.assertEqual((old / "recordings/activities.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
