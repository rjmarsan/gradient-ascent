import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def streams(times, watts):
    return {"streams": [{"type": "time", "data": times}, {"type": "watts", "data": watts}]}


class PowerMetricsTest(unittest.TestCase):
    def test_constant_and_variable_power_use_actual_thirty_second_windows(self):
        from gradient_ascent.power_metrics import estimate_normalized_power

        constant = estimate_normalized_power(list(range(601)), [200] * 601)
        self.assertEqual(constant["estimated_normalized_power_w"], 200)
        self.assertEqual(constant["observed_duration_s"], 600)
        self.assertEqual(constant["rolling_window_duration_s"], 571)
        values = [0] * 300 + [300] * 300
        expected = (
            sum((sum(values[i - 29 : i + 1]) / 30) ** 4 for i in range(29, 600)) / 571
        ) ** 0.25
        result = estimate_normalized_power(list(range(601)), values + [300])
        self.assertAlmostEqual(result["estimated_normalized_power_w"], expected, places=3)
        self.assertEqual(
            estimate_normalized_power(list(range(601)), [0] * 601)["estimated_normalized_power_w"],
            0,
        )

    def test_irregular_sampling_and_long_gaps_never_fill_missing_power(self):
        from gradient_ascent.power_metrics import estimate_normalized_power

        result = estimate_normalized_power(list(range(0, 1201, 2)), [200] * 601)
        self.assertEqual(result["estimated_normalized_power_w"], 200)
        self.assertEqual(result["observed_duration_s"], 1200)
        times = list(range(601)) + list(range(4200, 4801))
        result = estimate_normalized_power(times, [200] * 601 + [300] * 601)
        self.assertEqual(result["observed_duration_s"], 1200)
        self.assertAlmostEqual(
            result["estimated_normalized_power_w"], ((200**4 + 300**4) / 2) ** 0.25, places=3
        )
        self.assertIsNone(estimate_normalized_power([0, 600, 1200], [200, 200, 200]))
        self.assertIsNone(estimate_normalized_power(list(range(600)), [200] * 600))
        self.assertIsNone(estimate_normalized_power(list(range(601)), [float("nan")] * 601))
        self.assertIsNone(estimate_normalized_power(list(range(601)), [float("inf")] * 601))
        self.assertIsNone(estimate_normalized_power(list(range(601)), [-1] * 601))
        self.assertIsNone(estimate_normalized_power(list(range(601)), [True] * 601))
        self.assertIsNone(estimate_normalized_power(list(range(601)), [200] * 600))

    def test_repeated_or_backtracked_timestamps_cannot_double_count_power(self):
        from gradient_ascent.power_metrics import estimate_normalized_power

        for times in (
            list(range(601)) + list(range(601)),
            list(range(601)) + list(range(300, 601)),
            list(range(601)) + [600, 601],
        ):
            with self.subTest(length=len(times)):
                self.assertIsNone(
                    estimate_normalized_power(times, [200] * 601 + [400] * (len(times) - 601))
                )

    def test_tiny_ftp_never_emits_nonfinite_training_load(self):
        from gradient_ascent.insights import _normalize_activity

        for ftp in (1e-200, 1e-308, 5e-324):
            with self.subTest(ftp=ftp):
                result = _normalize_activity(
                    {"moving_time_s": 600, "weighted_average_watts": 200}, ftp
                )
                self.assertIsNone(result["intensity_factor"])
                self.assertIsNone(result["estimated_tss"])
        for values in (
            {"moving_time_s": 600, "weighted_average_watts": 1e308},
            {"moving_time_s": 1e308, "weighted_average_watts": 1e100},
        ):
            result = _normalize_activity(values, 200)
            self.assertIsNone(result["intensity_factor"])
            self.assertIsNone(result["estimated_tss"])

    def test_local_rebuild_repairs_existing_recording_and_cache_tracks_stream_not_ftp(self):
        from gradient_ascent import power_metrics
        from gradient_ascent.cli import _init_data_dir
        from gradient_ascent.insights import build_insights

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            _init_data_dir(root)
            identifier = "recording-" + "a" * 64
            record = {
                "id": identifier,
                "name": "Synthetic ride",
                "sport_type": "Ride",
                "start_date": "2026-08-17T10:00:00Z",
                "start_date_local": "2026-08-17T10:00:00Z",
                "moving_time": 900,
                "elapsed_time": 900,
                "import_source": "local_recording",
            }
            index_path = root / "recordings" / "activities.json"
            index_path.write_text(json.dumps({identifier: record}))
            stream_path = root / "recordings" / "streams" / f"{identifier}.json"
            stream_path.parent.mkdir(parents=True, exist_ok=True)
            stream_path.write_text(json.dumps(streams(list(range(601)), [200] * 601)))
            profile_path = root / "plan" / "athlete.json"
            profile_path.write_text(json.dumps({"ftp_w": 200}))
            original_index = index_path.read_bytes()

            def rebuild():
                build_insights(root, None, root / "derived")
                return json.loads((root / "derived" / "activities.json").read_text())[0]

            first = rebuild()
            self.assertEqual(first["estimated_tss"], 16.7)
            self.assertEqual(first["weighted_average_watts_source"], "estimated_power_stream")
            self.assertEqual(first["estimated_tss_source"], "estimated_power_stream")
            self.assertEqual(first["power_load_estimate"]["scope"], "recorded_power")
            self.assertAlmostEqual(first["power_load_estimate"]["coverage_ratio"], 2 / 3, places=3)
            profile_path.write_text(json.dumps({"ftp_w": 400}))
            with mock.patch.object(
                power_metrics,
                "estimate_normalized_power",
                wraps=power_metrics.estimate_normalized_power,
            ) as estimate:
                self.assertEqual(rebuild()["estimated_tss"], 4.2)
                estimate.assert_not_called()
                stream_path.write_text(json.dumps(streams(list(range(601)), [100] * 601)))
                self.assertEqual(rebuild()["estimated_tss"], 1.0)
                estimate.assert_called_once()
            self.assertEqual(index_path.read_bytes(), original_index)

    def test_source_metrics_win_and_missing_ftp_or_power_remain_missing(self):
        from gradient_ascent.insights import AggregateTotals, _normalize_activity

        estimate = {
            "estimated_normalized_power_w": 200.0,
            "method": "power_stream_30s_v2",
            "observed_duration_s": 600,
            "rolling_window_duration_s": 571,
            "gap_limit_s": 5,
        }
        base = {
            "sport_type": "Ride",
            "moving_time_s": 900,
            "power_load_estimate": estimate,
            "estimated_normalized_power_w": 200,
        }
        missing = _normalize_activity(base, None)
        self.assertIsNone(missing["estimated_tss"])
        self.assertIsNone(
            _normalize_activity(
                {"sport_type": "Ride", "moving_time": 3600, "average_watts": 200}, 200
            )["estimated_tss"]
        )
        source = _normalize_activity(
            {**base, "weighted_average_watts": 300, "estimated_tss": 77}, 200
        )
        self.assertEqual((source["weighted_average_watts"], source["estimated_tss"]), (300, 77))
        self.assertEqual(source["estimated_tss_source"], "source")
        derived = _normalize_activity(base, 200)
        totals = AggregateTotals()
        totals.add_activity(source)
        totals.add_activity(derived)
        summary = totals.finalize()
        self.assertEqual(summary["estimated_tss_estimated_activity_count"], 1)
        self.assertEqual(summary["estimated_tss_partial_activity_count"], 1)
        for invalid in (float("nan"), float("inf"), -1, True):
            self.assertIsNone(_normalize_activity(base, invalid)["estimated_tss"])
        self.assertTrue(math.isfinite(derived["estimated_tss"]))

    def test_rwgps_source_identity_is_forwarded_without_raw_provider_metadata(self):
        from gradient_ascent.insights import _normalize_activity

        row = {
            "id": "recording:recording-" + "a" * 64,
            "name": "My title",
            "source": {"provider": "recording"},
            "raw": {
                "source_provider": "ridewithgps",
                "source_activity_id": "101",
                "source_provider_name": "Provider title",
                "private_extra": "must not escape",
            },
        }
        result = _normalize_activity(row, None)
        self.assertEqual(result["source_provider"], "ridewithgps")
        self.assertEqual(result["source_activity_id"], "101")
        self.assertTrue(result["name_is_authored"])
        self.assertNotIn("raw", result)
        self.assertNotIn("source_provider_name", result)
        self.assertNotIn("must not escape", json.dumps(result))
        row["raw"]["source_activity_id"] = "../escape"
        self.assertNotIn("source_activity_id", _normalize_activity(row, None))

    def test_stream_symlinks_and_extra_cached_payload_are_never_used(self):
        from gradient_ascent import power_metrics

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_dir = root / "recordings" / "streams"
            source_dir.mkdir(parents=True)
            output = root / "derived"
            output.mkdir()
            identifier = "recording-" + "b" * 64
            activity = {
                "id": "recording:" + identifier,
                "provider_id": identifier,
                "source": {"provider": "recording"},
                "weighted_average_watts": None,
            }
            outside = root / "outside.json"
            outside.write_text(json.dumps(streams(list(range(601)), [200] * 601)))
            path = source_dir / f"{identifier}.json"
            path.symlink_to(outside)
            with mock.patch.object(power_metrics, "_estimate_file") as estimate:
                self.assertEqual(
                    power_metrics.enrich_recording_power(root, [activity], output), [activity]
                )
                estimate.assert_not_called()
            path.unlink()
            path.write_bytes(outside.read_bytes())
            first = power_metrics.enrich_recording_power(root, [activity], output)
            cache_path = output / "power_metrics_cache.json"
            cache = json.loads(cache_path.read_text())
            cache["entries"][identifier]["estimate"]["private_extra"] = "must not escape"
            cache_path.write_text(json.dumps(cache))
            with mock.patch.object(
                power_metrics,
                "estimate_normalized_power",
                wraps=power_metrics.estimate_normalized_power,
            ) as estimate:
                second = power_metrics.enrich_recording_power(root, [activity], output)
                estimate.assert_called_once()
            self.assertEqual(first, second)
            self.assertNotIn("must not escape", json.dumps(second))

    def test_canonical_recording_keeps_supplied_load_without_reading_streams(self):
        from gradient_ascent.canonical import canonical_activity_records
        from gradient_ascent.insights import _normalize_activity
        from gradient_ascent.power_metrics import enrich_recording_power

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "recordings").mkdir()
            identifier = "recording-" + "c" * 64
            record = {
                "id": identifier,
                "sport_type": "Ride",
                "start_date": "2026-08-17T10:00:00Z",
                "moving_time": 3600,
                "weighted_average_watts": 250,
                "estimated_tss": 83,
                "intensity_factor": 0.91,
            }
            (root / "recordings" / "activities.json").write_text(json.dumps({identifier: record}))
            canonical = canonical_activity_records(root)
            with mock.patch("gradient_ascent.power_metrics._estimate_file") as estimate:
                enriched = enrich_recording_power(root, canonical, root / "derived")
                estimate.assert_not_called()
            result = _normalize_activity(enriched[0], 200)
            self.assertEqual((result["estimated_tss"], result["intensity_factor"]), (83, 0.91))
            self.assertEqual(result["weighted_average_watts_source"], "source")
