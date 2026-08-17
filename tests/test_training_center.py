import json
import hashlib
import os
import shutil
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from threading import Event, Lock
from unittest.mock import patch

from gradient_ascent import training_center as training_center_module
from gradient_ascent.cli import _init_workspace
from gradient_ascent.insights import build_insights
from gradient_ascent.onboarding import add_onboarding_event, set_onboarding_goals
from gradient_ascent.recordings import import_activity_recording
from gradient_ascent.refresh import refresh_workspace
from gradient_ascent.training_center import (
    _activity_lap_details,
    _activity_stream_shape,
    _minute_bucket_stream_values,
    _planned_load_for_day,
    _status_label,
    _week_display_status,
    build_training_center,
)


def _training_center_payload(data_js: str) -> dict:
    prefix = "window.__COACH_TRAINING_CENTER_DATA__ = "
    return json.loads(data_js.removeprefix(prefix).removesuffix(";\n"))


class TrainingCenterTest(unittest.TestCase):
    def test_ride_setup_is_clickable_and_uses_the_guarded_local_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            _init_workspace(workspace, force=False)
            build_insights(workspace, None, workspace / "derived")
            result = build_training_center(workspace)
            html = Path(result["html"]).read_text(encoding="utf-8")

        self.assertIn('const RIDE_SETUP_API = "./api/connections/ridewithgps/setup"', html)
        self.assertIn("Install and connect", html)
        self.assertIn("Open Ride with GPS sign-in", html)
        self.assertIn("Import older rides", html)
        self.assertIn("Stop syncing", html)
        self.assertIn("async function rideSetupAction", html)
        self.assertIn("headers: apiHeaders", html)
        self.assertNotIn("window.open(state.rideSetup", html)

    def test_ridewithgps_activity_link_is_reconstructed_from_trusted_source_id(self) -> None:
        activity = {
            "id": "recording:recording-synthetic",
            "provider_id": "recording-synthetic",
            "name": "A synthetic ride",
            "source": {"provider": "recording"},
            "raw": {"source_provider": "ridewithgps", "source_activity_id": "123", "source_url": "javascript:bad"},
        }
        detail = training_center_module._activity_detail(activity, {}, Path("/tmp"), include_heavy=False)
        self.assertEqual(detail["source_label"], "Ride with GPS")
        self.assertEqual(detail["source_url"], "https://ridewithgps.com/trips/123")
        self.assertIsNone(detail["strava_url"])
        activity["raw"]["source_activity_id"] = "../unsafe"
        detail = training_center_module._activity_detail(activity, {}, Path("/tmp"), include_heavy=False)
        self.assertIsNone(detail["source_url"])

    def test_calendar_renders_one_selectable_year_instead_of_all_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "workspace"
            _init_workspace(data_dir, force=False)
            build_insights(data_dir, None, data_dir / "derived")

            result = build_training_center(data_dir)
            html = Path(result["html"]).read_text(encoding="utf-8")

            self.assertIn('id="calendar-year-select"', html)
            self.assertIn("day.date.slice(0, 4) === state.calendarYear", html)
            self.assertIn("state.calendarYear = state.selectedDate.slice(0, 4)", html)

    def test_blank_workspace_builds_a_self_contained_local_dashboard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            _init_workspace(workspace, force=False)
            build_insights(workspace, None, workspace / "derived")

            result = build_training_center(workspace)
            html = Path(result["html"]).read_text(encoding="utf-8")
            data_js = Path(result["data_js"]).read_text(encoding="utf-8")

        self.assertIn("Gradient Ascent Training Center", html)
        self.assertIn("GRADIENT ASCENT", html)
        self.assertIn('id="connections-view"', html)
        self.assertIn('id="recording-drop-overlay"', html)
        self.assertIn('id="activity-recording-input"', html)
        self.assertIn('accept=".fit,.tcx,.gpx"', html)
        self.assertIn('window.addEventListener("dragenter"', html)
        self.assertIn('const ACTIVITY_RECORDINGS_API = "./api/activity-recordings"', html)
        self.assertIn("void loadRuntimeState();", html)
        self.assertNotIn("await loadNotes();", html)
        self.assertNotIn("cdn.plot.ly", html)
        self.assertNotIn("open-graphs", html)
        self.assertNotIn("metrics-dashboard", html)
        removed_view = "met" + "rics"
        self.assertNotIn(f'data-view="{removed_view}"', html)
        for old_version in range(2, 6):
            self.assertNotIn(f"/* V{old_version} ", html)
        self.assertNotIn("COACH / CODEX", html)
        self.assertIn("window.__COACH_TRAINING_CENTER_DATA__", data_js)
        self.assertNotIn("metrics_css", result)

    def test_dashboard_always_offers_a_private_workspace_coaching_conversation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "private athlete workspace"
            _init_workspace(workspace, force=False)
            build_insights(workspace, None, workspace / "derived")

            result = build_training_center(workspace)
            html = Path(result["html"]).read_text(encoding="utf-8")
            payload = _training_center_payload(
                Path(result["data_js"]).read_text(encoding="utf-8")
            )

        self.assertIn('id="ask-coach-button"', html)
        self.assertIn('aria-label="Start a coaching conversation in Codex"', html)
        self.assertIn("Ask Coach</a>", html)
        self.assertIn('document.getElementById("ask-coach-button")', html)
        self.assertIn("codexThreadUrl(COACH_CONVERSATION_PROMPT)", html)
        self.assertIn("Use $coach-advice to review my training", html)
        self.assertIn('query.set("path", DATA.workspacePath)', html)
        self.assertEqual(payload["workspacePath"], str(workspace.resolve()))

    def test_no_plan_dashboard_keeps_structured_goal_and_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            _init_workspace(workspace, force=False)
            set_onboarding_goals(
                workspace,
                north_star="Finish my first century feeling in control",
                goal="Complete the century without a late-ride fade",
                why="It is my main event this season.",
                success="Finish safely using practiced pacing and fueling.",
                coaching_implication="Prioritize durability and fueling practice.",
                evidence="Long rides, pacing stability, and fueling tolerance.",
            )
            add_onboarding_event(
                workspace,
                name="Community Century",
                event_date="2026-09-12",
                discipline="road",
                priority="A",
                location="Madison WI",
            )

            result = build_training_center(workspace)
            data_js = Path(result["data_js"]).read_text(encoding="utf-8")
            progress = (workspace / "derived" / "progress.html").read_text(encoding="utf-8")

        self.assertIn('"name":"Community Century"', data_js)
        self.assertIn("Finish my first century feeling in control", progress)
        self.assertIn("Complete the century without a late-ride fade", progress)

    def test_recovery_panel_uses_canonical_source_without_provider_specific_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            _init_workspace(workspace, force=False)
            (workspace / "garmin" / "2026-05-01.json").write_text(
                json.dumps(
                    {
                        "heartrate": {"restingHeartRate": 48},
                        "stress": {"avgStressLevel": 21},
                        "sleep": {"dailySleepDTO": {"sleepTimeSeconds": 27000}},
                    }
                ),
                encoding="utf-8",
            )
            build_insights(workspace, None, workspace / "derived")

            result = build_training_center(workspace)
            payload_text = Path(result["data_js"]).read_text(encoding="utf-8")

        self.assertIn('"status_label":"Garmin Connect recovery for this date"', payload_text)
        self.assertIn('"resting_hr":48.0', payload_text)
        self.assertIn('"sleep_duration_s":27000.0', payload_text)

    def test_apple_health_only_workout_is_visible_without_a_strava_link(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            _init_workspace(workspace, force=False)
            (workspace / "apple_health" / "workouts.json").write_text(
                json.dumps(
                    [
                        {
                            "id": "apple-ride-1",
                            "workout_type": "Cycling",
                            "start_date": "2026-05-02 09:00:00 -0700",
                            "duration_s": 3600,
                            "distance_m": 25000,
                            "energy_kj": 500,
                        }
                    ]
                ),
                encoding="utf-8",
            )
            build_insights(workspace, None, workspace / "derived")

            result = build_training_center(workspace)
            payload_text = Path(result["data_js"]).read_text(encoding="utf-8")

        self.assertIn('"id":"apple_health:apple-ride-1"', payload_text)
        self.assertIn('"name":"Cycling"', payload_text)
        self.assertIn('"strava_url":null', payload_text)

    def test_local_recording_renders_ride_detail_without_a_strava_link(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            _init_workspace(workspace, force=False)
            activity_id = "recording-test-ride"
            (workspace / "recordings" / "streams").mkdir(parents=True, exist_ok=True)
            (workspace / "recordings" / "activities.json").write_text(
                json.dumps(
                    {
                        activity_id: {
                            "id": activity_id,
                            "name": "Dropped Ride",
                            "sport_type": "Ride",
                            "start_date": "2026-05-02T08:00:00Z",
                            "start_date_local": "2026-05-02T08:00:00Z",
                            "moving_time": 360,
                            "elapsed_time": 360,
                            "distance": 3000,
                            "average_watts": 205,
                        }
                    }
                ),
                encoding="utf-8",
            )
            samples = list(range(12))
            (workspace / "recordings" / "streams" / f"{activity_id}.json").write_text(
                json.dumps(
                    {
                        "streams": [
                            {"type": "time", "data": [value * 30 for value in samples]},
                            {"type": "moving", "data": [True] * len(samples)},
                            {"type": "watts", "data": [200 + value for value in samples]},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            build_insights(workspace, None, workspace / "derived")

            result = build_training_center(workspace)
            payload_text = Path(result["data_js"]).read_text(encoding="utf-8")
            payload = _training_center_payload(payload_text)
            detail_files = sorted(Path(result["activity_details_dir"]).glob("*.js"))
            detail_text = "\n".join(path.read_text(encoding="utf-8") for path in detail_files)
            html = Path(result["html"]).read_text(encoding="utf-8")

        self.assertIn(f'"id":"recording:{activity_id}"', payload_text)
        self.assertIn('"name":"Dropped Ride"', payload_text)
        self.assertIn('"strava_url":null', payload_text)
        self.assertTrue(payload["weeks"])
        self.assertTrue(all("days" not in week for week in payload["weeks"]))
        self.assertNotIn('"stream_shape"', payload_text)
        self.assertNotIn('"laps":', payload_text)
        self.assertEqual(len(detail_files), 1)
        self.assertIn('"source":"watts"', detail_text)
        self.assertIn('"stream_shape"', detail_text)
        self.assertIn("loadWeekActivityDetails", html)
        self.assertIn("DAYS_BY_WEEK", html)

    def test_activity_detail_sidecars_reuse_unchanged_weeks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            _init_workspace(workspace, force=False)
            activity_id = "recording-cache-test"
            streams_dir = workspace / "recordings" / "streams"
            laps_dir = workspace / "recordings" / "laps"
            streams_dir.mkdir(parents=True, exist_ok=True)
            laps_dir.mkdir(parents=True, exist_ok=True)
            (workspace / "recordings" / "activities.json").write_text(
                json.dumps(
                    {
                        activity_id: {
                            "id": activity_id,
                            "name": "Cache Test Ride",
                            "sport_type": "Ride",
                            "start_date": "2026-05-02T08:00:00Z",
                            "start_date_local": "2026-05-02T08:00:00Z",
                            "moving_time": 360,
                            "elapsed_time": 360,
                            "distance": 3000,
                            "average_watts": 205,
                        }
                    }
                ),
                encoding="utf-8",
            )
            samples = list(range(12))
            stream_path = streams_dir / f"{activity_id}.json"
            lap_path = laps_dir / f"{activity_id}.json"

            def write_stream(offset: int) -> None:
                stream_path.write_text(
                    json.dumps(
                        {
                            "streams": [
                                {"type": "time", "data": [value * 30 for value in samples]},
                                {"type": "moving", "data": [True] * len(samples)},
                                {"type": "watts", "data": [offset + value for value in samples]},
                            ]
                        }
                    ),
                    encoding="utf-8",
                )

            write_stream(200)
            lap_path.write_text(
                json.dumps(
                    {
                        "laps": [
                            {"lap_index": 1, "moving_time": 180, "average_watts": 200},
                            {"lap_index": 2, "moving_time": 180, "average_watts": 210},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            build_insights(workspace, None, workspace / "derived")

            with (
                patch(
                    "gradient_ascent.training_center._activity_stream_shape",
                    wraps=_activity_stream_shape,
                ) as stream_shape,
                patch(
                    "gradient_ascent.training_center._activity_lap_details",
                    wraps=_activity_lap_details,
                ) as lap_details,
            ):
                cold = build_training_center(workspace)
                cold_payload = _training_center_payload(
                    Path(cold["data_js"]).read_text(encoding="utf-8")
                )
                cold_week = next(
                    week
                    for week in cold_payload["weeks"]
                    if week.get("activity_details_file")
                )
                cold_filename = cold_week["activity_details_file"]
                sidecar = Path(cold["activity_details_dir"]) / cold_filename
                cold_text = sidecar.read_text(encoding="utf-8")
                self.assertEqual(
                    cold_filename,
                    f'{cold_week["start_date"]}.{cold_week["activity_details_key"]}.js',
                )
                self.assertIn(
                    f'"cache_key":"{cold_week["activity_details_key"]}"',
                    cold_text,
                )
                self.assertEqual(stream_shape.call_count, 1)
                self.assertEqual(lap_details.call_count, 1)
                self.assertEqual(cold["activity_detail_files_rebuilt"], 1)
                self.assertEqual(cold["activity_detail_files_reused"], 0)
                cold_activity = next(
                    activity
                    for day in cold_payload["days"]
                    for activity in day["activities"]
                )
                self.assertEqual(cold_activity["lap_count"], 2)
                self.assertEqual(cold_activity["lap_count_label"], "2 laps")

                stale_sidecar = sidecar.parent / "1999-01-04.js"
                stale_sidecar.write_text("stale", encoding="utf-8")
                warm = build_training_center(workspace)
                self.assertEqual(stream_shape.call_count, 1)
                self.assertEqual(lap_details.call_count, 1)
                self.assertEqual(warm["activity_detail_files_rebuilt"], 0)
                self.assertEqual(warm["activity_detail_files_reused"], 1)
                self.assertEqual(sidecar.read_text(encoding="utf-8"), cold_text)
                self.assertFalse(stale_sidecar.exists())
                warm_payload = _training_center_payload(
                    Path(warm["data_js"]).read_text(encoding="utf-8")
                )
                warm_activity = next(
                    activity
                    for day in warm_payload["days"]
                    for activity in day["activities"]
                )
                self.assertEqual(warm_activity["lap_count"], 2)

                write_stream(300)
                changed = build_training_center(workspace)
                changed_payload = _training_center_payload(
                    Path(changed["data_js"]).read_text(encoding="utf-8")
                )
                changed_filename = next(
                    week["activity_details_file"]
                    for week in changed_payload["weeks"]
                    if week.get("activity_details_file")
                )
                changed_sidecar = Path(changed["activity_details_dir"]) / changed_filename
                changed_text = changed_sidecar.read_text(encoding="utf-8")
                self.assertEqual(stream_shape.call_count, 2)
                self.assertEqual(lap_details.call_count, 2)
                self.assertEqual(changed["activity_detail_files_rebuilt"], 1)
                self.assertEqual(changed["activity_detail_files_reused"], 0)
                self.assertNotEqual(changed_text, cold_text)
                self.assertIn('"values":[300.5', changed_text)

                (workspace / "plan" / "dashboard_labels.json").write_text(
                    json.dumps(
                        {
                            "version": 1,
                            "days": {},
                            "rides": {
                                f"recording:{activity_id}": {"reaction": "strong"}
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                annotated = build_training_center(workspace)
                annotated_payload = _training_center_payload(
                    Path(annotated["data_js"]).read_text(encoding="utf-8")
                )
                annotated_filename = next(
                    week["activity_details_file"]
                    for week in annotated_payload["weeks"]
                    if week.get("activity_details_file")
                )
                self.assertEqual(stream_shape.call_count, 3)
                self.assertEqual(lap_details.call_count, 3)
                self.assertEqual(annotated["activity_detail_files_rebuilt"], 1)
                self.assertIn(
                    '"reaction":"strong"',
                    (
                        Path(annotated["activity_details_dir"])
                        / annotated_filename
                    ).read_text(encoding="utf-8"),
                )

                lap_path.write_text(
                    json.dumps(
                        {
                            "laps": [
                                {"lap_index": index, "moving_time": 120}
                                for index in range(1, 4)
                            ]
                        }
                    ),
                    encoding="utf-8",
                )
                changed_laps = build_training_center(workspace)
                changed_laps_payload = _training_center_payload(
                    Path(changed_laps["data_js"]).read_text(encoding="utf-8")
                )
                changed_laps_activity = next(
                    activity
                    for day in changed_laps_payload["days"]
                    for activity in day["activities"]
                )
                self.assertEqual(stream_shape.call_count, 4)
                self.assertEqual(lap_details.call_count, 4)
                self.assertEqual(changed_laps["activity_detail_files_rebuilt"], 1)
                self.assertEqual(changed_laps_activity["lap_count"], 3)

                changed_laps_filename = next(
                    week["activity_details_file"]
                    for week in changed_laps_payload["weeks"]
                    if week.get("activity_details_file")
                )
                changed_laps_sidecar = (
                    Path(changed_laps["activity_details_dir"])
                    / changed_laps_filename
                )
                original_text = changed_laps_sidecar.read_text(encoding="utf-8")
                original_stat = changed_laps_sidecar.stat()
                tampered_text = original_text.replace(
                    "Cache Test Ride",
                    "Cache Xest Ride",
                    1,
                )
                self.assertEqual(len(tampered_text), len(original_text))
                changed_laps_sidecar.write_text(tampered_text, encoding="utf-8")
                os.utime(
                    changed_laps_sidecar,
                    ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
                )

                repaired = build_training_center(workspace)
                self.assertEqual(stream_shape.call_count, 5)
                self.assertEqual(lap_details.call_count, 5)
                self.assertEqual(repaired["activity_detail_files_rebuilt"], 1)
                self.assertEqual(
                    changed_laps_sidecar.read_text(encoding="utf-8"),
                    original_text,
                )

            manifest = json.loads(
                (
                    workspace
                    / "derived"
                    / ".cache"
                    / "training_center_activity_details.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["version"], training_center_module.ACTIVITY_DETAILS_CACHE_VERSION)
            self.assertEqual(len(manifest["weeks"]), 1)
            self.assertNotIn("lap_counts", next(iter(manifest["weeks"].values())))
            with patch(
                "gradient_ascent.training_center.ACTIVITY_DETAILS_CACHE_VERSION",
                training_center_module.ACTIVITY_DETAILS_CACHE_VERSION + 1,
            ):
                versioned = build_training_center(workspace)
                self.assertTrue(changed_laps_sidecar.exists())
                versioned_warm = build_training_center(workspace)
            self.assertEqual(versioned["activity_detail_files_rebuilt"], 1)
            self.assertEqual(versioned["activity_detail_files_reused"], 0)
            self.assertEqual(versioned_warm["activity_detail_files_rebuilt"], 0)
            self.assertEqual(versioned_warm["activity_detail_files_reused"], 1)
            self.assertFalse(changed_laps_sidecar.exists())

            (workspace / "derived" / "activities.json").write_text(
                "[]\n",
                encoding="utf-8",
            )
            retained_before_empty_build = set(
                Path(versioned_warm["activity_details_dir"]).glob("*.js")
            )
            self.assertTrue(retained_before_empty_build)
            empty_transition = build_training_center(workspace)
            self.assertEqual(empty_transition["activity_detail_files"], 0)
            self.assertTrue(
                retained_before_empty_build
                <= set(Path(empty_transition["activity_details_dir"]).glob("*.js"))
            )
            empty_warm = build_training_center(workspace)
            self.assertEqual(empty_warm["activity_detail_files"], 0)
            self.assertEqual(
                list(Path(empty_warm["activity_details_dir"]).glob("*.js")),
                [],
            )

    def test_refresh_migrates_read_only_legacy_cache_without_rebuilding_sidecars(self) -> None:
        recording = """<?xml version="1.0" encoding="UTF-8"?>
<gpx xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>
<trkpt lat="37.0" lon="-122.0"><time>2026-05-02T08:00:00Z</time></trkpt>
<trkpt lat="37.001" lon="-122.001"><time>2026-05-02T08:01:00Z</time></trkpt>
</trkseg></trk></gpx>"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            _init_workspace(workspace, force=False)
            source = root / "synthetic.gpx"
            source.write_text(recording, encoding="utf-8")
            import_activity_recording(workspace, source)
            cold = refresh_workspace(workspace)
            self.assertEqual(cold["training_center"]["activity_detail_files_rebuilt"], 1)

            legacy_cache = workspace / ".codex" / "cache"
            legacy_manifest = legacy_cache / "training_center_activity_details.json"
            current_manifest = (
                workspace / "derived" / ".cache" / "training_center_activity_details.json"
            )
            if current_manifest.exists():
                legacy_cache.mkdir(parents=True, exist_ok=True)
                current_manifest.replace(legacy_manifest)
            legacy_bytes = legacy_manifest.read_bytes()
            generation_marker = legacy_cache / "workspace-generation"
            generation_bytes = (
                generation_marker.read_bytes() if generation_marker.exists() else None
            )
            gitignore = workspace / ".gitignore"
            gitignore.write_text(
                gitignore.read_text(encoding="utf-8").replace("derived/.cache/\n", ""),
                encoding="utf-8",
            )
            self.assertNotIn("derived/.cache/", gitignore.read_text(encoding="utf-8"))
            legacy_cache.chmod(0o500)
            resolved_legacy_cache = legacy_cache.resolve()
            original_chmod = Path.chmod
            original_write_json = training_center_module.write_json

            def protected_codex_cache(path: Path, mode: int, *args, **kwargs):
                if path in (legacy_cache, resolved_legacy_cache):
                    raise PermissionError("Codex sandbox protects workspace .codex directories")
                return original_chmod(path, mode, *args, **kwargs)

            def require_private_cache_before_write(path: Path, payload: object) -> None:
                if path == current_manifest:
                    self.assertIn("derived/.cache/", gitignore.read_text(encoding="utf-8"))
                original_write_json(path, payload)

            try:
                with (
                    patch.object(Path, "chmod", protected_codex_cache),
                    patch.object(
                        training_center_module,
                        "write_json",
                        require_private_cache_before_write,
                    ),
                ):
                    warm = refresh_workspace(workspace)
            finally:
                legacy_cache.chmod(0o700)

            self.assertEqual(warm["training_center"]["activity_detail_files_rebuilt"], 0)
            self.assertEqual(warm["training_center"]["activity_detail_files_reused"], 1)
            self.assertTrue(current_manifest.is_file())
            self.assertEqual(legacy_manifest.read_bytes(), legacy_bytes)
            self.assertIn("derived/.cache/", gitignore.read_text(encoding="utf-8"))
            if generation_bytes is not None:
                self.assertEqual(generation_marker.read_bytes(), generation_bytes)

    def test_activity_detail_manifest_refuses_final_component_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "outside.json"
            outside.write_text(
                json.dumps({"version": 1, "weeks": {}}),
                encoding="utf-8",
            )
            manifest = root / "manifest.json"
            try:
                manifest.symlink_to(outside)
            except OSError:
                self.skipTest("Symlinks are unavailable on this filesystem")

            self.assertIsNone(training_center_module._load_activity_detail_manifest(manifest))

    def test_activity_detail_files_cannot_escape_provider_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            streams_dir = workspace / "recordings" / "streams"
            laps_dir = workspace / "recordings" / "laps"
            streams_dir.mkdir(parents=True)
            laps_dir.mkdir(parents=True)
            (workspace / "secret.json").write_text(
                json.dumps(
                    {
                        "streams": [
                            {"type": "watts", "data": [200] * 12},
                            {"type": "moving", "data": [True] * 12},
                        ],
                        "laps": [{"average_watts": 999}],
                    }
                ),
                encoding="utf-8",
            )

            self.assertIsNone(
                _activity_stream_shape(workspace, "../../secret", "recording")
            )
            self.assertEqual(
                _activity_lap_details(workspace, "../../secret", "recording"),
                [],
            )

            symlink_path = streams_dir / "linked.json"
            try:
                symlink_path.symlink_to(workspace / "secret.json")
            except OSError:
                self.skipTest("Symlinks are unavailable on this filesystem")
            self.assertIsNone(
                _activity_stream_shape(workspace, "linked", "recording")
            )

            anchored_id = "anchored"
            (streams_dir / f"{anchored_id}.json").write_text(
                json.dumps({"streams": [{"type": "watts", "data": [200]}]}),
                encoding="utf-8",
            )
            outside_streams = Path(tmp) / "outside-streams"
            outside_streams.mkdir()
            (outside_streams / f"{anchored_id}.json").write_text(
                json.dumps({"streams": [{"type": "watts", "data": [999]}]}),
                encoding="utf-8",
            )
            with training_center_module._activity_detail_roots(workspace) as roots:
                detail_root = roots.get(("recording", "streams"))
                if detail_root is None or detail_root.descriptor is None:
                    self.skipTest("Anchored no-follow file access is unavailable")
                anchored_streams = workspace / "recordings" / "anchored-streams"
                streams_dir.rename(anchored_streams)
                streams_dir.symlink_to(outside_streams, target_is_directory=True)
                payload = training_center_module._read_activity_detail_artifact(
                    workspace,
                    anchored_id,
                    "recording",
                    "streams",
                    {},
                    detail_roots=roots,
                )
            self.assertEqual(payload["streams"][0]["data"], [200])

    def test_activity_detail_reads_fail_closed_without_nofollow_support(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            _init_workspace(workspace, force=False)
            stream_path = workspace / "recordings" / "streams" / "safe.json"
            stream_path.write_text(
                json.dumps({"streams": [{"type": "watts", "data": [200]}]}),
                encoding="utf-8",
            )
            (workspace / "recordings" / "activities.json").write_text(
                json.dumps(
                    {
                        "safe": {
                            "id": "safe",
                            "name": "Safe local ride",
                            "sport_type": "Ride",
                            "start_date": "2026-05-05T08:00:00Z",
                            "start_date_local": "2026-05-05T08:00:00Z",
                            "moving_time": 3600,
                            "distance": 25000,
                            "average_watts": 200,
                        }
                    }
                ),
                encoding="utf-8",
            )
            build_insights(workspace, None, workspace / "derived")

            with (
                patch.object(training_center_module.os, "supports_dir_fd", set()),
                patch.object(
                    training_center_module,
                    "_nofollow_read_flags",
                    return_value=None,
                ),
            ):
                self.assertIsNone(
                    training_center_module._activity_stream_shape(
                        workspace,
                        "safe",
                        "recording",
                    )
                )
                self.assertIsNone(
                    training_center_module._read_regular_file_bytes(stream_path)
                )
                first = build_training_center(workspace)
                second = build_training_center(workspace)

            self.assertEqual(first["activity_detail_files_rebuilt"], 1)
            self.assertEqual(first["activity_detail_files_reused"], 0)
            self.assertEqual(second["activity_detail_files_rebuilt"], 1)
            self.assertEqual(second["activity_detail_files_reused"], 0)
            self.assertTrue(Path(second["html"]).is_file())

    def test_activity_detail_cache_rejects_symlinks_and_malformed_payloads(self) -> None:
        malformed = (
            "window.__COACH_TRAINING_CENTER_ACTIVITY_DETAILS__ = "
            "window.__COACH_TRAINING_CENTER_ACTIVITY_DETAILS__ || {};\n"
            'window.__COACH_TRAINING_CENTER_ACTIVITY_DETAILS__["2026-05-04"] = '
            '{"cache_key":"fingerprint","days":{"2026-05-04":["bad"]}};\n'
        ).encode("utf-8")
        self.assertIsNone(
            training_center_module._cached_activity_detail_lap_counts(
                malformed,
                week_start="2026-05-04",
                fingerprint="fingerprint",
            )
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "outside.js"
            target.write_bytes(malformed)
            sidecar = root / "2026-05-04.fingerprint.js"
            try:
                sidecar.symlink_to(target)
            except OSError:
                self.skipTest("Symlinks are unavailable on this filesystem")
            entry = {
                "fingerprint": "fingerprint",
                "file": sidecar.name,
                "size": target.stat().st_size,
                "sha256": hashlib.sha256(malformed).hexdigest(),
            }
            with patch.object(
                Path,
                "read_bytes",
                side_effect=AssertionError("symlink target must not be read"),
            ):
                self.assertIsNone(
                    training_center_module._cached_activity_detail_matches(
                        sidecar,
                        entry,
                        week_start="2026-05-04",
                        fingerprint="fingerprint",
                        filename=sidecar.name,
                    )
                )

    def test_training_center_refuses_symlinked_private_output_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            outside = Path(tmp) / "outside"
            _init_workspace(workspace, force=False)
            outside.mkdir()
            details_dir = workspace / "derived" / "training_center_activity_details"
            try:
                details_dir.symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("Symlinks are unavailable on this filesystem")

            sentinel = outside / "keep.js"
            sentinel.write_text("keep", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "cannot be a symlink"):
                build_training_center(workspace)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

            cache_workspace = Path(tmp) / "cache-workspace"
            _init_workspace(cache_workspace, force=False)
            cache_dir = cache_workspace / ".codex" / "cache"
            shutil.rmtree(cache_dir)
            cache_dir.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex((OSError, ValueError), "cannot be a symlink"):
                build_training_center(cache_workspace)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

            derived_workspace = Path(tmp) / "derived-cache-workspace"
            _init_workspace(derived_workspace, force=False)
            derived_cache = derived_workspace / "derived" / ".cache"
            derived_cache.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "cannot be a symlink"):
                build_training_center(derived_workspace)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_concurrent_training_center_builds_are_serialized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            _init_workspace(workspace, force=False)
            build_insights(workspace, None, workspace / "derived")

            original_build = training_center_module._build_training_center_unlocked
            guard = Lock()
            active = 0
            max_active = 0

            def instrumented_build(data_dir: Path) -> dict:
                nonlocal active, max_active
                with guard:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.05)
                try:
                    return original_build(data_dir)
                finally:
                    with guard:
                        active -= 1

            with patch.object(
                training_center_module,
                "_build_training_center_unlocked",
                side_effect=instrumented_build,
            ):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    results = list(
                        executor.map(
                            build_training_center,
                            [workspace, workspace],
                        )
                    )

            self.assertEqual(max_active, 1)
            self.assertEqual(len(results), 2)
            self.assertTrue(all(Path(result["data_js"]).is_file() for result in results))

    def test_workspace_refresh_lock_is_reentrant_and_blocks_other_builds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            _init_workspace(workspace, force=False)
            build_insights(workspace, None, workspace / "derived")
            build_started = Event()

            def waiting_build() -> dict:
                build_started.set()
                return build_training_center(workspace)

            with ThreadPoolExecutor(max_workers=1) as executor:
                with training_center_module._training_center_build_lock(workspace):
                    # A sanctioned writer can safely rebuild while already holding
                    # the workspace lock, and another thread cannot enter midway.
                    nested = build_training_center(workspace)
                    future = executor.submit(waiting_build)
                    self.assertTrue(build_started.wait(timeout=1))
                    time.sleep(0.05)
                    self.assertFalse(future.done())
                queued = future.result(timeout=5)

            self.assertTrue(Path(nested["data_js"]).is_file())
            self.assertTrue(Path(queued["data_js"]).is_file())

    def test_activity_detail_cache_invalidates_only_the_changed_week(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            _init_workspace(workspace, force=False)
            activities = {
                "first-ride": {
                    "id": "first-ride",
                    "name": "First Ride",
                    "sport_type": "Ride",
                    "start_date": "2026-04-28T08:00:00Z",
                    "start_date_local": "2026-04-28T08:00:00Z",
                    "moving_time": 360,
                    "distance": 3000,
                    "average_watts": 200,
                },
                "second-ride": {
                    "id": "second-ride",
                    "name": "Second Ride",
                    "sport_type": "Ride",
                    "start_date": "2026-05-12T08:00:00Z",
                    "start_date_local": "2026-05-12T08:00:00Z",
                    "moving_time": 360,
                    "distance": 3200,
                    "average_watts": 210,
                },
            }
            activities_path = workspace / "recordings" / "activities.json"
            activities_path.write_text(json.dumps(activities), encoding="utf-8")
            streams_dir = workspace / "recordings" / "streams"
            streams_dir.mkdir(parents=True, exist_ok=True)
            samples = list(range(12))

            def write_stream(activity_id: str, offset: int) -> None:
                (streams_dir / f"{activity_id}.json").write_text(
                    json.dumps(
                        {
                            "streams": [
                                {"type": "time", "data": [value * 30 for value in samples]},
                                {"type": "moving", "data": [True] * len(samples)},
                                {"type": "watts", "data": [offset + value for value in samples]},
                            ]
                        }
                    ),
                    encoding="utf-8",
                )

            def details_files(result: dict) -> dict[str, str]:
                payload = _training_center_payload(
                    Path(result["data_js"]).read_text(encoding="utf-8")
                )
                return {
                    week["start_date"]: week["activity_details_file"]
                    for week in payload["weeks"]
                    if week.get("activity_details_file")
                }

            write_stream("first-ride", 200)
            write_stream("second-ride", 210)
            build_insights(workspace, None, workspace / "derived")
            cold = build_training_center(workspace)
            cold_files = details_files(cold)
            self.assertEqual(cold["activity_detail_files_rebuilt"], 2)
            self.assertEqual(len(cold_files), 2)

            warm = build_training_center(workspace)
            self.assertEqual(warm["activity_detail_files_rebuilt"], 0)
            self.assertEqual(warm["activity_detail_files_reused"], 2)
            self.assertEqual(details_files(warm), cold_files)

            write_stream("first-ride", 300)
            changed_stream = build_training_center(workspace)
            changed_stream_files = details_files(changed_stream)
            changed_weeks = {
                week_start
                for week_start, filename in changed_stream_files.items()
                if filename != cold_files[week_start]
            }
            self.assertEqual(changed_stream["activity_detail_files_rebuilt"], 1)
            self.assertEqual(changed_stream["activity_detail_files_reused"], 1)
            self.assertEqual(len(changed_weeks), 1)

            activities["second-ride"]["name"] = "Renamed Second Ride"
            activities_path.write_text(json.dumps(activities), encoding="utf-8")
            build_insights(workspace, None, workspace / "derived")
            changed_activity = build_training_center(workspace)
            changed_activity_files = details_files(changed_activity)
            activity_changed_weeks = {
                week_start
                for week_start, filename in changed_activity_files.items()
                if filename != changed_stream_files[week_start]
            }
            self.assertEqual(changed_activity["activity_detail_files_rebuilt"], 1)
            self.assertEqual(changed_activity["activity_detail_files_reused"], 1)
            self.assertEqual(len(activity_changed_weeks), 1)
            self.assertNotEqual(activity_changed_weeks, changed_weeks)

            original_fingerprint = (
                training_center_module._activity_detail_week_fingerprint
            )
            fingerprint_calls = 0

            def mutate_accepted_week(*args, **kwargs):
                nonlocal fingerprint_calls
                fingerprint = original_fingerprint(*args, **kwargs)
                fingerprint_calls += 1
                if fingerprint_calls == 3:
                    write_stream("first-ride", 400)
                return fingerprint

            with patch.object(
                training_center_module,
                "_activity_detail_week_fingerprint",
                side_effect=mutate_accepted_week,
            ):
                raced = build_training_center(workspace)
            raced_files = details_files(raced)
            first_week = next(iter(changed_weeks))
            raced_sidecar = (
                Path(raced["activity_details_dir"])
                / raced_files[first_week]
            ).read_text(encoding="utf-8")
            self.assertIn('"values":[400.5', raced_sidecar)

    def test_planned_load_keeps_unsourced_tss_and_duration_missing(self) -> None:
        load = _planned_load_for_day("3x10 min threshold", [])
        self.assertIsNone(load["estimated_tss"])
        self.assertIsNone(load["hours"])
        self.assertEqual(load["label"], "-- planned TSS")

    def test_week_status_is_not_measured_without_actual_load(self) -> None:
        row = {
            "start_date": "2026-07-06",
            "end_date": "2026-07-12",
            "plan": {"tss_target": {"min": 300, "max": 350}},
            "status_meaningful": "below",
            "totals": {"estimated_tss": None},
        }
        self.assertEqual(
            _week_display_status(row, [], today=date(2026, 7, 8)),
            "not_measured",
        )
        self.assertEqual(_status_label("not_measured"), "Not measured")

    def test_minute_bucket_stream_values_average_each_moving_minute(self) -> None:
        values = [100, 140, 180, 220, 260, 300, 340, 380, 420, 460, 500, 540]
        times = [index * 30 for index in range(len(values))]
        moving = [True] * len(values)
        self.assertEqual(
            _minute_bucket_stream_values(values, times, moving, allow_zero=True),
            [120.0, 200.0, 280.0, 360.0, 440.0, 520.0],
        )

    def test_activity_stream_shape_prefers_power_and_keeps_elevation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            streams = workspace / "strava" / "streams"
            streams.mkdir(parents=True)
            samples = list(range(12))
            (streams / "123.json").write_text(
                json.dumps(
                    {
                        "streams": [
                            {"type": "time", "data": [value * 30 for value in samples]},
                            {"type": "moving", "data": [True] * len(samples)},
                            {"type": "watts", "data": [200 + value for value in samples]},
                            {"type": "altitude", "data": [100 + value for value in samples]},
                        ]
                    }
                ),
                encoding="utf-8",
            )

            shape = _activity_stream_shape(workspace, 123)

        self.assertEqual(shape["source"], "watts")
        self.assertEqual(shape["label"], "Minute-average power stream")
        self.assertEqual(len(shape["values"]), 6)
        self.assertEqual(len(shape["elevation_ft"]), 6)


if __name__ == "__main__":
    unittest.main()
