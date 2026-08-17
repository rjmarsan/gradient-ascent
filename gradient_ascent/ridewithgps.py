"""Bounded Ride with GPS imports through an explicitly supplied authenticated client.

The caller owns the workspace-generation lock and the eventual dashboard rebuild.
This module never authenticates, opens a network connection, or exposes raw rides.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import stat
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .recordings import (
    LOCAL_RECORDING_SOURCE,
    apply_recording_source_durations,
    prepare_activity_recording,
    recording_source_duration_fields,
)


DEFAULT_SYNC_DAYS = 14
MAX_SYNC_DAYS = 365
MAX_PAGES = 10
MAX_PAGE = 100_000
MAX_TRIPS = 1000
MAX_TRACK_POINTS = 100_000
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_TCX_BYTES = 32 * 1024 * 1024
MAX_SYNC_BYTES = 256 * 1024 * 1024
MAX_INDEX_BYTES = 128 * 1024 * 1024
MAX_METADATA_BYTES = 64 * 1024
_ID = re.compile(r"[1-9][0-9]{0,31}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_TCX = "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"
_EXT = "http://www.garmin.com/xmlschemas/ActivityExtension/v2"
_GET_JSON = Callable[[str, dict[str, int | str]], dict[str, Any]]
_SOURCE_TAXONOMY_FIELDS = ("source_activity_type", "source_fit_sport", "source_fit_sub_sport")
# https://ridewithgps.com/api/v1/doc/reference/activity_types
_CYCLING_TYPES = frozenset(
    {
        "cycling:generic",
        "cycling:road",
        "cycling:gravel",
        "cycling:cyclocross",
        "cycling:mountain",
        "cycling:recumbent",
        "cycling:hand_cycling",
        "cycling:commute",
        "cycling:indoor",
        "cycling:virtual",
        "e_biking:generic",
        "e_biking:road",
        "e_biking:mountain",
        "cycling",
        "bike",
        "biking",
        "ride",
        "virtualride",
    }
)


def _owner(metadata: os.stat_result) -> None:
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise ValueError("Ride with GPS files must belong to the current user.")
    if stat.S_IMODE(metadata.st_mode) & 0o022:
        raise ValueError("Ride with GPS files cannot be writable by other users.")


@contextmanager
def _directory(root: Path, *parts: str) -> Iterator[int]:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise RuntimeError("This platform cannot safely open private activity files.")
    if ".." in root.parts or root.is_symlink():
        raise ValueError("Ride with GPS workspace cannot contain symbolic links or traversal.")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(root, flags)
    try:
        _owner(os.fstat(descriptor))
        for part in parts:
            if not part or Path(part).name != part or part in {".", ".."}:
                raise ValueError("Ride with GPS directory escaped its workspace.")
            try:
                expected = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                os.mkdir(part, mode=0o700, dir_fd=descriptor)
                expected = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISLNK(expected.st_mode):
                raise ValueError("Ride with GPS directory cannot be a symbolic link.")
            if not stat.S_ISDIR(expected.st_mode):
                raise ValueError("Ride with GPS requires a real private directory.")
            _owner(expected)
            child = os.open(part, flags, dir_fd=descriptor)
            actual = os.fstat(child)
            if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
                os.close(child)
                raise ValueError("Ride with GPS directory changed while opening.")
            os.fchmod(child, 0o700)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def _file_stat(directory: int, name: str, limit: int) -> os.stat_result | None:
    if Path(name).name != name or name in {"", ".", ".."}:
        raise ValueError("Ride with GPS file escaped its private directory.")
    try:
        result = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(result.st_mode):
        raise ValueError("Ride with GPS file cannot be a symbolic link.")
    if not stat.S_ISREG(result.st_mode) or result.st_nlink != 1:
        raise ValueError("Ride with GPS requires an unlinked regular file.")
    _owner(result)
    if result.st_size > limit:
        raise ValueError("Ride with GPS file exceeds its size limit.")
    return result


def _read(directory: int, name: str, limit: int) -> bytes | None:
    expected = _file_stat(directory, name, limit)
    if expected is None:
        return None
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
    try:
        actual = os.fstat(descriptor)
        if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
            raise ValueError("Ride with GPS file changed while opening.")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            body = handle.read(limit + 1)
        if len(body) > limit:
            raise ValueError("Ride with GPS file exceeds its size limit.")
        return body
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _write(directory: int, name: str, body: bytes, limit: int) -> None:
    if len(body) > limit:
        raise ValueError("Ride with GPS file exceeds its size limit.")
    _file_stat(directory, name, limit)
    temporary = f".{name}.{secrets.token_hex(12)}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        _file_stat(directory, name, limit)
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass


def _read_json(directory: int, name: str, limit: int, default: Any = None) -> Any:
    body = _read(directory, name, limit)
    if body is None:
        return default
    try:
        return json.loads(body)
    except (UnicodeError, ValueError) as exc:
        raise ValueError("Ride with GPS local state is invalid.") from exc


def _write_json(directory: int, name: str, value: Any, limit: int) -> None:
    body = (
        json.dumps(value, separators=(",", ":"), sort_keys=True, allow_nan=False) + "\n"
    ).encode()
    _write(directory, name, body, limit)


def _identifier(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    text = str(value)
    return text if _ID.fullmatch(text) else None


def _aware(value: Any, description: str) -> datetime:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ValueError(f"Ride with GPS {description} is invalid.")
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Ride with GPS {description} is invalid.") from exc
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"Ride with GPS {description} requires a time zone.")
    return moment


def _local_departure(trip: dict[str, Any]) -> datetime:
    departed = _aware(trip.get("departed_at"), "departure")
    name = trip.get("time_zone")
    if not isinstance(name, str) or not name or len(name) > 128:
        raise ValueError("Ride with GPS requires a valid provider time zone.")
    try:
        return departed.astimezone(ZoneInfo(name))
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("Ride with GPS requires a valid provider time zone.") from exc


def _number(value: Any, minimum: float, maximum: float, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Ride with GPS {label} must be numeric.")
    value = float(value)
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"Ride with GPS {label} is outside its supported range.")
    return value


def _time(point: dict[str, Any]) -> datetime:
    seconds = _number(point.get("t"), 0, 4_102_444_800, "timestamp")
    if seconds != int(seconds):
        raise ValueError("Ride with GPS track timestamps must be whole seconds.")
    return datetime.fromtimestamp(seconds, timezone.utc)


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _tag(name: str, extension: bool = False) -> str:
    return f"{{{_EXT if extension else _TCX}}}{name}"


def _text(parent: ET.Element, name: str, value: Any, extension: bool = False) -> None:
    ET.SubElement(parent, _tag(name, extension)).text = (
        format(value, ".12g") if isinstance(value, float) else str(value)
    )


def _groups(points: Any) -> list[list[dict[str, Any]]]:
    if not isinstance(points, list) or not 2 <= len(points) <= MAX_TRACK_POINTS:
        raise ValueError("Ride with GPS track points exceed the supported limit.")
    groups: list[list[dict[str, Any]]] = [[]]
    previous = None
    for point in points:
        if not isinstance(point, dict):
            raise ValueError("Ride with GPS track points must be objects.")
        moment = _time(point)
        if previous is not None and moment < previous:
            raise ValueError("Ride with GPS timestamps cannot move backward.")
        previous = moment
        if point.get("lap") is True and groups[-1]:
            groups.append([])
        groups[-1].append(point)
    return groups


def _trackpoint(track: ET.Element, point: dict[str, Any]) -> None:
    node = ET.SubElement(track, _tag("Trackpoint"))
    _text(node, "Time", _utc(_time(point)))
    if point.get("x") is not None or point.get("y") is not None:
        position = ET.SubElement(node, _tag("Position"))
        _text(position, "LatitudeDegrees", _number(point.get("y"), -90, 90, "latitude"))
        _text(position, "LongitudeDegrees", _number(point.get("x"), -180, 180, "longitude"))
    for key, name, low, high in (
        ("e", "AltitudeMeters", -1000, 20_000),
        ("d", "DistanceMeters", 0, 10_000_000),
        ("c", "Cadence", 0, 300),
    ):
        if point.get(key) is not None:
            _text(node, name, _number(point[key], low, high, name))
    if point.get("h") is not None:
        _text(
            ET.SubElement(node, _tag("HeartRateBpm")),
            "Value",
            _number(point["h"], 0, 300, "heart rate"),
        )
    if any(point.get(key) is not None for key in ("s", "p", "T")):
        tpx = ET.SubElement(ET.SubElement(node, _tag("Extensions")), _tag("TPX", True))
        for key, name, low, high, divisor in (
            ("s", "Speed", 0, 300, 3.6),
            ("p", "Watts", 0, 10_000, 1),
            ("T", "Temperature", -100, 100, 1),
        ):
            if point.get(key) is not None:
                _text(tpx, name, _number(point[key], low, high, name) / divisor, True)


def trip_to_tcx(trip: dict[str, Any]) -> bytes:
    """Convert documented trip telemetry to deterministic TCX History bytes."""
    if not isinstance(trip, dict) or _identifier(trip.get("id")) is None:
        raise ValueError("Ride with GPS trip has an invalid identifier.")
    groups = _groups(trip.get("track_points"))
    ET.register_namespace("", _TCX)
    ET.register_namespace("activityext", _EXT)
    root = ET.Element(_tag("TrainingCenterDatabase"))
    activity = ET.SubElement(
        ET.SubElement(root, _tag("Activities")), _tag("Activity"), {"Sport": "Biking"}
    )
    _text(activity, "Id", _utc(_time(groups[0][0])))
    for index, group in enumerate(groups):
        end = groups[index + 1][0] if index + 1 < len(groups) else group[-1]
        first_distance = _number(group[0].get("d", 0), 0, 10_000_000, "distance")
        final_distance = _number(end.get("d", 0), 0, 10_000_000, "distance")
        if final_distance < first_distance:
            raise ValueError("Ride with GPS lap distance cannot move backward.")
        lap = ET.SubElement(activity, _tag("Lap"), {"StartTime": _utc(_time(group[0]))})
        _text(lap, "TotalTimeSeconds", (_time(end) - _time(group[0])).total_seconds())
        _text(lap, "DistanceMeters", final_distance - first_distance)
        track = ET.SubElement(lap, _tag("Track"))
        for point in group:
            _trackpoint(track, point)
    _text(activity, "Notes", f"Ride with GPS trip {_identifier(trip['id'])}")
    result = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    if len(result) > MAX_TCX_BYTES:
        raise ValueError("Ride with GPS recording exceeds its size limit.")
    return result


def _response(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Ride with GPS returned an invalid response.")
    try:
        body = json.dumps(value, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Ride with GPS returned unsupported data.") from exc
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("Ride with GPS response exceeds its size limit.")
    return value


def _next_page(response: dict[str, Any], page: int, page_size: int, count: int) -> int | None:
    meta = response.get("meta")
    if meta is not None and not isinstance(meta, dict):
        raise ValueError("Ride with GPS pagination is invalid.")
    pagination = (meta or {}).get("pagination")
    if pagination is None:
        return page + 1 if count == page_size else None
    if not isinstance(pagination, dict) or "next_page_url" not in pagination:
        raise ValueError("Ride with GPS pagination is invalid.")
    value = pagination["next_page_url"]
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("Ride with GPS pagination is invalid.")
    parsed = urlparse(value)
    query = parse_qs(parsed.query, strict_parsing=True)
    if (
        (parsed.scheme, parsed.netloc, parsed.path)
        != ("https", "ridewithgps.com", "/api/v1/trips.json")
        or parsed.fragment
        or query != {"page": [str(page + 1)], "page_size": [str(page_size)]}
        or page >= MAX_PAGE
    ):
        raise ValueError("Ride with GPS pagination is invalid.")
    return page + 1


def _cycling(trip: dict[str, Any]) -> bool:
    kind = trip.get("activity_type")
    kind = kind.strip().casefold() if isinstance(kind, str) else ""
    if kind in _CYCLING_TYPES:
        return True
    # Explicit non-cycling labels take precedence over contradictory FIT data.
    return (
        kind in {"", "unknown:generic"}
        and type(trip.get("fit_sport")) is int
        and trip["fit_sport"] in {2, 21}
    )


def _source_taxonomy(trip: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    kind = trip.get("activity_type")
    if isinstance(kind, str):
        kind = kind.strip().casefold()
        if kind in _CYCLING_TYPES or kind == "unknown:generic":
            result["source_activity_type"] = kind
    for key in ("fit_sport", "fit_sub_sport"):
        value = trip.get(key)
        if type(value) is int and 0 <= value <= 254:
            result[f"source_{key}"] = value
    return result


def _sport_metadata(trip: dict[str, Any]) -> dict[str, Any]:
    """Return only documented cycling classifications and bounded source values."""
    if not _cycling(trip):
        return {}
    source = _source_taxonomy(trip)
    kind = source.get("source_activity_type", "")
    sport, sub = source.get("source_fit_sport"), source.get("source_fit_sub_sport")
    electric = kind.startswith("e_biking:") or sport == 21 or sub in {28, 47}
    mountain = kind == "e_biking:mountain" or sub in {8, 47}
    return {
        **source,
        "type": "EBikeRide" if electric else "Ride",
        "sport_type": "EMountainBikeRide"
        if electric and mountain
        else "EBikeRide"
        if electric
        else "Ride",
    }


def _owned_record(activity: Any, activity_id: str, trip_id: str) -> bool:
    return (
        isinstance(activity, dict)
        and activity.get("id") == activity_id
        and activity.get("import_source") == LOCAL_RECORDING_SOURCE
        and activity.get("source_provider") == "ridewithgps"
        and activity.get("source_activity_id") == trip_id
    )


def _local_record(activity: Any, activity_id: str) -> bool:
    return (
        isinstance(activity, dict)
        and activity.get("id") == activity_id
        and activity.get("import_source") == LOCAL_RECORDING_SOURCE
    )


def _metadata(value: Any, trip_id: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or value.get("version") != 1
        or value.get("trip_id") != trip_id
        or not isinstance(value.get("sha256"), str)
        or _SHA.fullmatch(value["sha256"]) is None
        or type(value.get("owned", True)) is not bool
        or (
            value.get("last_provider_name") is not None
            and _provider_name(value.get("last_provider_name")) != value["last_provider_name"]
        )
    ):
        raise ValueError("Ride with GPS activity metadata is invalid.")
    superseded = value.get("superseded", [])
    if (
        not isinstance(superseded, list)
        or len(superseded) > 256
        or any(not isinstance(item, str) or _SHA.fullmatch(item) is None for item in superseded)
    ):
        raise ValueError("Ride with GPS superseded metadata is invalid.")
    recording_source_duration_fields(
        value.get("source_moving_time"), value.get("source_elapsed_time")
    )
    return value


def _source_durations(
    trip: dict[str, Any],
    previous: dict[str, Any] | None = None,
) -> dict[str, int]:
    # Both fields are documented integer seconds. Keep the original TCX and
    # its elapsed timestamps unchanged; never infer a pause from missing points.
    fields = recording_source_duration_fields(
        (previous or {}).get("source_moving_time"),
        (previous or {}).get("source_elapsed_time"),
    )
    fields.update(recording_source_duration_fields(trip.get("moving_time"), trip.get("duration")))
    return recording_source_duration_fields(
        fields.get("source_moving_time"), fields.get("source_elapsed_time")
    )


def _provider_name(value: Any) -> str | None:
    return (
        value.strip() if isinstance(value, str) and value.strip() and len(value) <= 1000 else None
    )


def _title_baseline(activity: dict[str, Any] | None, previous: dict[str, Any] | None) -> str | None:
    if not activity:
        return None
    name = _provider_name(activity.get("source_provider_name"))
    if name is None and previous and activity.get("id") == f"recording-{previous.get('sha256')}":
        name = previous.get("last_provider_name")
    return name


def _previous_owned_activity(
    activities: dict[str, Any],
    owned_ids: set[str],
    current_id: str,
    metadata_id: str | None,
    previous: dict[str, Any] | None,
) -> dict[str, Any] | None:
    candidates = [activities[key] for key in sorted(owned_ids)]
    authored = {
        row["name"]
        for row in candidates
        if isinstance(row.get("name"), str)
        and row["name"].strip()
        and row["name"] != _title_baseline(row, previous)
    }
    if len(authored) > 1:
        raise ValueError("Ride with GPS has conflicting locally authored titles.")
    if authored:
        return next(row for row in candidates if row.get("name") in authored)
    for identifier in (current_id, metadata_id):
        if identifier in owned_ids:
            return activities[identifier]
    return candidates[0] if candidates else None


def _decorate(
    activity: dict[str, Any],
    trip: dict[str, Any],
    identifier: str,
    *,
    previous: dict[str, Any] | None = None,
    previous_activity: dict[str, Any] | None = None,
) -> None:
    start = _aware(activity.get("start_date"), "recording start")
    local = _local_departure(trip)
    if abs((start - local).total_seconds()) > 60:
        raise ValueError("Ride with GPS departure does not match its recording.")
    name = _provider_name(trip.get("name"))
    existing_name = previous_activity.get("name") if previous_activity else None
    last_name = _title_baseline(previous_activity, previous)
    if isinstance(existing_name, str) and existing_name.strip():
        activity["name"] = existing_name
        # Older/adopted metadata has no title history. Keep its existing title
        # until we can distinguish a provider rename from an athlete edit.
        if name is not None and existing_name == last_name:
            activity["name"] = name
    elif name is not None:
        activity["name"] = name
    if name is not None or last_name is not None:
        # Commit the displayed title and its upstream baseline in one atomic
        # activity-index write. Per-trip metadata may lag after an interrupted
        # page commit and must not turn an upstream title into an athlete edit.
        activity["source_provider_name"] = name or last_name
    activity.update(
        start_date_local=start.astimezone(local.tzinfo).isoformat(timespec="seconds"),
        source_provider="ridewithgps",
        source_activity_id=identifier,
        source_url=f"https://ridewithgps.com/trips/{identifier}",
    )
    for key in _SOURCE_TAXONOMY_FIELDS:
        activity.pop(key, None)
    activity.update(_sport_metadata(trip))
    activity.update(_source_durations(trip, {**(previous or {}), **(previous_activity or {})}))
    activity.update(apply_recording_source_durations(activity))


def _summary(full_history: bool) -> dict[str, Any]:
    return {
        "provider": "ridewithgps",
        "mode": "history" if full_history else "recent",
        "pages": 0,
        "listed": 0,
        "eligible": 0,
        "imported": 0,
        "updated": 0,
        "existing": 0,
        "skipped": 0,
        "streams": 0,
        "laps": 0,
        "complete": False,
        "next_page": 1,
        "next_offset": 0,
        "has_more": True,
    }


def sync_ridewithgps(
    data_dir: Path,
    get_json: _GET_JSON,
    *,
    days: int = DEFAULT_SYNC_DAYS,
    full_history: bool = False,
    restart: bool = False,
    page_size: int = 100,
    max_pages: int = 5,
    today: date | None = None,
) -> dict[str, Any]:
    """Import bounded pages; caller must hold its expected-generation workspace lock."""
    for value, minimum, maximum, label in (
        (days, 1, MAX_SYNC_DAYS, "days"),
        (page_size, 20, 200, "page size"),
        (max_pages, 1, MAX_PAGES, "page count"),
    ):
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f"Ride with GPS {label} exceeds its supported limit.")
    if (
        type(full_history) is not bool
        or type(restart) is not bool
        or restart
        and not full_history
        or page_size * max_pages > MAX_TRIPS
        or not callable(get_json)
    ):
        raise ValueError("Ride with GPS sync options are invalid.")
    end_date = today or date.today()
    start_date = end_date - timedelta(days=days - 1)
    root = Path(os.path.abspath(data_dir.expanduser()))
    result = _summary(full_history)
    consumed_bytes = 0
    with ExitStack() as stack:
        provider = stack.enter_context(_directory(root, "integrations", "ridewithgps"))
        files = stack.enter_context(_directory(root, "integrations", "ridewithgps", "files"))
        recording_dir = stack.enter_context(_directory(root, "recordings"))
        stream_dir = stack.enter_context(_directory(root, "recordings", "streams"))
        lap_dir = stack.enter_context(_directory(root, "recordings", "laps"))
        activities = _read_json(recording_dir, "activities.json", MAX_INDEX_BYTES, {})
        state = _read_json(provider, "state.json", MAX_METADATA_BYTES, {"version": 1})
        if (
            not isinstance(activities, dict)
            or not isinstance(state, dict)
            or state.get("version") != 1
        ):
            raise ValueError("Ride with GPS workspace state is invalid.")
        owned_by_trip: dict[str, set[str]] = {}
        for key, activity in activities.items():
            identifier = (
                _identifier(activity.get("source_activity_id"))
                if isinstance(activity, dict)
                else None
            )
            if (
                identifier is not None
                and isinstance(key, str)
                and key.startswith("recording-")
                and _SHA.fullmatch(key.removeprefix("recording-")) is not None
                and _owned_record(activity, key, identifier)
            ):
                owned_by_trip.setdefault(identifier, set()).add(key)
        cursor = state.get("backfill") if full_history and not restart else None
        if cursor is not None and (
            not isinstance(cursor, dict)
            or cursor.get("page_size") != page_size
            or type(cursor.get("next_page")) is not int
            or not 1 <= cursor["next_page"] <= MAX_PAGE
            or type(cursor.get("next_offset", 0)) is not int
            or not 0 <= cursor.get("next_offset", 0) < page_size
        ):
            raise ValueError("Ride with GPS backfill cursor is invalid; restart the import.")
        if cursor and cursor.get("complete") is True:
            result.update(complete=True, next_page=None, has_more=False)
            return result
        page = cursor["next_page"] if cursor else 1
        offset = cursor.get("next_offset", 0) if cursor else 0
        seen: set[str] = set()
        for _ in range(max_pages):
            response = _response(
                get_json("/api/v1/trips.json", {"page": page, "page_size": page_size})
            )
            trips = response.get("trips")
            if not isinstance(trips, list) or len(trips) > page_size or offset > len(trips):
                raise ValueError("Ride with GPS returned an invalid trip page.")
            following = _next_page(response, page, page_size, len(trips))
            result["pages"] += 1
            stop_offset = None
            metadata_updates: dict[str, dict[str, Any]] = {}
            for position, trip in enumerate(trips[offset:], start=offset):
                result["listed"] += 1
                identifier = _identifier(trip.get("id")) if isinstance(trip, dict) else None
                if identifier is None or identifier in seen or not _cycling(trip):
                    result["skipped"] += 1
                    continue
                local = _local_departure(trip)
                if not full_history and not start_date <= local.date() <= end_date:
                    result["skipped"] += 1
                    continue
                seen.add(identifier)
                result["eligible"] += 1
                updated_at = trip.get("updated_at")
                _aware(updated_at, "update timestamp")
                metadata_name = f"{identifier}.json"
                previous = _metadata(
                    _read_json(files, metadata_name, MAX_METADATA_BYTES), identifier
                )
                old_sha = previous["sha256"] if previous else None
                activity_id = f"recording-{old_sha}" if old_sha else None
                owned_ids = owned_by_trip.get(identifier, set())
                cached = previous is not None and previous.get("updated_at") == updated_at
                if (
                    cached
                    and _file_stat(files, f"{identifier}-{old_sha}.tcx", MAX_TCX_BYTES) is not None
                    and (
                        owned_ids == {activity_id}
                        and _owned_record(activities.get(activity_id), activity_id, identifier)
                        if previous.get("owned", True)
                        else not owned_ids
                        and _local_record(activities.get(activity_id), activity_id)
                    )
                ):
                    if previous.get("owned", True):
                        _decorate(
                            activities[activity_id],
                            trip,
                            identifier,
                            previous=previous,
                            previous_activity=activities[activity_id],
                        )
                        provider_name = _provider_name(trip.get("name"))
                        refreshed_metadata = {
                            **{
                                key: value
                                for key, value in previous.items()
                                if key not in _SOURCE_TAXONOMY_FIELDS
                            },
                            **_source_taxonomy(trip),
                            **_source_durations(trip, activities[activity_id]),
                        }
                        if provider_name is not None:
                            refreshed_metadata["last_provider_name"] = provider_name
                        if refreshed_metadata != previous:
                            metadata_updates[metadata_name] = refreshed_metadata
                    else:
                        borrowed = _read(files, f"{identifier}-{old_sha}.tcx", MAX_TCX_BYTES)
                        if borrowed is None or hashlib.sha256(borrowed).hexdigest() != old_sha:
                            raise ValueError("Ride with GPS cached recording is invalid.")
                    result["existing"] += 1
                    continue
                if cached:
                    body = _read(files, f"{identifier}-{old_sha}.tcx", MAX_TCX_BYTES)
                    if body is None or hashlib.sha256(body).hexdigest() != old_sha:
                        raise ValueError("Ride with GPS cached recording is invalid.")
                else:
                    detail = _response(get_json(f"/api/v1/trips/{identifier}.json", {})).get("trip")
                    if not isinstance(detail, dict) or _identifier(detail.get("id")) != identifier:
                        raise ValueError("Ride with GPS detail has the wrong identifier.")
                    body = trip_to_tcx(detail)
                if consumed_bytes + len(body) > MAX_SYNC_BYTES:
                    if consumed_bytes == 0:
                        raise ValueError("Ride with GPS recording exceeds the sync byte limit.")
                    stop_offset = position
                    result["listed"] -= 1
                    result["eligible"] -= 1
                    break
                consumed_bytes += len(body)
                digest = hashlib.sha256(body).hexdigest()
                raw_name = f"{identifier}-{digest}.tcx"
                if _file_stat(files, raw_name, MAX_TCX_BYTES) is None:
                    _write(files, raw_name, body, MAX_TCX_BYTES)
                prepared = prepare_activity_recording(
                    root / "integrations" / "ridewithgps" / "files" / raw_name,
                    filename=f"ridewithgps-{identifier}.tcx",
                )
                activity = prepared["activity"]
                if activity["id"] != f"recording-{digest}":
                    raise ValueError("Ride with GPS recording changed during import.")
                current = activities.get(activity["id"])
                borrowed = current is not None and not _owned_record(
                    current, activity["id"], identifier
                )
                if borrowed and not _local_record(current, activity["id"]):
                    raise ValueError("Ride with GPS recording conflicts with existing data.")
                previous_activity = _previous_owned_activity(
                    activities, owned_ids, activity["id"], activity_id, previous
                )
                if borrowed and previous_activity:
                    previous_name = previous_activity.get("name")
                    if (
                        isinstance(previous_name, str)
                        and previous_name.strip()
                        and previous_name != _title_baseline(previous_activity, previous)
                        and previous_name != current.get("name")
                    ):
                        raise ValueError("Ride with GPS conflicts with a locally authored title.")
                if not borrowed:
                    _decorate(
                        activity,
                        trip,
                        identifier,
                        previous=previous,
                        previous_activity=previous_activity,
                    )
                    _write_json(
                        stream_dir, f"{activity['id']}.json", prepared["streams"], MAX_INDEX_BYTES
                    )
                    _write_json(
                        lap_dir, f"{activity['id']}.json", prepared["laps"], MAX_INDEX_BYTES
                    )
                superseded = list(previous.get("superseded", [])) if previous else []
                prior_digests = [key.removeprefix("recording-") for key in sorted(owned_ids)]
                if old_sha:
                    prior_digests.append(old_sha)
                for old in prior_digests:
                    if old != digest and old not in superseded:
                        superseded.append(old)
                superseded = superseded[-256:]
                if not borrowed:
                    activities[activity["id"]] = activity
                for old_id in owned_ids | {f"recording-{old}" for old in superseded}:
                    if old_id != activity["id"] and _owned_record(
                        activities.get(old_id), old_id, identifier
                    ):
                        del activities[old_id]
                if borrowed:
                    owned_by_trip.pop(identifier, None)
                else:
                    owned_by_trip[identifier] = {activity["id"]}
                metadata_updates[metadata_name] = {
                    "version": 1,
                    "trip_id": identifier,
                    "sha256": digest,
                    "owned": not borrowed,
                    "updated_at": updated_at,
                    "departed_at": trip["departed_at"],
                    "time_zone": trip["time_zone"],
                    **_source_taxonomy(trip),
                    **_source_durations(trip, previous if borrowed else activity),
                    "last_provider_name": _provider_name(trip.get("name"))
                    or (previous.get("last_provider_name") if previous else None),
                    "superseded": superseded,
                }
                if borrowed:
                    result["existing"] += 1
                else:
                    result["updated" if previous else "imported"] += 1
                    result["streams"] += prepared["stream_count"]
                    result["laps"] += prepared["lap_count"]
            _write_json(recording_dir, "activities.json", activities, MAX_INDEX_BYTES)
            # A failed page leaves prior metadata intact. After the index is
            # committed, its source_provider_name makes interrupted metadata
            # writes safely replayable without losing an authored title.
            for metadata_name, metadata_value in metadata_updates.items():
                _write_json(files, metadata_name, metadata_value, MAX_METADATA_BYTES)
            recording_state = _read_json(recording_dir, "state.json", MAX_METADATA_BYTES, {})
            if not isinstance(recording_state, dict):
                raise ValueError("Recording import state is invalid.")
            recording_state.update(
                activity_count=len(activities), last_import_at=_utc(datetime.now(timezone.utc))
            )
            _write_json(recording_dir, "state.json", recording_state, MAX_METADATA_BYTES)
            next_page = page if stop_offset is not None else following
            next_offset = stop_offset or 0
            complete = next_page is None
            if full_history:
                state["backfill"] = {
                    "next_page": next_page or page,
                    "next_offset": next_offset,
                    "page_size": page_size,
                    "complete": complete,
                }
            state["last_sync_at"] = _utc(datetime.now(timezone.utc))
            state["activity_count"] = sum(
                _owned_record(item, key, str(item.get("source_activity_id")))
                for key, item in activities.items()
                if isinstance(item, dict)
            )
            _write_json(provider, "state.json", state, MAX_METADATA_BYTES)
            result.update(
                complete=complete,
                next_page=next_page,
                next_offset=next_offset,
                has_more=not complete,
            )
            if complete or stop_offset is not None:
                break
            page, offset = following, 0
    return result
