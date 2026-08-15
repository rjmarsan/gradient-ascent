from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .spreadsheet import iter_sheet_rows
from .storage import write_json


DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%y", "%m/%d/%Y")
DATE_TOKEN = r"(?:\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4})"


@dataclass
class WeekEntry:
    range_label: str
    start_date: Optional[str]
    end_date: Optional[str]
    plan: Dict[str, Any]
    actual: Optional[Dict[str, Any]]


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


def ingest_calendar(calendar_path: Path, output_path: Path) -> Dict[str, Any]:
    reader = iter_sheet_rows(calendar_path)
    try:
        headers = next(reader)
    except StopIteration:
        raise ValueError("Calendar input is empty")
    headers = _unique_headers(headers)

    meta_rows: List[Dict[str, Any]] = []
    weeks: List[WeekEntry] = []
    last_week: Optional[WeekEntry] = None

    for row in reader:
        # Pad short rows
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
        if start_date or end_date:
            entry = WeekEntry(
                range_label=first_cell,
                start_date=start_date,
                end_date=end_date,
                plan=data,
                actual=None,
            )
            weeks.append(entry)
            last_week = entry
        else:
            meta_rows.append(data)

    if not weeks:
        raise ValueError(
            "No weeks were recognized. Use the supported weekly calendar layout; "
            "see examples/calendar/sample-training-calendar.csv. Existing calendar data was not changed."
        )

    payload = {
        "source": str(calendar_path),
        "meta": meta_rows,
        "weeks": [
            {
                "range_label": week.range_label,
                "start_date": week.start_date,
                "end_date": week.end_date,
                "plan": week.plan,
                "actual": week.actual,
            }
            for week in weeks
        ],
    }

    write_json(output_path, payload)
    return {
        "weeks": len(weeks),
        "meta_rows": len(meta_rows),
        "output": str(output_path),
    }
