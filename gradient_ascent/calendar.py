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


def _calendar_payload(calendar_path: Path) -> tuple[dict[str, Any], int]:
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

    return payload, len(meta_rows)


def ingest_calendar(
    calendar_path: Path,
    output_path: Path,
    *,
    record_history: bool | None = None,
    history_request: dict[str, Any] | None = None,
    expected_identity: tuple[int, int] | None = None,
) -> Dict[str, Any]:
    from . import coaching_history, recording_repair
    from .plan_changes import (
        change_request,
        commit_plan_files,
        file_digest,
        json_bytes,
        scopes_for_dates,
    )
    from .workspace_lock import workspace_identity, workspace_lock

    output_path = Path(output_path)
    official = output_path.name == "calendar.json" if record_history is None else record_history
    payload, meta_count = _calendar_payload(calendar_path)
    summary = {"weeks": len(payload["weeks"]), "meta_rows": meta_count, "output": str(output_path)}
    if not official:
        write_json(output_path, payload)
        return summary
    if output_path.name != "calendar.json":
        raise ValueError("Official calendar imports must target workspace calendar.json.")
    data_dir = output_path.parent
    data_dir.mkdir(parents=True, exist_ok=True)
    identity = expected_identity if expected_identity is not None else workspace_identity(data_dir)
    with workspace_lock(data_dir, expected_identity=identity):
        expected = None
        if coaching_history.history_write_available(data_dir):
            with recording_repair._directory(data_dir) as root:
                expected = {
                    "calendar.json": file_digest(
                        coaching_history._read_target(root, "calendar.json")
                    )
                }
                recording_repair._assert_generation(data_dir, root, identity)
        dates = [
            str(week.get(key) or "")
            for week in payload["weeks"]
            for key in ("start_date", "end_date")
        ]
        result = commit_plan_files(
            data_dir,
            {"calendar.json": json_bytes(payload)},
            request=change_request(
                "import-calendar",
                title="Import source calendar",
                rationale="Imported a reviewed source calendar. No additional coaching rationale was supplied.",
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
