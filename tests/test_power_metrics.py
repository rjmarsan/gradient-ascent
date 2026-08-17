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
            "method": "power_stream_30s_v3",
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

    def test_missing_load_counts_only_positive_duration_cycling(self):
        from gradient_ascent.insights import AggregateTotals

        totals = AggregateTotals()
        totals.add_activity(
            {
                "sport_type": "Ride",
                "moving_time_s": 3600,
                "estimated_tss": 75,
                "estimated_tss_source": "source",
            }
        )
        for row in (
            {"sport_type": "Walk", "moving_time_s": 0},
            {"sport_type": "Run", "moving_time_s": 1800},
            {"sport_type": "Ride", "moving_time_s": 0},
        ):
            totals.add_activity(row)
        complete = totals.finalize()
        self.assertEqual(complete["estimated_tss"], 75)
        self.assertEqual(complete["estimated_tss_relevant_activity_count"], 1)
        self.assertEqual(complete["estimated_tss_missing_activity_count"], 0)
        totals.add_activity(
            {
                "sport_type": "Run",
                "moving_time_s": 1800,
                "estimated_tss": 12,
                "estimated_tss_source": "source",
            }
        )
        totals.add_activity(
            {
                "sport_type": "Ride",
                "moving_time_s": 0,
                "estimated_tss": 2,
                "estimated_tss_source": "source",
            }
        )
        self.assertEqual(totals.finalize()["estimated_tss"], 89)
        self.assertEqual(totals.finalize()["estimated_tss_missing_activity_count"], 0)
        totals.add_activity({"sport_type": "EBikeRide", "moving_time_s": 900})
        totals.add_activity({"sport_type": "Ride", "elapsed_time_s": 1200})
        incomplete = totals.finalize()
        self.assertEqual(incomplete["estimated_tss_relevant_activity_count"], 3)
        self.assertEqual(incomplete["estimated_tss_missing_activity_count"], 2)
        self.assertEqual(incomplete["estimated_tss"], 89)
        totals.add_activity(
            {
                "sport_type": "EMountainBikeRide",
                "moving_time_s": 600,
                "estimated_tss": 0,
                "estimated_tss_source": "source",
            }
        )
        self.assertEqual(totals.finalize()["estimated_tss_relevant_activity_count"], 4)
        self.assertEqual(totals.finalize()["estimated_tss_missing_activity_count"], 2)

    def test_power_coverage_is_duration_weighted_and_does_not_guess_source_coverage(self):
        from gradient_ascent.insights import AggregateTotals, _normalize_activity

        def recording(observed, moving):
            return _normalize_activity(
                {
                    "sport_type": "Ride",
                    "moving_time_s": moving,
                    "power_load_estimate": {
                        "method": "power_stream_30s_v3",
                        "estimated_normalized_power_w": 200,
                        "observed_duration_s": observed,
                        "rolling_window_duration_s": observed - 29,
                        "gap_limit_s": 5,
                    },
                },
                200,
            )

        totals = AggregateTotals()
        totals.add_activity(recording(600, 900))
        totals.add_activity(recording(2000, 1800))
        source = {**recording(600, 900), "estimated_tss": 75, "estimated_tss_source": "source"}
        totals.add_activity(source)
        result = totals.finalize()
        self.assertEqual(result["estimated_tss_missing_activity_count"], 0)
        self.assertEqual(result["estimated_tss_power_stream_activity_count"], 2)
        self.assertEqual(result["estimated_tss_partial_activity_count"], 1)
        self.assertEqual(result["estimated_tss_relevant_partial_activity_count"], 1)
        self.assertEqual(result["estimated_tss_power_observed_duration_s"], 2600)
        self.assertEqual(result["estimated_tss_power_load_duration_s"], 2400)
        self.assertEqual(result["estimated_tss_power_reported_duration_s"], 2700)
        self.assertAlmostEqual(result["estimated_tss_power_coverage_ratio"], 2400 / 2700, places=6)
        self.assertEqual(source["estimated_tss"], 75)
        totals.add_activity({**recording(600, 900), "sport_type": "Run"})
        with_noncycling = totals.finalize()
        self.assertEqual(with_noncycling["estimated_tss_partial_activity_count"], 2)
        self.assertEqual(with_noncycling["estimated_tss_relevant_partial_activity_count"], 1)
        self.assertEqual(with_noncycling["estimated_tss_power_stream_activity_count"], 2)
        self.assertEqual(
            with_noncycling["estimated_tss_power_coverage_ratio"],
            result["estimated_tss_power_coverage_ratio"],
        )

    def test_completeness_merge_and_empty_coverage_are_stable(self):
        from gradient_ascent.insights import AggregateTotals

        rows = [
            {
                "sport_type": "Ride",
                "moving_time_s": 3600,
                "estimated_tss": 75,
                "estimated_tss_source": "source",
            },
            {
                "sport_type": "Ride",
                "moving_time_s": 900,
                "estimated_tss": 16.7,
                "estimated_tss_source": "estimated_power_stream",
                "power_load_estimate": {
                    "observed_duration_s": 600,
                    "load_duration_s": 600,
                    "coverage_ratio": 1,
                    "scope": "recorded_power",
                },
            },
            {"sport_type": "Ride", "moving_time_s": 600},
            {"sport_type": "Walk", "moving_time_s": 0},
        ]
        all_rows, left, right = AggregateTotals(), AggregateTotals(), AggregateTotals()
        self.assertIsNone(all_rows.finalize()["estimated_tss_power_coverage_ratio"])
        for index, row in enumerate(rows):
            all_rows.add_activity(row)
            (left if index < 2 else right).add_activity(row)
        left.merge(right)
        self.assertEqual(left.finalize(), all_rows.finalize())
        result = left.finalize()
        self.assertEqual(result["activity_count"], 4)
        self.assertEqual(result["estimated_tss_activity_count"], 2)
        self.assertEqual(result["estimated_tss_missing_activity_count"], 1)
        self.assertAlmostEqual(result["estimated_tss_power_coverage_ratio"], 2 / 3, places=6)

    def test_invalid_scores_and_coverage_cannot_make_totals_nonfinite(self):
        from gradient_ascent.insights import AggregateTotals

        totals = AggregateTotals()
        for invalid in (float("nan"), float("inf"), -1, True):
            totals.add_activity(
                {"sport_type": "Ride", "moving_time_s": 600, "estimated_tss": invalid}
            )
        totals.add_activity(
            {
                "sport_type": "Ride",
                "moving_time_s": 900,
                "estimated_tss": 10,
                "estimated_tss_source": "estimated_power_stream",
                "power_load_estimate": {
                    "observed_duration_s": float("inf"),
                    "load_duration_s": 600,
                    "scope": "recorded_power",
                },
            }
        )
        result = totals.finalize()
        self.assertEqual(result["estimated_tss"], 10)
        self.assertEqual(result["estimated_tss_missing_activity_count"], 4)
        self.assertIsNone(result["estimated_tss_power_coverage_ratio"])
        json.dumps(result, allow_nan=False)

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

    def test_legacy_placeholders_are_not_inferred_authored_but_explicit_intent_wins(self):
        from gradient_ascent.insights import _normalize_activity

        for name in (
            "101",
            "123456789",
            "2026-08-17",
            "08/17/26",
            "Ridewithgps 123456789.tcx",
            "Imported Ride",
        ):
            row = {
                "name": name,
                "source": {"provider": "recording"},
                "raw": {
                    "source_provider": "ridewithgps",
                    "source_activity_id": "101",
                    "source_provider_name": "Original provider title",
                },
            }
            with self.subTest(name=name):
                self.assertFalse(_normalize_activity(row, None)["name_is_authored"])
                self.assertTrue(
                    _normalize_activity({**row, "name_is_authored": True}, None)["name_is_authored"]
                )
                self.assertTrue(
                    _normalize_activity(
                        {**row, "raw": {**row["raw"], "name_is_authored": True}}, None
                    )["name_is_authored"]
                )
                self.assertFalse(
                    _normalize_activity({**row, "name_is_authored": "true"}, None)[
                        "name_is_authored"
                    ]
                )
        meaningful = {**row, "name": "My deliberate custom ride title"}
        self.assertTrue(_normalize_activity(meaningful, None)["name_is_authored"])
        unchanged = {**row, "name": "Original provider title"}
        self.assertFalse(_normalize_activity(unchanged, None)["name_is_authored"])

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
