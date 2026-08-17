import io
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from gradient_ascent import activity_files
from gradient_ascent.insights import _normalize_activity
from gradient_ascent.power_metrics import estimate_normalized_power


START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def frame(name, **values):
    return SimpleNamespace(
        frame_type=activity_files.fitdecode.FIT_FRAME_DATA,
        name=name,
        fields=[SimpleNamespace(name=key, value=value) for key, value in values.items()],
    )


def parse_frames(frames):
    reader = mock.MagicMock()
    reader.__enter__.return_value = frames
    with mock.patch.object(activity_files.fitdecode, "FitReader", return_value=reader):
        return activity_files.parse_activity_recording(io.BytesIO(), "synthetic.fit")


class RecordedLoadAccuracyTest(unittest.TestCase):
    def test_unrelated_missing_heart_rate_never_discards_power(self):
        points = [
            {
                "timestamp": START + timedelta(seconds=i),
                "watts": 100 if i % 2 == 0 else 300,
                "heartrate": 140 if i % 2 == 0 else None,
            }
            for i in range(1201)
        ]
        payload = activity_files._stream_payload(points, "tcx")
        streams = {item["type"]: item["data"] for item in payload["streams"]}
        self.assertEqual(len(streams["watts"]), 1201)
        self.assertIsNone(streams["heartrate"][1])
        self.assertEqual(
            estimate_normalized_power(streams["time"], streams["watts"])[
                "estimated_normalized_power_w"
            ],
            200,
        )

    def test_lap_np_is_not_promoted_to_whole_activity_source_np(self):
        points = [{"timestamp": START + timedelta(seconds=i), "watts": 200} for i in range(1201)]
        summary = activity_files._recording_summary(
            points,
            [
                {"weighted_average_watts": 100, "moving_time": 600},
                {"weighted_average_watts": 300, "moving_time": 600},
            ],
        )
        self.assertNotIn("weighted_average_watts", summary)

    def test_complementary_same_time_records_merge_without_double_counting(self):
        points = []
        for second in range(601):
            timestamp = START + timedelta(seconds=second)
            points.extend(
                [{"timestamp": timestamp, "watts": 200}, {"timestamp": timestamp, "heartrate": 140}]
            )
        streams = {
            item["type"]: item["data"]
            for item in activity_files._stream_payload(points, "fit")["streams"]
        }
        self.assertEqual(len(streams["time"]), 601)
        self.assertEqual(
            estimate_normalized_power(streams["time"], streams["watts"])["observed_duration_s"], 600
        )
        points.insert(1, {"timestamp": START, "watts": 300})
        conflict = {
            item["type"]: item["data"]
            for item in activity_files._stream_payload(points, "fit")["streams"]
        }
        self.assertIsNone(estimate_normalized_power(conflict["time"], conflict["watts"]))

    def test_single_fit_session_metrics_are_preserved_and_win(self):
        parsed = parse_frames(
            [
                frame("record", timestamp=START, power=200, speed=0),
                frame("record", timestamp=START + timedelta(seconds=3600), power=200, speed=0),
                frame("lap", normalized_power=100, total_timer_time=3600),
                frame(
                    "session",
                    normalized_power=225,
                    training_stress_score=91.5,
                    intensity_factor=0.9,
                    total_timer_time=3600,
                    total_elapsed_time=4000,
                ),
            ]
        )
        summary = parsed["summary"]
        self.assertEqual(summary["weighted_average_watts"], 225)
        self.assertEqual(summary["estimated_tss"], 91.5)
        self.assertEqual(summary["intensity_factor"], 0.9)
        self.assertEqual(summary["timer_time"], 3600)
        self.assertEqual(_normalize_activity(summary, 300)["estimated_tss"], 91.5)
        without_tss = {
            key: value
            for key, value in summary.items()
            if key not in {"estimated_tss", "intensity_factor"}
        }
        self.assertEqual(_normalize_activity(without_tss, 225)["estimated_tss"], 100)

    def test_multiple_fit_sessions_do_not_guess_a_combined_source_np(self):
        parsed = parse_frames(
            [
                frame("record", timestamp=START, power=200),
                frame("record", timestamp=START + timedelta(seconds=1200), power=200),
                frame("session", normalized_power=100, training_stress_score=10),
                frame("session", normalized_power=300, training_stress_score=30),
            ]
        )
        self.assertNotIn("weighted_average_watts", parsed["summary"])
        self.assertNotIn("estimated_tss", parsed["summary"])

    def test_existing_provider_tss_and_if_survive_canonical_conversion(self):
        from gradient_ascent.canonical import strava_raw_to_activity

        record = strava_raw_to_activity(
            {
                "id": 123,
                "moving_time": 3600,
                "weighted_average_watts": 200,
                "estimated_tss": 83.5,
                "intensity_factor": 0.91,
            }
        )
        normalized = _normalize_activity(record, 300)
        self.assertEqual(normalized["estimated_tss"], 83.5)
        self.assertEqual(normalized["intensity_factor"], 0.91)

    def test_explicit_timer_pauses_exclude_only_proven_stopped_intervals(self):
        times = list(range(7201))
        watts = [200] * 1800 + [0] * 3600 + [200] * 1801
        timer = [True] * 1800 + [False] * 3600 + [True] * 1801
        result = estimate_normalized_power(times, watts, timer_active=timer)
        self.assertAlmostEqual(result["estimated_normalized_power_w"], 200)
        self.assertGreaterEqual(result["observed_duration_s"], 3598)
        self.assertLessEqual(result["observed_duration_s"], 3600)
        self.assertEqual(
            estimate_normalized_power(times, [0] * len(times))["estimated_normalized_power_w"], 0
        )
        self.assertIsNone(estimate_normalized_power(times, watts, timer_active=[True]))

    def test_fit_timer_events_are_preserved_without_speed_guessing(self):
        parsed = parse_frames(
            [
                frame("event", timestamp=START, event="timer", event_type="start"),
                frame("record", timestamp=START, power=200, speed=0),
                frame(
                    "event",
                    timestamp=START + timedelta(seconds=10),
                    event="timer",
                    event_type="stop_all",
                ),
                frame("record", timestamp=START + timedelta(seconds=11), power=0, speed=0),
                frame(
                    "event",
                    timestamp=START + timedelta(seconds=20),
                    event="timer",
                    event_type="start",
                ),
                frame("record", timestamp=START + timedelta(seconds=21), power=200, speed=0),
            ]
        )
        streams = {item["type"]: item["data"] for item in parsed["streams"]["streams"]}
        self.assertEqual(streams["timer_active"], [True, False, True])

    def test_timer_based_stream_load_has_matching_aggregate_coverage(self):
        from gradient_ascent.insights import AggregateTotals

        estimate = estimate_normalized_power(list(range(601)), [200] * 601)
        activity = _normalize_activity(
            {
                "sport_type": "VirtualRide",
                "moving_time_s": 0,
                "elapsed_time_s": 1200,
                "timer_time_s": 900,
                "power_load_estimate": estimate,
            },
            200,
        )
        totals = AggregateTotals()
        totals.add_activity(activity)
        self.assertEqual(activity["power_load_estimate"]["reported_duration_source"], "timer_time")
        self.assertAlmostEqual(
            totals.finalize()["estimated_tss_power_coverage_ratio"], 2 / 3, places=3
        )


if __name__ == "__main__":
    unittest.main()
