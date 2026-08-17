"""Presentation-only activity title selection; source records are never changed."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime
import re
from typing import Any

_GENERIC = frozenset(
    {
        "ride",
        "private ride",
        "imported ride",
        "untitled",
        "untitled ride",
        "untitled activity",
        "activity",
    }
)
_EMPTY_PLAN = frozenset(
    {"no plan", "no planned session", "no planned workout", "no workout", "unplanned", "none"}
)
_NUMERIC_ID = re.compile(r"[0-9]{6,}\Z")
_RECORDING_SUFFIX = re.compile(r"\.(?:fit|tcx|gpx)(?:\.gz)?\Z", re.IGNORECASE)
_GENERATED_ID = re.compile(
    r"(?:ridewithgps|strava|garmin|recording)[ _:#-]+(?:[0-9]{6,}|[a-f0-9]{64})\Z", re.IGNORECASE
)
_SOURCE_URL = re.compile(
    r"https?://(?:www\.)?(?:ridewithgps\.com/trips|strava\.com/activities|connect\.garmin\.com/modern/activity)/[1-9][0-9]*(?:[/?#].*)?\Z",
    re.IGNORECASE,
)
_ISO = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?:[T ][0-9]{2}:[0-9]{2}(?::[0-9]{2}(?:\.[0-9]{1,6})?)?(?:Z|[+-][0-9]{2}:[0-9]{2})?)?\Z"
)
_OTHER_DATES = (
    (re.compile(r"[0-9]{1,2}/[0-9]{1,2}/[0-9]{2}\Z"), "%m/%d/%y"),
    (re.compile(r"[0-9]{1,2}/[0-9]{1,2}/[0-9]{4}\Z"), "%m/%d/%Y"),
    (re.compile(r"[0-9]{1,2}-[0-9]{1,2}-[0-9]{4}\Z"), "%m-%d-%Y"),
    (re.compile(r"[0-9]{4}/[0-9]{1,2}/[0-9]{1,2}\Z"), "%Y/%m/%d"),
)


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if type(value) is int:
        return str(value)
    return ""


def _identifiers(values: Iterable[Any]) -> tuple[str, ...]:
    items = (values,) if isinstance(values, (str, int)) else values
    return tuple(value for item in items if (value := _text(item)))


def _date_title(value: str) -> bool:
    if _ISO.fullmatch(value):
        try:
            if len(value) == 10:
                date.fromisoformat(value)
            else:
                datetime.fromisoformat(value.replace("Z", "+00:00"))
            return True
        except ValueError:
            return False
    for pattern, format_name in _OTHER_DATES:
        if pattern.fullmatch(value):
            try:
                datetime.strptime(value, format_name)
                return True
            except ValueError:
                return False
    return False


def is_placeholder_title(title: Any, *, source_ids: Iterable[Any] = ()) -> bool:
    """Recognize whole generated labels, preserving titles merely containing dates.

    Short numeric names are preserved unless they equal an explicitly supplied
    source identifier. Callers with authored-title provenance should use the
    override in ``select_activity_title`` instead of guessing from its spelling.
    """
    value = _text(title)
    if not value or value.casefold() in _GENERIC:
        return True
    identifiers = _identifiers(source_ids)
    if value in identifiers:
        return True
    if (
        _NUMERIC_ID.fullmatch(value)
        or _GENERATED_ID.fullmatch(value)
        or _SOURCE_URL.fullmatch(value)
    ):
        return True
    if _date_title(value):
        return True
    suffix = _RECORDING_SUFFIX.search(value)
    if suffix:
        stem = value[: suffix.start()]
        return bool(
            stem in identifiers
            or _NUMERIC_ID.fullmatch(stem)
            or _GENERATED_ID.fullmatch(stem)
            or _date_title(stem)
        )
    return False


def select_activity_title(
    title: Any,
    *,
    planned_name: Any = None,
    authored_title: Any = None,
    source_ids: Iterable[Any] = (),
    fallback: str = "Ride",
) -> str:
    """Choose a useful display label without exposing or mutating source metadata.

    Privacy masking belongs to the caller: pass an already-safe title and only an
    authored override that the caller permits displaying. A planned title is a
    presentation fallback, not evidence that the planned workout was completed.
    """
    authored = _text(authored_title)
    if authored:
        return authored
    identifiers = _identifiers(source_ids)
    value = _text(title)
    if not is_placeholder_title(value, source_ids=identifiers):
        return value
    planned = _text(planned_name)
    if planned.casefold() not in _EMPTY_PLAN and not is_placeholder_title(
        planned, source_ids=identifiers
    ):
        return planned
    default = _text(fallback)
    if default and (
        default.casefold() in _GENERIC or not is_placeholder_title(default, source_ids=identifiers)
    ):
        return default
    return "Ride"
