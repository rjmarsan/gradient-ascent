from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, Iterable
from xml.etree import ElementTree

import fitdecode


SUPPORTED_RECORDING_FORMATS = frozenset({"fit", "tcx", "gpx"})
RECORDING_STREAM_VERSION = 2
SEMICIRCLES_TO_DEGREES = 180.0 / (2**31)


def recording_format(filename: str) -> str | None:
    path = Path(str(filename or "").lower())
    if path.suffix == ".gz":
        path = path.with_suffix("")
    value = path.suffix.removeprefix(".")
    return value if value in SUPPORTED_RECORDING_FORMATS else None


def parse_activity_recording(handle: BinaryIO, filename: str) -> dict[str, Any]:
    format_name = recording_format(filename)
    session = {}
    if format_name == "fit":
        points, laps, session = _parse_fit(handle)
    elif format_name == "tcx":
        points, laps = _parse_tcx(handle)
    elif format_name == "gpx":
        points, laps = _parse_gpx(handle)
    else:
        raise ValueError(f"Unsupported activity recording format: {filename}")

    streams = _stream_payload(points, format_name)
    if not streams["streams"]:
        raise ValueError(f"No timestamped stream samples found in {format_name.upper()} recording")
    return {
        "format": format_name,
        "streams": streams,
        "laps": {"laps": laps, "source": "strava_archive_recording", "format": format_name},
        "summary": {**_recording_summary(points, laps), **session},
    }


def _recording_summary(
    points: list[dict[str, Any]],
    laps: list[dict[str, Any]],
) -> dict[str, Any]:
    timed = [point for point in points if isinstance(point.get("timestamp"), datetime)]
    first_time = timed[0]["timestamp"] if timed else None
    last_time = timed[-1]["timestamp"] if timed else None
    elapsed_time = (
        max(0.0, (last_time - first_time).total_seconds())
        if first_time is not None and last_time is not None
        else None
    )
    moving_time = 0.0
    for previous, current in zip(timed, timed[1:]):
        delta = (current["timestamp"] - previous["timestamp"]).total_seconds()
        if delta <= 0:
            continue
        explicit = current.get("moving")
        speed = _safe_float(current.get("velocity_smooth"))
        if bool(explicit) if explicit is not None else speed is None or speed > 0.3:
            moving_time += delta
    moving_duration = moving_time if len(timed) >= 2 else elapsed_time

    distances = [_safe_float(point.get("distance")) for point in points]
    numeric_distances = [value for value in distances if value is not None]
    altitudes = [_safe_float(point.get("altitude")) for point in timed]
    elevation_segments = [
        (previous, current)
        for previous, current in zip(altitudes, altitudes[1:])
        if previous is not None and current is not None
    ]
    elevation_gain = sum(
        max(0.0, current - previous)
        for previous, current in elevation_segments
    ) if elevation_segments else None
    average_watts = _average(points, "watts")
    return _clean_mapping(
        {
            "start_date": _render_timestamp(first_time),
            "elapsed_time": elapsed_time,
            "moving_time": moving_duration,
            "distance": max(numeric_distances) if numeric_distances else None,
            "total_elevation_gain": elevation_gain,
            "average_heartrate": _average(points, "heartrate"),
            "max_heartrate": _maximum(points, "heartrate"),
            "average_watts": average_watts,
            "max_watts": _maximum(points, "watts"),
            "average_cadence": _average(points, "cadence"),
            "average_temp": _average(points, "temp"),
            "kilojoules": (
                average_watts * moving_duration / 1000.0
                if average_watts is not None and moving_duration is not None
                else None
            ),
        }
    )


