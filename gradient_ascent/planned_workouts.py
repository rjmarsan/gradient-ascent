"""Read explicit workout prescriptions and export a local planned calendar.

Weekday prose is never interpreted as device instructions. The optional structured
file is independent: revising/importing a weekly plan does not replace its entries.
No source file, provider account, or device is modified by this module.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import stat
from collections.abc import Iterable, Mapping
from datetime import date, timedelta
from pathlib import Path
from typing import Any


MAX_PLAN_BYTES = 8 * 1024 * 1024
MAX_ENTRIES = 10_000
MAX_STRUCTURED_WORKOUTS = 5_000
MAX_STEPS = 50
MAX_DURATION_SECONDS = 86_400
MAX_NAME_BYTES = 254
MAX_DESCRIPTION_BYTES = 64 * 1024
_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,79}\Z")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_INTENSITIES = {"warmup", "active", "recovery", "cooldown", "rest"}
_SOURCES = {"plan/weeks.json", "plan/events.json", "plan/workouts.json"}
_STATUSES = {"confirmed", "tentative", "cancelled"}
_CSV_FIELDS = (
    "id",
    "date",
    "name",
    "description",
    "sport",
    "structured",
    "status",
    "location",
    "priority",
    "source",
)


def _integer(value: Any, minimum: int, maximum: int, label: str) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{label} must be an integer from {minimum} to {maximum}.")
    return value


def _text(value: Any, limit: int, label: str, *, required: bool = False) -> str:
    if not isinstance(value, str) or "\0" in value or (required and not value.strip()):
        raise ValueError(f"{label} must be valid text.")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        raise ValueError(f"{label} must be valid UTF-8 text.") from None
    if len(encoded) > limit or any(
        (ord(char) < 32 and char not in "\t\r\n") or ord(char) == 127 for char in value
    ):
        raise ValueError(f"{label} exceeds its text limits.")
    return value


def _day(value: Any) -> date:
    if not isinstance(value, str) or _DATE.fullmatch(value) is None:
        raise ValueError("Workout dates must be ISO calendar dates.")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError("Workout dates must be ISO calendar dates.") from None


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ValueError("Workout ids must be unique lowercase slugs of at most 80 characters.")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Planned workout JSON contains duplicate keys.")
        result[key] = value
    return result


def _read_optional(data_dir: Path, filename: str, default: Any) -> Any:
    """Read only a fixed direct plan child, rejecting symlinks and special files."""
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise RuntimeError("This platform cannot safely read planned workout files.")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    root = plan = descriptor = -1
    try:
        root = os.open(data_dir, flags)
        try:
            plan = os.open("plan", flags, dir_fd=root)
            expected = os.stat(filename, dir_fd=plan, follow_symlinks=False)
        except FileNotFoundError:
            return default
        if not stat.S_ISREG(expected.st_mode) or expected.st_nlink != 1:
            raise ValueError("Planned workout sources must be regular non-symlink files.")
        if expected.st_size > MAX_PLAN_BYTES:
            raise ValueError("Planned workout source exceeds its size limit.")
        descriptor = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=plan)
        actual = os.fstat(descriptor)
        if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
            raise ValueError("Planned workout source changed while opening.")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            body = handle.read(MAX_PLAN_BYTES + 1)
        if len(body) > MAX_PLAN_BYTES:
            raise ValueError("Planned workout source exceeds its size limit.")
        return json.loads(body, object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Could not safely read planned workout source.") from exc
    finally:
        for handle in (descriptor, plan, root):
            if handle >= 0:
                os.close(handle)


def _target(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Every workout step requires an explicit target.")
    if value == {"type": "open"}:
        return {"type": "open"}
    if set(value) != {"type", "unit", "low", "high"} or value.get("type") != "power":
        raise ValueError("Workout targets must be open or explicit power ranges.")
    unit = value["unit"]
    if unit == "percent_ftp":
        minimum, maximum = 0, 300
    elif unit == "watts":
        minimum, maximum = 1, 3000
    else:
        raise ValueError("Power targets must use percent_ftp or watts.")
    low = _integer(value["low"], minimum, maximum, "Power target low")
    high = _integer(value["high"], minimum, maximum, "Power target high")
    if low > high:
        raise ValueError("Power target low must not exceed high.")
    return {"type": "power", "unit": unit, "low": low, "high": high}


def _step(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"name", "duration_s", "intensity", "target"}:
        raise ValueError("Workout steps must use the explicit supported fields.")
    intensity = value["intensity"]
    if not isinstance(intensity, str) or intensity not in _INTENSITIES:
        raise ValueError("Unsupported workout step intensity.")
    return {
        "name": _text(value["name"], MAX_NAME_BYTES, "Step name"),
        "duration_s": _integer(value["duration_s"], 1, MAX_DURATION_SECONDS, "Step duration"),
        "intensity": intensity,
        "target": _target(value["target"]),
    }


def _steps(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_STEPS:
        raise ValueError("Structured workouts require 1 to 50 steps.")
    result: list[dict[str, Any]] = []
    for entry in value:
        if isinstance(entry, dict) and "repeat" in entry:
            if set(entry) != {"repeat", "steps"}:
                raise ValueError("Repeat groups require only repeat and steps.")
            count = _integer(entry["repeat"], 1, MAX_STEPS, "Repeat count")
            children = entry["steps"]
            if not isinstance(children, list) or not 1 <= len(children) <= MAX_STEPS:
                raise ValueError("Repeat groups require explicit simple steps.")
            simple = [_step(child) for child in children]
            if len(result) + count * len(simple) > MAX_STEPS:
                raise ValueError("A workout cannot exceed 50 flattened steps.")
            result.extend(
                {**child, "target": dict(child["target"])} for _ in range(count) for child in simple
            )
        else:
            result.append(_step(entry))
        if len(result) > MAX_STEPS:
            raise ValueError("A workout cannot exceed 50 flattened steps.")
    if sum(entry["duration_s"] for entry in result) > MAX_DURATION_SECONDS:
        raise ValueError("A workout cannot exceed 24 hours.")
    return result


def _structured(value: Any) -> dict[str, Any]:
    required = {"id", "date", "name", "sport", "steps"}
    optional = {"description", "device_description"}
    if (
        not isinstance(value, dict)
        or not required <= set(value)
        or set(value) - required - optional
    ):
        raise ValueError("Structured workout fields do not match the supported schema.")
    if value["sport"] != "cycling":
        raise ValueError("Structured workouts currently support cycling only.")
    return {
        "id": _identifier(value["id"]),
        "date": _day(value["date"]).isoformat(),
        "name": _text(value["name"], MAX_NAME_BYTES, "Workout name", required=True),
        "description": _text(
            value.get("description", ""), MAX_DESCRIPTION_BYTES, "Workout description"
        ),
        "device_description": _text(
            value.get("device_description", ""), MAX_NAME_BYTES, "Device description"
        ),
        "sport": "cycling",
        "steps": _steps(value["steps"]),
        "structured": True,
        "source": "plan/workouts.json",
    }


def _legacy(weeks: Any) -> list[dict[str, Any]]:
    if not isinstance(weeks, list) or len(weeks) > MAX_ENTRIES // 7:
        raise ValueError("Weekly plan exceeds its supported limits.")
    result = []
    for week in weeks:
        if not isinstance(week, dict) or not isinstance(week.get("days", {}), dict):
            raise ValueError("Weekly plan has an invalid calendar entry.")
        start = _day(week.get("start_date"))
        try:
            span_end = start + timedelta(days=6)
        except OverflowError:
            raise ValueError("Weekly plan exceeds the calendar date range.") from None
        end = _day(week["end_date"]) if week.get("end_date") not in (None, "") else span_end
        if end < start:
            raise ValueError("Weekly plan end precedes its start.")
        end = min(end, span_end)
        for offset, weekday in enumerate(_DAYS):
            scheduled = start + timedelta(days=(offset - start.weekday()) % 7)
            if scheduled > end:
                continue
            prose = week.get("days", {}).get(weekday)
            if prose is None or prose == "":
                continue
            prose = _text(prose, MAX_DESCRIPTION_BYTES, "Weekly plan description").strip()
            if not prose:
                continue
            title = prose.splitlines()[0].encode("utf-8")[:MAX_NAME_BYTES].decode("utf-8", "ignore")
            result.append(
                {
                    "id": f"week-{start.isoformat()}-{weekday.lower()}",
                    "date": scheduled.isoformat(),
                    "name": title,
                    "description": prose,
                    "device_description": "",
                    "sport": "unspecified",
                    "steps": [],
                    "structured": False,
                    "source": "plan/weeks.json",
                }
            )
    return result


def _event_status(event: dict[str, Any], markers: dict[str, Any]) -> str:
    status = event.get("status")
    if status is not None and not isinstance(status, str):
        raise ValueError("Planned event status must be text.")
    status = (status or "").strip().casefold()
    if markers.get("skip") is True or status in {
        "cancelled",
        "canceled",
        "skipped",
        "skip",
        "withdrawn",
    }:
        return "cancelled"
    if markers.get("maybe") is True or status in {"tentative", "maybe", "optional"}:
        return "tentative"
    if markers.get("commitment") is True or status == "confirmed":
        return "confirmed"
    return "tentative"


def _events(events: Any) -> list[dict[str, Any]]:
    if not isinstance(events, list) or len(events) > MAX_ENTRIES:
        raise ValueError("Planned events exceed their supported limits.")
    result = []
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("Planned event must be an object.")
        scheduled = _day(event.get("date")).isoformat()
        name = _text(event.get("name"), MAX_DESCRIPTION_BYTES, "Event name", required=True)
        location_value = event.get("location")
        location = _text(
            "" if location_value is None else location_value,
            MAX_DESCRIPTION_BYTES,
            "Event location",
        )
        markers = event.get("markers")
        if markers is None:
            markers = {}
        if not isinstance(markers, dict):
            raise ValueError("Planned event markers must be an object.")
        priority = event.get("priority")
        if priority is None:
            priority = "team_priority" if markers.get("team_priority") is True else ""
        elif type(priority) is int and 0 <= priority <= 9:
            priority = str(priority)
        priority = _text(priority, MAX_NAME_BYTES, "Event priority")
        source_id = event.get("id")
        if source_id is not None:
            if type(source_id) is int and 0 <= source_id <= 2**63 - 1:
                source_id = str(source_id)
            source_id = _text(source_id, 4096, "Event id", required=True)
            identity = "id:" + source_id
        else:
            discipline = _text(event.get("discipline") or "", MAX_NAME_BYTES, "Event discipline")
            identity = json.dumps([scheduled, name, location, discipline], ensure_ascii=False)
        description = event.get("description") or event.get("notes") or event.get("raw") or name
        result.append(
            {
                "id": "event-" + hashlib.sha256(identity.encode("utf-8")).hexdigest(),
                "date": scheduled,
                "name": name,
                "description": _text(description, MAX_DESCRIPTION_BYTES, "Event description"),
                "device_description": "",
                "sport": "unspecified",
                "steps": [],
                "structured": False,
                "source": "plan/events.json",
                "status": _event_status(event, markers),
                "location": location,
                "priority": priority,
            }
        )
    return result


def load_planned_workouts(
    data_dir: Path, *, start: date | None = None, end: date | None = None
) -> list[dict[str, Any]]:
    """Read independent calendar prose and explicit prescriptions, without writes."""
    if any(value is not None and type(value) is not date for value in (start, end)) or (
        start and end and start > end
    ):
        raise ValueError("Planned workout date range is invalid.")
    root = Path(data_dir).expanduser()
    entries = _legacy(_read_optional(root, "weeks.json", []))
    entries.extend(_events(_read_optional(root, "events.json", [])))
    document = _read_optional(root, "workouts.json", {"version": 1, "workouts": []})
    if (
        not isinstance(document, dict)
        or set(document) != {"version", "workouts"}
        or type(document["version"]) is not int
        or document["version"] != 1
        or not isinstance(document["workouts"], list)
        or len(document["workouts"]) > MAX_STRUCTURED_WORKOUTS
    ):
        raise ValueError("Structured workout file must use the supported version 1 schema.")
    entries.extend(_structured(value) for value in document["workouts"])
    entries = _calendar_entries(entries)
    return [
        entry
        for entry in entries
        if (start is None or _day(entry["date"]) >= start)
        and (end is None or _day(entry["date"]) <= end)
    ]


def _calendar_entries(entries: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result = []
    identifiers = set()
    for entry in entries:
        if len(result) >= MAX_ENTRIES or not isinstance(entry, Mapping):
            raise ValueError("Planned calendar exceeds its supported limits.")
        identifier = _identifier(entry.get("id"))
        if identifier in identifiers:
            raise ValueError("Planned workout ids must be unique across both source files.")
        identifiers.add(identifier)
        _day(entry.get("date"))
        _text(
            entry.get("name"),
            MAX_NAME_BYTES if entry.get("structured") is True else MAX_DESCRIPTION_BYTES,
            "Workout name",
            required=True,
        )
        _text(entry.get("description", ""), MAX_DESCRIPTION_BYTES, "Workout description")
        status = entry.get("status", "confirmed")
        if (
            not isinstance(entry.get("source"), str)
            or entry["source"] not in _SOURCES
            or not isinstance(entry.get("sport"), str)
            or entry["sport"] not in {"cycling", "unspecified"}
            or type(entry.get("structured")) is not bool
            or not isinstance(status, str)
            or status not in _STATUSES
        ):
            raise ValueError("Planned calendar entry is invalid.")
        result.append(
            {
                **entry,
                "status": status,
                "location": _text(
                    entry.get("location", ""), MAX_DESCRIPTION_BYTES, "Event location"
                ),
                "priority": _text(entry.get("priority", ""), MAX_NAME_BYTES, "Event priority"),
            }
        )
    return sorted(result, key=lambda entry: (entry["date"], entry["id"]))


def _csv_cell(value: Any) -> str:
    text = str(value)
    if text[:1] in {"\t", "\r", "\n"} or text.lstrip().startswith(
        ("=", "+", "-", "@", "＝", "＋", "－", "＠")
    ):
        return "'" + text
    return text


def serialize_plan_csv(entries: Iterable[Mapping[str, Any]]) -> bytes:
    """Return a deterministic UTF-8 calendar CSV safe for spreadsheet opening."""
    output = io.StringIO(newline="")
    writer = csv.writer(output, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    writer.writerow(_CSV_FIELDS)
    for entry in _calendar_entries(entries):
        writer.writerow(
            _csv_cell(str(entry[key]).lower() if key == "structured" else entry.get(key, ""))
            for key in _CSV_FIELDS
        )
    return output.getvalue().encode("utf-8")


def _ics_text(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", "\\n")
        .replace(";", "\\;")
        .replace(",", "\\,")
    )


def _fold(line: str) -> bytes:
    lines: list[bytes] = []
    current = bytearray()
    for character in line:
        encoded = character.encode("utf-8")
        if len(current) + len(encoded) > 75:
            lines.append(bytes(current))
            current = bytearray(b" ")
        current.extend(encoded)
    lines.append(bytes(current))
    return b"\r\n".join(lines) + b"\r\n"


def serialize_plan_ics(entries: Iterable[Mapping[str, Any]]) -> bytes:
    """Return a reproducible RFC 5545 all-day snapshot, not calendar synchronization."""
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Gradient Ascent//Planned Workouts//EN",
        "CALSCALE:GREGORIAN",
    ]
    for entry in _calendar_entries(entries):
        scheduled = _day(entry["date"])
        try:
            following = scheduled + timedelta(days=1)
        except OverflowError:
            raise ValueError("Workout date exceeds the calendar export range.") from None
        uid = hashlib.sha256(f"{entry['source']}\0{entry['id']}".encode()).hexdigest()
        scheduled_text = scheduled.isoformat().replace("-", "")
        following_text = following.isoformat().replace("-", "")
        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:{uid}@gradient-ascent",
                f"DTSTAMP:{scheduled_text}T000000Z",
                f"DTSTART;VALUE=DATE:{scheduled_text}",
                f"DTEND;VALUE=DATE:{following_text}",
                f"STATUS:{entry['status'].upper()}",
                f"SUMMARY:{_ics_text(entry['name'])}",
                f"DESCRIPTION:{_ics_text(entry.get('description', ''))}",
            ]
        )
        if entry["location"]:
            lines.append(f"LOCATION:{_ics_text(entry['location'])}")
        if entry["priority"]:
            lines.append(f"X-GRADIENT-ASCENT-PRIORITY:{_ics_text(entry['priority'])}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return b"".join(_fold(line) for line in lines)
