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


def import_activity_recording(
    data_dir: Path,
    recording_path: Path,
    *,
    filename: str | None = None,
) -> dict[str, Any]:
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

    activities = read_json(_activities_path(data_dir), default={}) or {}
    if not isinstance(activities, dict):
        activities = {}
    created = activity_id not in activities
    existing = activities.get(activity_id) if isinstance(activities.get(activity_id), dict) else {}
    display_filename = filename or recording_path.name
    activity = {
        "id": activity_id,
        "name": existing.get("name") or _activity_name(display_filename),
        "sport_type": "Ride",
        "type": "Ride",
        "start_date": start_date,
        "start_date_local": start_date,
        **{key: value for key, value in summary.items() if value is not None},
        "recording_format": format_name,
        "recording_filename": existing.get("recording_filename") or display_filename,
        "import_source": LOCAL_RECORDING_SOURCE,
    }
    activities[activity_id] = activity

    streams = dict(parsed["streams"])
    streams["source"] = LOCAL_RECORDING_SOURCE
    laps = dict(parsed["laps"])
    laps["source"] = LOCAL_RECORDING_SOURCE
    write_json(_activities_path(data_dir), activities)
    write_json(data_dir / "recordings" / "streams" / f"{activity_id}.json", streams)
    write_json(data_dir / "recordings" / "laps" / f"{activity_id}.json", laps)

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
        "stream_count": len(streams.get("streams") or []),
        "lap_count": len(laps.get("laps") or []),
    }
