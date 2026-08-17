"""Pure recorded-load CTL/ATL model over published daily TSS totals.

TrainingPeaks' current Help formulas use yesterday + (today - yesterday) / N:
https://help.trainingpeaks.com/hc/en-us/articles/204071884-Fitness-CTL-
https://help.trainingpeaks.com/hc/en-us/articles/204071894-Fatigue-ATL
No provider calls, TSS recalculation, athlete seed, or planned-load allocation.
"""

from __future__ import annotations

import math
import re
from collections import deque
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import Any


METHOD = "ctl_atl_daily_ewma_v1"
CTL_DAYS = 42
ATL_DAYS = 7
MAX_DAILY_ROWS = 100_000
MAX_HISTORY_DAYS = 36_600
MAX_DAILY_TSS = 1_000_000
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")


def _date(value: Any) -> date:
    if not isinstance(value, str) or _DATE.fullmatch(value) is None:
        raise ValueError("Training-load dates must be ISO calendar dates.")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError("Training-load dates must be ISO calendar dates.") from None


def _score(value: Any) -> float | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0 <= value <= MAX_DAILY_TSS
        or not math.isfinite(value)
    ):
        raise ValueError("Daily recorded TSS must be a bounded finite nonnegative number.")
    return float(value)


def _count(value: Any) -> int:
    if value is None:
        return 0
    if type(value) is not int or not 0 <= value <= MAX_DAILY_ROWS:
        raise ValueError("Daily training-load counts must be bounded nonnegative integers.")
    return value


def _daily(totals: Any) -> tuple[float | None, str]:
    if totals is None:
        return None, "no_recording"
    if not isinstance(totals, Mapping):
        raise ValueError("Daily training-load totals must be an object.")
    score = _score(totals.get("estimated_tss"))
    missing = _count(totals.get("estimated_tss_missing_activity_count"))
    partial = _count(
        totals.get(
            "estimated_tss_relevant_partial_activity_count",
            totals.get("estimated_tss_partial_activity_count"),
        )
    )
    relevant = _count(totals.get("estimated_tss_relevant_activity_count"))
    activities = _count(totals.get("activity_count"))
    unclassified = activities > 0 and "estimated_tss_relevant_activity_count" not in totals
    if score is None:
        return None, "missing" if missing or partial or relevant or unclassified else "no_recording"
    return score, "partial" if missing or partial else "complete"


def build_training_load(
    daily_rows: Sequence[Mapping[str, Any]],
    *,
    as_of: date,
    start: date | None = None,
    end: date | None = None,
) -> dict[str, Any]:
    """Densify known history, then filter its display range without reseeding.

    Absent days contribute zero *recorded* load, not a claim of physiological rest.
    Known unscored/partial activities remain disclosed permanently. The zero baseline
    is mathematical initialization, never an estimated athlete fitness value.
    """
    if (
        type(as_of) is not date
        or any(value is not None and type(value) is not date for value in (start, end))
        or (start is not None and end is not None and start > end)
    ):
        raise ValueError("Training-load display date range is invalid.")
    if (
        not isinstance(daily_rows, Sequence)
        or isinstance(daily_rows, (str, bytes))
        or len(daily_rows) > MAX_DAILY_ROWS
    ):
        raise ValueError("Daily training-load history exceeds its limits.")
    history: dict[date, tuple[float | None, str]] = {}
    seen = set()
    for item in daily_rows:
        if not isinstance(item, Mapping):
            raise ValueError("Daily training-load rows must be objects.")
        day = _date(item.get("date"))
        if day in seen:
            raise ValueError("Daily training-load dates must be unique.")
        seen.add(day)
        if day <= as_of:
            history[day] = _daily(item.get("totals"))
    scored_dates = [day for day, (score, _) in history.items() if score is not None]
    first = min(scored_dates) if scored_dates else None
    prior_missing = sorted(
        day
        for day, (_, status) in history.items()
        if status == "missing" and (first is None or day < first)
    )
    summary = {
        "available": first is not None,
        "scored_days": 0,
        "incomplete_days": 0,
        "missing_score_days": 0,
        "no_recording_days": 0,
        "prior_unscored_days": len(prior_missing),
        "history_incomplete": bool(prior_missing),
    }
    result = {
        "method": METHOD,
        "time_constants": {"ctl": CTL_DAYS, "atl": ATL_DAYS},
        "initialization": "zero_before_first_scored_day",
        "history_start": first.isoformat() if first else None,
        "through_date": as_of.isoformat(),
        "rows": [],
        "summary": summary,
    }
    if first is None:
        return result
    length = (as_of - first).days + 1
    if length > MAX_HISTORY_DAYS:
        raise ValueError("Recorded training-load history exceeds the supported calendar span.")
    recent42 = deque(day for day in prior_missing if (first - day).days < CTL_DAYS)
    recent7 = deque(day for day in prior_missing if (first - day).days < ATL_DAYS)
    ctl = atl = 0.0
    seed_ctl = seed_atl = 1.0
    for offset in range(length):
        day = first + timedelta(days=offset)
        score, status = history.get(day, (None, "no_recording"))
        applied = score if score is not None else 0.0
        tsb = ctl - atl
        ctl += (applied - ctl) / CTL_DAYS
        atl += (applied - atl) / ATL_DAYS
        seed_ctl *= (CTL_DAYS - 1) / CTL_DAYS
        seed_atl *= (ATL_DAYS - 1) / ATL_DAYS
        if score is not None:
            summary["scored_days"] += 1
        if status in {"partial", "missing"}:
            summary["incomplete_days"] += 1
            summary["history_incomplete"] = True
            recent42.append(day)
            recent7.append(day)
        if status == "missing":
            summary["missing_score_days"] += 1
        elif status == "no_recording":
            summary["no_recording_days"] += 1
        while recent42 and (day - recent42[0]).days >= CTL_DAYS:
            recent42.popleft()
        while recent7 and (day - recent7[0]).days >= ATL_DAYS:
            recent7.popleft()
        if (start is None or day >= start) and (end is None or day <= end):
            result["rows"].append(
                {
                    "date": day.isoformat(),
                    "tss_observed": score if status != "no_recording" else 0.0,
                    "load_applied": applied,
                    "ctl": round(ctl, 6),
                    "atl": round(atl, 6),
                    "tsb": round(tsb, 6),
                    "day_status": status,
                    "history_days": offset + 1,
                    "seed_weight_ctl": round(seed_ctl, 6),
                    "seed_weight_atl": round(seed_atl, 6),
                    "history_incomplete": summary["history_incomplete"],
                    "recent_incomplete_days_42": len(recent42),
                    "recent_incomplete_days_7": len(recent7),
                    "to_date": day == as_of,
                }
            )
    return result