def _first_value(values: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = values.get(key)
        if value is not None:
            return value
    return None


def _safe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def _safe_int(value: Any) -> int | None:
    numeric = _safe_float(value)
    return int(numeric) if numeric is not None else None


def _coalesce(*values: Any) -> Any:
    return next((value for value in values if value is not None), None)


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if value in (None, ""):
        return None
    try:
        return datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def _degrees(value: Any) -> float | None:
    numeric = _safe_float(value)
    if numeric is None:
        return None
    if abs(numeric) > 180:
        return numeric * SEMICIRCLES_TO_DEGREES
    return numeric


def _fit_values(frame: fitdecode.FitDataMessage) -> dict[str, Any]:
    return {
        field.name: field.value
        for field in frame.fields
        if field.name and field.value is not None
    }


def _fit_point(values: dict[str, Any]) -> dict[str, Any]:
    latitude = _degrees(values.get("position_lat"))
    longitude = _degrees(values.get("position_long"))
    return {
        "timestamp": _timestamp(values.get("timestamp")),
        "latlng": [latitude, longitude] if latitude is not None and longitude is not None else None,
        "altitude": _safe_float(_first_value(values, "enhanced_altitude", "altitude")),
        "distance": _safe_float(values.get("distance")),
        "velocity_smooth": _safe_float(_first_value(values, "enhanced_speed", "speed")),
        "heartrate": _safe_float(values.get("heart_rate")),
        "cadence": _safe_float(values.get("cadence")),
        "temp": _safe_float(values.get("temperature")),
        "watts": _safe_float(values.get("power")),
        "grade_smooth": _safe_float(values.get("grade")),
    }


def _fit_lap(values: dict[str, Any], index: int) -> dict[str, Any]:
    message_index = _safe_int(values.get("message_index"))
    return _clean_mapping(
        {
            "lap_index": (message_index + 1) if message_index is not None else index,
            "start_date": _render_timestamp(values.get("start_time")),
            "elapsed_time": _safe_float(values.get("total_elapsed_time")),
            "moving_time": _safe_float(values.get("total_timer_time")),
            "distance": _safe_float(values.get("total_distance")),
            "average_speed": _safe_float(_first_value(values, "enhanced_avg_speed", "avg_speed")),
            "max_speed": _safe_float(_first_value(values, "enhanced_max_speed", "max_speed")),
            "average_watts": _safe_float(values.get("avg_power")),
            "weighted_average_watts": _safe_float(values.get("normalized_power")),
            "max_watts": _safe_float(values.get("max_power")),
            "average_heartrate": _safe_float(values.get("avg_heart_rate")),
            "max_heartrate": _safe_float(values.get("max_heart_rate")),
            "average_cadence": _safe_float(values.get("avg_cadence")),
            "max_cadence": _safe_float(values.get("max_cadence")),
            "total_elevation_gain": _safe_float(values.get("total_ascent")),
        }
    )


def _fit_session(values: dict[str, Any]) -> dict[str, Any]:
    """Only a single device session can supply whole-activity power metrics."""
    result = {}
    for source, target, maximum in (
        ("normalized_power", "weighted_average_watts", 10000),
        ("training_stress_score", "estimated_tss", 100000),
        ("intensity_factor", "intensity_factor", 100),
        ("total_timer_time", "timer_time", 31 * 86400),
        ("total_elapsed_time", "elapsed_time", 31 * 86400),
    ):
        raw = values.get(source)
        value = _safe_float(raw)
        if not isinstance(raw, bool) and value is not None and math.isfinite(value) and 0 <= value <= maximum:
            result[target] = value
    if result.get("timer_time", 0) > result.get("elapsed_time", 31 * 86400):
        result.pop("timer_time", None)
    if "weighted_average_watts" in result:
        result["weighted_average_watts_source"] = "fit_session"
    return result


def _parse_fit(handle: BinaryIO) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    points: list[dict[str, Any]] = []
    laps: list[dict[str, Any]] = []
    sessions: list[dict[str, Any]] = []
    timer_events: list[tuple[datetime, bool]] = []
    with fitdecode.FitReader(
        handle,
        check_crc=fitdecode.CrcCheck.WARN,
        error_handling=fitdecode.ErrorHandling.WARN,
    ) as reader:
        for frame in reader:
            if frame.frame_type != fitdecode.FIT_FRAME_DATA:
                continue
            if frame.name == "record":
                points.append(_fit_point(_fit_values(frame)))
            elif frame.name == "lap":
                laps.append(_fit_lap(_fit_values(frame), len(laps) + 1))
            elif frame.name == "session":
                sessions.append(_fit_session(_fit_values(frame)))
            elif frame.name == "event":
                values = _fit_values(frame)
                timestamp = _timestamp(values.get("timestamp"))
                event_type = values.get("event_type")
                if values.get("event") in ("timer", 0) and timestamp is not None:
                    if event_type in ("start", 0):
                        timer_events.append((timestamp, True))
                    elif event_type in ("stop", "stop_all", "stop_disable", "stop_disable_all", 1, 4, 8, 9):
                        timer_events.append((timestamp, False))
    if timer_events:
        timer_events.sort(key=lambda event: event[0])
        cursor = 0
        active = None
        for point in points:
            timestamp = point.get("timestamp")
            if timestamp is None:
                continue
            while cursor < len(timer_events) and timer_events[cursor][0] <= timestamp:
                active = timer_events[cursor][1]
                cursor += 1
            point["timer_active"] = active
    return points, laps, sessions[0] if len(sessions) == 1 else {}


def _local_name(tag: str) -> str:
    return str(tag).rsplit("}", 1)[-1].lower()


def _descendant_text(element: ElementTree.Element, *names: str) -> str | None:
    wanted = {name.lower() for name in names}
    for child in element.iter():
        if _local_name(child.tag) not in wanted:
            continue
        if child.text and child.text.strip():
            return child.text.strip()
        for value_child in child.iter():
            if (
                _local_name(value_child.tag) == "value"
                and value_child.text
                and value_child.text.strip()
            ):
                return value_child.text.strip()
    return None


def _child_text(element: ElementTree.Element, *names: str) -> str | None:
    wanted = {name.lower() for name in names}
    for child in element:
        if _local_name(child.tag) not in wanted:
            continue
        if child.text and child.text.strip():
            return child.text.strip()
        return _descendant_text(child, "Value")
    return None


def _lap_extension_text(element: ElementTree.Element, *names: str) -> str | None:
    for child in element:
        if _local_name(child.tag) == "extensions":
            return _descendant_text(child, *names)
    return None


def _xml_point(element: ElementTree.Element) -> dict[str, Any]:
    latitude = _safe_float(_descendant_text(element, "LatitudeDegrees"))
    longitude = _safe_float(_descendant_text(element, "LongitudeDegrees"))
    return {
        "timestamp": _timestamp(_descendant_text(element, "Time")),
        "latlng": [latitude, longitude] if latitude is not None and longitude is not None else None,
        "altitude": _safe_float(_descendant_text(element, "AltitudeMeters", "ele")),
        "distance": _safe_float(_descendant_text(element, "DistanceMeters")),
        "velocity_smooth": _safe_float(_descendant_text(element, "Speed")),
        "heartrate": _safe_float(_descendant_text(element, "HeartRateBpm", "hr", "heartrate")),
        "cadence": _safe_float(_descendant_text(element, "Cadence", "RunCadence", "cad")),
        "temp": _safe_float(_descendant_text(element, "Temperature", "atemp", "temp")),
        "watts": _safe_float(_descendant_text(element, "Watts", "Power", "power")),
        "grade_smooth": _safe_float(_descendant_text(element, "Grade")),
    }


def _tcx_lap(element: ElementTree.Element, points: list[dict[str, Any]], index: int) -> dict[str, Any]:
    start_time = element.attrib.get("StartTime") or _child_text(element, "StartTime")
    return _clean_mapping(
        {
            "lap_index": index,
            "start_date": _render_timestamp(start_time),
            "elapsed_time": _safe_float(_child_text(element, "TotalTimeSeconds")),
            "moving_time": _safe_float(_child_text(element, "TotalTimeSeconds")),
            "distance": _safe_float(_child_text(element, "DistanceMeters")),
            "average_speed": _safe_float(_lap_extension_text(element, "AvgSpeed")),
            "max_speed": _coalesce(
                _safe_float(_child_text(element, "MaximumSpeed")),
                _safe_float(_lap_extension_text(element, "MaxSpeed")),
            ),
            "average_watts": _coalesce(
                _safe_float(_lap_extension_text(element, "AvgWatts")),
                _average(points, "watts"),
            ),
            "weighted_average_watts": _safe_float(
                _lap_extension_text(element, "NormalizedPower", "NormalizedWatts")
            ),
            "max_watts": _coalesce(
                _safe_float(_lap_extension_text(element, "MaxWatts")),
                _maximum(points, "watts"),
            ),
            "average_heartrate": _coalesce(
                _safe_float(_child_text(element, "AverageHeartRateBpm")),
                _average(points, "heartrate"),
            ),
            "max_heartrate": _coalesce(
                _safe_float(_child_text(element, "MaximumHeartRateBpm")),
                _maximum(points, "heartrate"),
            ),
            "average_cadence": _coalesce(
                _safe_float(_child_text(element, "Cadence")),
                _average(points, "cadence"),
            ),
            "max_cadence": _coalesce(
                _safe_float(_lap_extension_text(element, "MaxBikeCadence", "MaxCadence")),
                _maximum(points, "cadence"),
            ),
        }
    )


def _parse_tcx(handle: BinaryIO) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    root = ElementTree.parse(handle).getroot()
    lap_elements = [element for element in root.iter() if _local_name(element.tag) == "lap"]
    points: list[dict[str, Any]] = []
    laps: list[dict[str, Any]] = []
    if lap_elements:
        for index, lap_element in enumerate(lap_elements, start=1):
            lap_points = [
                _xml_point(element)
                for element in lap_element.iter()
                if _local_name(element.tag) == "trackpoint"
            ]
            points.extend(lap_points)
            laps.append(_tcx_lap(lap_element, lap_points, index))
    else:
        points = [
            _xml_point(element)
            for element in root.iter()
            if _local_name(element.tag) == "trackpoint"
        ]
    return points, laps


def _gpx_point(element: ElementTree.Element) -> dict[str, Any]:
    latitude = _safe_float(element.attrib.get("lat"))
    longitude = _safe_float(element.attrib.get("lon"))
    point = _xml_point(element)
    point["latlng"] = (
        [latitude, longitude] if latitude is not None and longitude is not None else None
    )
    return point


def _parse_gpx(handle: BinaryIO) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    root = ElementTree.parse(handle).getroot()
    points = [
        _gpx_point(element)
        for element in root.iter()
        if _local_name(element.tag) == "trkpt"
    ]
    _add_gpx_distance_and_speed(points)
    return points, []


def _add_gpx_distance_and_speed(points: list[dict[str, Any]]) -> None:
    distance = 0.0
    previous: dict[str, Any] | None = None
    for point in points:
        if previous is not None and previous.get("latlng") and point.get("latlng"):
            distance += _haversine_m(previous["latlng"], point["latlng"])
        point["distance"] = distance
        if point.get("velocity_smooth") is None and previous is not None:
            previous_time = previous.get("timestamp")
            current_time = point.get("timestamp")
            if isinstance(previous_time, datetime) and isinstance(current_time, datetime):
                elapsed = (current_time - previous_time).total_seconds()
                if elapsed > 0:
                    point["velocity_smooth"] = (
                        distance - float(previous.get("distance") or 0.0)
                    ) / elapsed
        previous = point


def _haversine_m(start: list[float], end: list[float]) -> float:
    lat1, lon1 = map(math.radians, start)
    lat2, lon2 = map(math.radians, end)
    delta_lat = lat2 - lat1
    delta_lon = lon2 - lon1
    value = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    )
    return 6_371_000.0 * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))


