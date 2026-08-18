from __future__ import annotations

import re
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .planned_load import MAX_DAILY_HOURS, MAX_DAILY_TSS, MAX_WEEKLY_HOURS, MAX_WEEKLY_TSS, parse_source_range
from .spreadsheet import iter_sheet_rows
from .storage import read_json, write_json


MARKER_MAP = {
    "★": "team_priority",
    "[team]": "team_priority",
    "💚": "commitment",
    "[commit]": "commitment",
    "🟡": "maybe",
    "[maybe]": "maybe",
    "❌": "skip",
    "[skip]": "skip",
}

DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%y", "%m/%d/%Y")
DATE_TOKEN = r"(?:\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4})"
EVENT_COLUMNS = {
    "Events": "Cycling",
    "Event": "Cycling",
    "Cycling": "Cycling",
    "Road": "Road",
    "Crit": "Crit",
    "Gravel": "Gravel",
    "MTB": "MTB",
    "Mountain Bike": "Mountain Bike",
    "Cyclocross": "Cyclocross",
    "CX": "Cyclocross",
    "Track": "Track",
}
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
WEEKDAY_NAMES = {
    "Mon": "monday",
    "Tue": "tuesday",
    "Wed": "wednesday",
    "Thu": "thursday",
    "Fri": "friday",
    "Sat": "saturday",
    "Sun": "sunday",
}


@dataclass
class WeekRow:
    start_date: str
    end_date: str
    range_label: str
    plan: Dict[str, Any]
    actual: Optional[Dict[str, Any]]
    events: List[str]
    day_loads: Dict[str, Dict[str, Optional[float]]] = dataclass_field(default_factory=dict)


def _unique_headers(headers: List[str]) -> List[str]:
    seen: Dict[str, int] = {}
    result: List[str] = []
    for header in headers:
        base = header.strip() or "column"
        count = seen.get(base, 0)
        seen[base] = count + 1
        if count == 0:
            result.append(base)
        else:
            result.append(f"{base}_{count+1}")
    return result


def _parse_date(value: str) -> Optional[str]:
    value = value.strip()
    if not value:
        return None
    for date_format in DATE_FORMATS:
        try:
            return datetime.strptime(value, date_format).date().isoformat()
        except ValueError:
            continue
    return None


def _parse_range(label: str) -> Tuple[Optional[str], Optional[str]]:
    match = re.fullmatch(
        rf"\s*({DATE_TOKEN})\s*(?:–|—|\bto\b|\s+-\s+)\s*({DATE_TOKEN})\s*",
        label,
        re.IGNORECASE,
    )
    if not match:
        return None, None
    return _parse_date(match.group(1)), _parse_date(match.group(2))


def _slug(value: str) -> str:
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value or "event"


def _parse_hours_target(value: str | None) -> Tuple[Optional[float], Optional[float]]:
    return parse_source_range(value, unit="hours", maximum=MAX_WEEKLY_HOURS)


