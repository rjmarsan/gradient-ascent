from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .activity_files import parse_activity_recording, recording_format
from .storage import read_json, write_json


LOCAL_RECORDING_SOURCE = "local_recording"


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
    activity = prepared["activity"]
    activity_id = activity["id"]
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