def _stream_payload(points: list[dict[str, Any]], format_name: str) -> dict[str, Any]:
    timed = [point for point in points if isinstance(point.get("timestamp"), datetime)]
    if not timed:
        return {"streams": [], "source": "strava_archive_recording", "format": format_name}

    # Sensor absence is represented by None on the shared timeline. Filtering
    # on another sensor would silently discard valid power (or HR) samples.
    aligned: list[dict[str, Any]] = []
    for point in timed:
        if aligned and aligned[-1]["timestamp"] == point["timestamp"] and all(
            value is None or aligned[-1].get(key) is None or aligned[-1][key] == value
            for key, value in point.items()
        ):
            aligned[-1].update({key: value for key, value in point.items() if value is not None})
        else:
            # Conflicting same-time values remain visible and make the power
            # estimator fail closed; complementary sensor frames lose nothing.
            aligned.append(dict(point))
    timed = aligned
    first_timestamp = timed[0]["timestamp"]
    streams = [
        _stream(
            "time",
            [round((point["timestamp"] - first_timestamp).total_seconds(), 3) for point in timed],
        )
    ]
    for stream_type in (
        "distance",
        "latlng",
        "altitude",
        "velocity_smooth",
        "heartrate",
        "cadence",
        "temp",
        "grade_smooth",
        "watts",
        "timer_active",
    ):
        values = [point.get(stream_type) for point in timed]
        if any(value is not None for value in values):
            streams.append(_stream(stream_type, values))

    moving = []
    for point in timed:
        explicit = point.get("moving")
        speed = _safe_float(point.get("velocity_smooth"))
        moving.append(bool(explicit) if explicit is not None else speed is None or speed > 0.3)
    streams.append(_stream("moving", moving))
    return {
        "streams": streams,
        "version": RECORDING_STREAM_VERSION,
        "source": "strava_archive_recording",
        "format": format_name,
    }


def _stream(stream_type: str, data: list[Any]) -> dict[str, Any]:
    return {
        "type": stream_type,
        "data": data,
        "series_type": "time",
        "original_size": len(data),
        "resolution": "high",
    }


def _average(points: Iterable[dict[str, Any]], key: str) -> float | None:
    values = [_safe_float(point.get(key)) for point in points]
    numeric = [value for value in values if value is not None]
    return sum(numeric) / len(numeric) if numeric else None


def _maximum(points: Iterable[dict[str, Any]], key: str) -> float | None:
    values = [_safe_float(point.get(key)) for point in points]
    numeric = [value for value in values if value is not None]
    return max(numeric) if numeric else None


def _weighted_average(values: Iterable[tuple[float | None, float | None]]) -> float | None:
    weighted = [
        (value, weight)
        for value, weight in values
        if value is not None and weight is not None and weight > 0
    ]
    total_weight = sum(weight for _, weight in weighted)
    if total_weight <= 0:
        return None
    return sum(value * weight for value, weight in weighted) / total_weight


def _render_timestamp(value: Any) -> str | None:
    parsed = _timestamp(value)
    if parsed is None:
        return None
    return parsed.isoformat(timespec="seconds").replace("+00:00", "Z")


def _clean_mapping(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}