def _first_value(data: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return None


def _weekday_value(data: Dict[str, Any], weekday: str) -> Any:
    exact = _first_value(data, weekday, WEEKDAY_NAMES[weekday].title())
    if exact not in (None, ""):
        return exact
    short = weekday.lower()
    long = WEEKDAY_NAMES[weekday]
    for key, value in data.items():
        normalized = re.sub(r"\s+", " ", str(key).strip().lower())
        if normalized.startswith(f"{short} (") or normalized.startswith(f"{long} ("):
            if value not in (None, ""):
                return value
    return None


def _column_name(headers: List[str], *names: str) -> Optional[str]:
    by_lower = {header.strip().lower(): header for header in headers}
    for name in names:
        if name.lower() in by_lower:
            return by_lower[name.lower()]
    return None


def _daily_plan_rows(
    headers: List[str],
    rows: Iterable[List[str]],
) -> Optional[List[WeekRow]]:
    date_column = _column_name(headers, "Date", "Workout Date", "Start Date")
    workout_column = _column_name(
        headers,
        "Workout",
        "Workout Name",
        "Title",
        "Session",
        "Description",
    )
    if not date_column or not workout_column:
        return None

    duration_column = _column_name(headers, "Duration", "Planned Duration", "Duration (min)", "Duration (hours)")
    tss_column = _column_name(headers, "Planned TSS", "TSS Planned", "TSS Target", "Training Stress Score Planned")
    phase_column = _column_name(headers, "Phase", "Training Phase")
    focus_column = _column_name(headers, "Focus", "Primary Focus")
    notes_column = _column_name(headers, "Notes", "Description")
    weeks_by_start: Dict[str, WeekRow] = {}

    for row in rows:
        if len(row) < len(headers):
            row = row + [""] * (len(headers) - len(row))
        data = {headers[i]: row[i].strip() for i in range(len(headers))}
        workout_date = _parse_date(data.get(date_column, ""))
        workout = data.get(workout_column, "").strip()
        if not workout_date or not workout:
            continue

        workout_day = datetime.fromisoformat(workout_date).date()
        week_start = workout_day - timedelta(days=workout_day.weekday())
        week_end = week_start + timedelta(days=6)
        start_key = week_start.isoformat()
        if start_key not in weeks_by_start:
            plan: Dict[str, Any] = {
                "Week": f"{start_key} – {week_end.isoformat()}",
                "Phase": data.get(phase_column, "") if phase_column else "",
                "Primary Focus": data.get(focus_column, "") if focus_column else "",
                **{weekday: "" for weekday in WEEKDAYS},
            }
            weeks_by_start[start_key] = WeekRow(
                start_date=start_key,
                end_date=week_end.isoformat(),
                range_label=plan["Week"],
                plan=plan,
                actual=None,
                events=[],
            )

        week = weeks_by_start[start_key]
        if phase_column and not week.plan.get("Phase"):
            week.plan["Phase"] = data.get(phase_column, "")
        if focus_column and not week.plan.get("Primary Focus"):
            week.plan["Primary Focus"] = data.get(focus_column, "")

        duration = data.get(duration_column, "").strip() if duration_column else ""
        duration_unit = "minutes" if duration_column and duration_column.lower() == "duration (min)" else "hours" if duration_column and duration_column.lower() == "duration (hours)" else "duration"
        hours_min, hours_max = parse_source_range(duration, unit=duration_unit, maximum=MAX_DAILY_HOURS)
        tss_min, tss_max = parse_source_range(data.get(tss_column, "") if tss_column else "", unit="tss", maximum=MAX_DAILY_TSS)
        if duration and hours_min is not None and not re.search(r"[a-z:]", duration, re.IGNORECASE):
            if duration_unit == "minutes":
                duration += " min"
            elif duration_unit == "hours":
                duration += " h"
        notes = data.get(notes_column, "").strip() if notes_column else ""
        description = f"{workout} ({duration})" if duration else workout
        if notes and notes != workout:
            description = f"{description} — {notes}"
        weekday = WEEKDAYS[workout_day.weekday()]
        existing = str(week.plan.get(weekday) or "").strip()
        week.plan[weekday] = f"{existing}\n{description}".strip()
        incoming = {"hours_min": hours_min, "hours_max": hours_max, "tss_min": tss_min, "tss_max": tss_max}
        previous = week.day_loads.get(weekday)
        if previous is not None:
            for metric, maximum in (("hours", MAX_DAILY_HOURS), ("tss", MAX_DAILY_TSS)):
                keys = (f"{metric}_min", f"{metric}_max")
                values = [previous.get(key) for key in keys] + [incoming[key] for key in keys]
                if all(value is not None for value in values) and values[1] + values[3] <= maximum:
                    incoming[keys[0]], incoming[keys[1]] = values[0] + values[2], values[1] + values[3]
                else:
                    incoming[keys[0]] = incoming[keys[1]] = None
        week.day_loads[weekday] = incoming

    return [weeks_by_start[key] for key in sorted(weeks_by_start)]


def _parse_event_line(line: str, discipline: str, week_id: str) -> Optional[Dict[str, Any]]:
    original = line
    line = line.strip()
    if not line:
        return None

    # Normalize bullets
    line = line.lstrip("• ")

    markers: Dict[str, bool] = {key: False for key in MARKER_MAP.values()}
    for marker, field in MARKER_MAP.items():
        if marker in line:
            markers[field] = True
            line = line.replace(marker, "")

    date_match = re.search(rf"\b({DATE_TOKEN})\b", line)
    if not date_match:
        return None
    date_str = _parse_date(date_match.group(1))
    if not date_str:
        return None

    remainder = line[date_match.end():].strip()
    remainder = re.sub(r"^\([^)]*\)", "", remainder).strip()
    remainder = remainder.lstrip("-– ")

    name = remainder.strip()
    location = None
    for separator in (" — ", " - "):
        if separator in name:
            parts = name.split(separator)
            name = separator.join(parts[:-1]).strip()
            location = parts[-1].strip()
            break

    event_id = f"{date_str}-{_slug(name)}-{_slug(discipline)}"

    return {
        "id": event_id,
        "date": date_str,
        "name": name,
        "location": location,
        "discipline": discipline,
        "week_id": week_id,
        "markers": markers,
        "raw": original,
    }


def _extract_athlete_profile(meta_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    profile_text = ""
    notes_text = ""
    if meta_rows:
        profile_parts: List[str] = []
        for value in meta_rows[0].values():
            text = str(value or "").strip()
            if text:
                profile_parts.append(text)
        profile_text = " ".join(profile_parts)
        notes_text = meta_rows[0].get("Notes_2", "")

    profile = {
        "profile_text": profile_text,
        "notes": notes_text,
        "ftp_w": None,
        "weight_lb": None,
        "height_in": None,
        "age": None,
        "race_category": None,
        "experience_years": None,
    }

    ftp_match = re.search(r"(\d{2,4})\s*w\s*ftp", profile_text.lower())
    if ftp_match:
        profile["ftp_w"] = int(ftp_match.group(1))

    weight_match = re.search(r"(~?\d{2,3})\s*lb", profile_text.lower())
    if weight_match:
        profile["weight_lb"] = int(weight_match.group(1).replace("~", ""))

    height_match = re.search(r"(\d+)\s*'\s*(\d+)", profile_text)
    if height_match:
        feet = int(height_match.group(1))
        inches = int(height_match.group(2))
        profile["height_in"] = feet * 12 + inches

    age_match = re.search(r"(\d{2})\s*yr", profile_text.lower())
    if age_match:
        profile["age"] = int(age_match.group(1))

    exp_match = re.search(r"(\d+)\s*(?:st|nd|rd|th) year", profile_text.lower())
    if exp_match:
        profile["experience_years"] = int(exp_match.group(1))

    cat_match = re.search(r"cat\s*\d+", profile_text.lower())
    if cat_match:
        profile["race_category"] = cat_match.group(0)

    return profile


def _plan_payloads_from_csv(csv_path: Path, output_dir: Path) -> tuple[dict[str, Any], Dict[str, Any]]:
    reader = iter_sheet_rows(csv_path)
    try:
        headers = next(reader)
    except StopIteration:
        raise ValueError("Calendar input is empty")
    headers = _unique_headers(headers)

    rows = list(reader)
    meta_rows: List[Dict[str, Any]] = []
    daily_weeks = _daily_plan_rows(headers, rows)
    weeks: List[WeekRow] = daily_weeks or []
    last_week: Optional[WeekRow] = None
    events: Dict[str, Dict[str, Any]] = {}

    for row in [] if daily_weeks is not None else rows:
        if len(row) < len(headers):
            row = row + [""] * (len(headers) - len(row))
        data = {headers[i]: row[i].strip() for i in range(len(headers))}
        first_cell = row[0].strip()
        if not first_cell:
            continue
        if first_cell.lower() == "actual":
            if last_week is not None:
                last_week.actual = data
            continue

        start_date, end_date = _parse_range(first_cell)
        if start_date and end_date:
            week_id = start_date
            events_for_week: List[str] = []
            for column, discipline in EVENT_COLUMNS.items():
                cell = data.get(column, "")
                if not cell:
                    continue
                for line in cell.splitlines():
                    event = _parse_event_line(line, discipline, week_id)
                    if not event:
                        continue
                    events[event["id"]] = event
                    events_for_week.append(event["id"])

            week = WeekRow(
                start_date=start_date,
                end_date=end_date,
                range_label=first_cell,
                plan=data,
                actual=None,
                events=events_for_week,
            )
            weeks.append(week)
            last_week = week
        else:
            meta_rows.append(data)

    if not weeks:
        raise ValueError(
            "No workouts were recognized. Use a supported weekly or daily training-plan layout; "
            "see examples/calendar/sample-training-calendar.csv. Existing plan files were not changed."
        )

    imported_athlete = _extract_athlete_profile(meta_rows)
    existing_athlete = read_json(output_dir / "athlete.json", default={}) or {}
    athlete = dict(existing_athlete) if isinstance(existing_athlete, dict) else {}
    for key, value in imported_athlete.items():
        if athlete.get(key) in (None, "", [], {}):
            athlete[key] = value
    existing_events = read_json(output_dir / "events.json", default=[]) or []
    if isinstance(existing_events, list):
        for event in existing_events:
            if not isinstance(event, dict) or not event.get("id"):
                continue
            events.setdefault(str(event["id"]), event)
    legend = {
        "markers": MARKER_MAP,
        "notes": (meta_rows[0].get("Notes_2") if meta_rows else None),
    }

    week_entries: List[Dict[str, Any]] = []
    for week in weeks:
        hours_min, hours_max = _parse_hours_target(
            (week.plan or {}).get("Hours Target")
        )
        tss_column = _column_name(list(week.plan), "TSS Target", "Planned TSS", "Weekly TSS", "Target TSS")
        tss_min, tss_max = parse_source_range(week.plan.get(tss_column) if tss_column else None, unit="tss", maximum=MAX_WEEKLY_TSS)
        week_entries.append(
            {
                "id": week.start_date,
                "range_label": week.range_label,
                "start_date": week.start_date,
                "end_date": week.end_date,
                "phase": week.plan.get("Phase"),
                "primary_focus": week.plan.get("Primary Focus"),
                "hours_target": {"min": hours_min, "max": hours_max},
                "tss_target": {"min": tss_min, "max": tss_max},
                "day_loads": week.day_loads,
                "hours_actual_text": (week.actual or {}).get("Hours actual")
                or week.plan.get("Hours actual"),
                "days": {
                    weekday: _weekday_value(week.plan, weekday) for weekday in WEEKDAYS
                },
                "strength_rehab": _first_value(week.plan, "Strength / Rehab", "Strength/Rehab"),
                "notes": week.plan.get("Notes"),
                "notes_2": week.plan.get("Notes_2"),
                "events": week.events,
                "actual": week.actual,
                "raw": week.plan,
            }
        )

    phases: List[Dict[str, Any]] = []
    current_phase: Optional[Dict[str, Any]] = None
    for week in week_entries:
        phase_name = week.get("phase") or ""
        if current_phase is None or current_phase["name"] != phase_name:
            if current_phase:
                phases.append(current_phase)
            current_phase = {
                "name": phase_name,
                "start_date": week["start_date"],
                "end_date": week["end_date"],
                "weeks": [week["id"]],
            }
        else:
            current_phase["end_date"] = week["end_date"]
            current_phase["weeks"].append(week["id"])

    if current_phase:
        phases.append(current_phase)

    event_entries = sorted(
        events.values(),
        key=lambda event: (str(event.get("date") or ""), str(event.get("name") or "")),
    )

    payloads = {
        "athlete.json": athlete,
        "events.json": event_entries,
        "weeks.json": week_entries,
        "phases.json": phases,
        "legend.json": legend,
    }
    summary = {
        "weeks": len(week_entries),
        "events": len(event_entries),
        "phases": len(phases),
        "output_dir": str(output_dir),
    }
    return payloads, summary


def build_plan_from_csv(
    csv_path: Path,
    output_dir: Path,
    *,
    record_history: bool | None = None,
    history_request: dict[str, Any] | None = None,
    expected_identity: tuple[int, int] | None = None,
) -> Dict[str, Any]:
    """Prepare the whole import before changing any authoritative plan file."""
    from .plan_changes import (
        change_request,
        commit_plan_files,
        file_digest,
        json_bytes,
        scopes_for_dates,
    )
    from . import coaching_history, recording_repair
    from .workspace_lock import workspace_identity, workspace_lock

    output_dir = Path(output_dir)
    official = output_dir.name == "plan" if record_history is None else record_history
    if not official:
        payloads, summary = _plan_payloads_from_csv(csv_path, output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        for name, payload in payloads.items():
            write_json(output_dir / name, payload)
        return summary
    if output_dir.name != "plan":
        raise ValueError("Official plan imports must target the workspace plan directory.")
    data_dir = output_dir.parent
    data_dir.mkdir(parents=True, exist_ok=True)
    identity = expected_identity if expected_identity is not None else workspace_identity(data_dir)
    with workspace_lock(data_dir, expected_identity=identity):
        supported = coaching_history.history_write_available(data_dir)
        filenames = ("athlete.json", "events.json", "weeks.json", "phases.json", "legend.json")
        expected = None
        if supported:
            with recording_repair._directory(data_dir) as root:
                expected = {
                    f"plan/{name}": file_digest(coaching_history._read_target(root, f"plan/{name}"))
                    for name in filenames
                }
                recording_repair._assert_generation(data_dir, root, identity)
        old_weeks = read_json(output_dir / "weeks.json", default=[]) or []
        payloads, summary = _plan_payloads_from_csv(csv_path, output_dir)
        dates = [
            str(week.get(key) or "")
            for week in [
                *(old_weeks if isinstance(old_weeks, list) else []),
                *payloads["weeks.json"],
            ]
            if isinstance(week, dict)
            for key in ("start_date", "end_date")
        ]
        result = commit_plan_files(
            data_dir,
            {f"plan/{name}": json_bytes(payload) for name, payload in payloads.items()},
            request=change_request(
                "import-plan",
                title="Import training plan",
                rationale="Imported a reviewed training-plan file. No additional coaching rationale was supplied.",
                scopes=scopes_for_dates(dates),
                supplied=history_request,
            ),
            expected_identity=identity,
            expected_hashes=expected,
            legacy_fallback=True,
            retry_from_current=True,
            inferred_scopes="scopes" not in (history_request or {}),
        )
        return {**summary, "history": result}
