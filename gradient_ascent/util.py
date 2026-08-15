from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Iterable


def parse_date(value: str) -> date:
    try:
        return datetime.fromisoformat(value).date()
    except ValueError as exc:
        raise ValueError(f"Invalid date: {value}. Use YYYY-MM-DD.") from exc


def date_range(end_date: date, days: int) -> Iterable[date]:
    for offset in range(days):
        yield end_date - timedelta(days=offset)


def to_epoch_seconds(value: date | datetime) -> int:
    if isinstance(value, date) and not isinstance(value, datetime):
        value = datetime.combine(value, datetime.min.time())
    return int(value.timestamp())
