from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

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
    if not value:
        return None, None
    cleaned = value.lower()
    cleaned = cleaned.replace("hours", "").replace("hour", "")
    cleaned = cleaned.replace("hrs", "").replace("hr", "")
    cleaned = cleaned.replace("h", "")
    cleaned = cleaned.replace("–", "-").replace("—", "-")
    numbers = re.findall(r"\d+(?:\.\d+)?", cleaned)
    if not numbers:
        return None, None
    if len(numbers) == 1:
        num = float(numbers[0])
        return num, num
    return float(numbers[0]), float(numbers[1])


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

    duration_column = _column_name(headers, "Duration", "Planned Duration", "Duration (min)")
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
        notes = data.get(notes_column, "").strip() if notes_column else ""
        description = f"{workout} ({duration})" if duration else workout
        if notes and notes != workout:
            description = f"{description} — {notes}"
        weekday = WEEKDAYS[workout_day.weekday()]
        existing = str(week.plan.get(weekday) or "").strip()
        week.plan[weekday] = f"{existing}\n{description}".strip()

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


def build_plan_from_csv(csv_path: Path, output_dir: Path) -> Dict[str, Any]:
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
        week_entries.append(
            {
                "id": week.start_date,
                "range_label": week.range_label,
                "start_date": week.start_date,
                "end_date": week.end_date,
                "phase": week.plan.get("Phase"),
                "primary_focus": week.plan.get("Primary Focus"),
                "hours_target": {"min": hours_min, "max": hours_max},
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

    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "athlete.json", athlete)
    write_json(output_dir / "events.json", event_entries)
    write_json(output_dir / "weeks.json", week_entries)
    write_json(output_dir / "phases.json", phases)
    write_json(output_dir / "legend.json", legend)

    return {
        "weeks": len(week_entries),
        "events": len(event_entries),
        "phases": len(phases),
        "output_dir": str(output_dir),
    }
