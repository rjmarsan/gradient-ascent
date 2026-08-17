from __future__ import annotations

import hashlib
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .activity_files import RECORDING_STREAM_VERSION, parse_activity_recording, recording_format
from .storage import read_json, write_json


LOCAL_RECORDING_SOURCE = "local_recording"
MAX_SOURCE_DURATION_SECONDS = 31 * 24 * 60 * 60


def recording_source_duration_fields(
    moving_time: Any = None,
    elapsed_time: Any = None,
) -> dict[str, int]:
    """Validate optional provider-reported integer seconds without inferring pauses."""
    result: dict[str, int] = {}
    for key, value in (("source_moving_time", moving_time), ("source_elapsed_time", elapsed_time)):
        if value is None:
            continue
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not 0 <= value <= MAX_SOURCE_DURATION_SECONDS
            or not math.isfinite(value)
            or int(value) != value
        ):
            raise ValueError("Recording source duration is invalid.")
        result[key] = int(value)
    if result.get("source_moving_time", 0) > result.get(
        "source_elapsed_time", MAX_SOURCE_DURATION_SECONDS
    ):
        raise ValueError("Recording moving time exceeds its source elapsed time.")
    return result


def _finite_nonnegative(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except (ValueError, OverflowError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def apply_recording_source_durations(activity: dict[str, Any]) -> dict[str, Any]:
    """Apply already-stored authoritative time to a proven owned recording copy."""
    result = dict(activity)
    if not (
        activity.get("import_source") == LOCAL_RECORDING_SOURCE
        and activity.get("source_provider") == "ridewithgps"
        and isinstance(activity.get("id"), str)
        and re.fullmatch(r"recording-[a-f0-9]{64}", activity["id"])
        and isinstance(activity.get("source_activity_id"), str)
        and re.fullmatch(r"[1-9][0-9]{0,31}", activity["source_activity_id"])
    ):
        return result
    try:
        fields = recording_source_duration_fields(
            activity.get("source_moving_time"), activity.get("source_elapsed_time")
        )
    except ValueError:
        return result
    old_moving = _finite_nonnegative(activity.get("moving_time"))
    moving = fields.get("source_moving_time", old_moving)
    elapsed = fields.get("source_elapsed_time", _finite_nonnegative(activity.get("elapsed_time")))
    if moving is not None and elapsed is not None and moving > elapsed:
        return result
    if "source_moving_time" in fields:
        result["moving_time"] = fields["source_moving_time"]
        average = _finite_nonnegative(activity.get("average_watts"))
        work = _finite_nonnegative(activity.get("kilojoules"))
        work_source = activity.get("kilojoules_source")
        derived_work = work_source == "estimated_average_power" or (
            work_source is None
            and activity.get("source_kilojoules") is None
            and average is not None
            and old_moving is not None
            and work is not None
            and math.isclose(work, average * old_moving / 1000.0, rel_tol=1e-9, abs_tol=1e-6)
        )
        if derived_work and average is not None and average <= 10000:
            result["kilojoules"] = average * fields["source_moving_time"] / 1000.0
            result["kilojoules_source"] = "estimated_average_power"
    if "source_elapsed_time" in fields:
        result["elapsed_time"] = fields["source_elapsed_time"]
    return result


def _activities_path(data_dir: Path) -> Path:
    return data_dir / "recordings" / "activities.json"


def _activity_name(filename: str) -> str:
    stem = Path(filename).stem
    words = re.sub(r"[-_]+", " ", stem).strip()
    return words.title() or "Imported Ride"


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prepare_activity_recording(
    recording_path: Path,
    *,
    filename: str | None = None,
) -> dict[str, Any]:
    """Parse one recording without changing a workspace."""
    recording_path = recording_path.expanduser().resolve()
    format_name = recording_format(recording_path.name)
    if format_name is None or recording_path.suffix.lower() == ".gz":
        raise ValueError("Expected a FIT, TCX, or GPX activity recording.")

    digest = _file_digest(recording_path)
    activity_id = f"recording-{digest}"
    try:
        with recording_path.open("rb") as handle:
            parsed = parse_activity_recording(handle, recording_path.name)
    except Exception as exc:
        raise ValueError(f"Could not parse {recording_path.name}: {exc}") from exc

    summary = parsed.get("summary") if isinstance(parsed.get("summary"), dict) else {}
    start_date = summary.get("start_date")
    if not start_date:
        raise ValueError("Activity recording does not contain timestamped samples.")

    display_filename = filename or recording_path.name
    activity = {
        "id": activity_id,
        "name": _activity_name(display_filename),
        "sport_type": "Ride",
        "type": "Ride",
        "start_date": start_date,
        "start_date_local": start_date,
        **{key: value for key, value in summary.items() if value is not None},
        "recording_format": format_name,
        "recording_parser_version": RECORDING_STREAM_VERSION,
        "recording_filename": display_filename,
        "import_source": LOCAL_RECORDING_SOURCE,
    }
    streams = dict(parsed["streams"])
    streams["source"] = LOCAL_RECORDING_SOURCE
    laps = dict(parsed["laps"])
    laps["source"] = LOCAL_RECORDING_SOURCE
    return {
        "activity": activity,
        "streams": streams,
        "laps": laps,
        "stream_count": len(streams.get("streams") or []),
        "lap_count": len(laps.get("laps") or []),
    }


def import_activity_recording(
    data_dir: Path,
    recording_path: Path,
    *,
    filename: str | None = None,
) -> dict[str, Any]:
    prepared = prepare_activity_recording(recording_path, filename=filename)
    from .recording_repair import merge_recording_metrics, retain_recording_original

    activity = prepared["activity"]
    activity_id = activity["id"]
    data_dir.mkdir(parents=True, exist_ok=True)
    retain_recording_original(data_dir, recording_path.expanduser().resolve(), activity_id, activity["recording_format"])
    activities = read_json(_activities_path(data_dir), default={}) or {}
    if not isinstance(activities, dict):
        activities = {}
    created = activity_id not in activities
    existing = activities.get(activity_id) if isinstance(activities.get(activity_id), dict) else {}
    activity["name"] = existing.get("name") or activity["name"]
    activity["recording_filename"] = (
        existing.get("recording_filename") or activity["recording_filename"]
    )
    if existing.get("id") == activity_id:
        existing_laps = read_json(data_dir / "recordings" / "laps" / f"{activity_id}.json", default={})
        metrics = merge_recording_metrics(
            existing,
            {"summary": activity, "laps": prepared["laps"]},
            existing_laps.get("laps") if isinstance(existing_laps, dict) else None,
        )
        for key in ("weighted_average_watts", "weighted_average_watts_source", "estimated_tss", "intensity_factor", "timer_time"):
            activity.pop(key, None)
            if key in metrics:
                activity[key] = metrics[key]
        for key in (
            "start_date_local",
            "source_provider",
            "source_provider_name",
            "source_activity_id",
            "source_url",
        ):
            value = existing.get(key)
            if isinstance(value, str) and value:
                activity[key] = value
        if (
            existing.get("import_source") == LOCAL_RECORDING_SOURCE
            and existing.get("source_provider") == "ridewithgps"
            and isinstance(existing.get("source_activity_id"), str)
            and re.fullmatch(r"[1-9][0-9]{0,31}", existing["source_activity_id"])
        ):
            # Reuse the provider's strict classifier only for an exact-content
            # row it already owns. Arbitrary/manual sport strings are not kept.
            from .ridewithgps import _sport_metadata

            activity.update(
                _sport_metadata(
                    {
                        key: existing.get(f"source_{key}")
                        for key in ("activity_type", "fit_sport", "fit_sub_sport")
                    }
                )
            )
            for key in ("source_moving_time", "source_elapsed_time"):
                if key in existing:
                    activity[key] = existing[key]
            if (
                existing.get("kilojoules_source") in ("source", "device", "provider")
                or _finite_nonnegative(existing.get("source_kilojoules")) is not None
            ):
                for key in ("kilojoules", "source_kilojoules", "kilojoules_source"):
                    if key in existing:
                        activity[key] = existing[key]
            activity = apply_recording_source_durations(activity)
    activities[activity_id] = activity
    write_json(_activities_path(data_dir), activities)
    write_json(data_dir / "recordings" / "streams" / f"{activity_id}.json", prepared["streams"])
    write_json(data_dir / "recordings" / "laps" / f"{activity_id}.json", prepared["laps"])

    state_path = data_dir / "recordings" / "state.json"
    state = read_json(state_path, default={}) or {}
    if not isinstance(state, dict):
        state = {}
    state.update(
        activity_count=len(activities),
        last_import_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        last_activity_id=activity_id,
    )
    write_json(state_path, state)
    return {
        "created": created,
        "activity": activity,
        "stream_count": prepared["stream_count"],
        "lap_count": prepared["lap_count"],
    }
