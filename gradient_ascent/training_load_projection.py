"""Pure conditional CTL/ATL projection from explicit dated prescriptions."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import Any

from .planned_load import MAX_DAILY_TSS
from .training_load import (
    ATL_DAYS,
    CTL_DAYS,
    MAX_DAILY_ROWS,
    MAX_HISTORY_DAYS,
    METHOD as RECORDED_METHOD,
    MAX_DAILY_TSS as MAX_RECORDED_TSS,
)


METHOD = "ctl_atl_daily_projection_v1"
MAX_PROJECTION_DAYS = 730
MAX_TARGET_ROWS = 3660
_TARGET_KEYS = frozenset({"date", "target_tss", "tss_source", "status"})
_PRESCRIBED_SOURCES = frozenset(
    {"source_target", "explicit_rest", "structured_power_model", "structured_workout_sum"}
)


def _day(value: Any) -> date:
    if not isinstance(value, str):
        raise ValueError("Projection dates must be ISO calendar dates.")
    try:
        result = date.fromisoformat(value)
    except ValueError:
        raise ValueError("Projection dates must be ISO calendar dates.") from None
    if result.isoformat() != value:
        raise ValueError("Projection dates must be ISO calendar dates.")
    return result


def _number(value: Any, maximum: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0 <= value <= maximum
        or not math.isfinite(value)
    ):
        raise ValueError("Projection values must be bounded finite nonnegative numbers.")
    return float(value)


def _count(value: Any, maximum: int) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError("Projection history counts are invalid.")
    return value


def _targets(values: Any) -> dict[date, dict[str, Any]]:
    if (
        not isinstance(values, Sequence)
        or isinstance(values, (str, bytes))
        or len(values) > MAX_TARGET_ROWS
    ):
        raise ValueError("Projection targets must be a bounded sequence.")
    result = {}
    for value in values:
        if not isinstance(value, Mapping) or set(value) != _TARGET_KEYS:
            raise ValueError("Projection targets require explicit dated TSS and provenance.")
        day = _day(value["date"])
        score = _number(value["target_tss"], MAX_DAILY_TSS)
        source, status = value["tss_source"], value["status"]
        if not isinstance(source, str) or not isinstance(status, str):
            raise ValueError("Projection target provenance is invalid.")
        if source == "coach_budget_allocation":
            valid = status in {"provisional", "confirmed"}
        else:
            valid = source in _PRESCRIBED_SOURCES and status == "prescribed"
        if not valid or (source == "explicit_rest" and score != 0):
            raise ValueError("Projection accepts only explicit current daily prescriptions.")
        if day in result:
            raise ValueError("Projection target dates must be unique.")
        result[day] = {"target_tss": score, "tss_source": source, "status": status}
    return result


def _baseline(model: Any, as_of: date) -> tuple[dict[str, Any] | None, str, str, bool]:
    if not isinstance(model, Mapping) or model.get("method") != RECORDED_METHOD:
        raise ValueError("Projection requires the supported recorded-load model.")
    constants = model.get("time_constants")
    if (
        not isinstance(constants, Mapping)
        or set(constants) != {"ctl", "atl"}
        or type(constants["ctl"]) is not int
        or type(constants["atl"]) is not int
        or constants != {"ctl": CTL_DAYS, "atl": ATL_DAYS}
        or model.get("initialization") != "zero_before_first_scored_day"
    ):
        raise ValueError("Projection recorded-load conventions are unsupported.")
    through = _day(model.get("through_date"))
    summary = model.get("summary")
    if (
        not isinstance(summary, Mapping)
        or type(summary.get("available")) is not bool
        or type(summary.get("history_incomplete")) is not bool
    ):
        raise ValueError("Projection recorded-load summary is invalid.")
    incomplete = summary["history_incomplete"]
    if through != as_of:
        return None, "stale_recorded_model", through.isoformat(), incomplete
    rows = model.get("rows")
    if (
        not isinstance(rows, Sequence)
        or isinstance(rows, (str, bytes))
        or len(rows) > MAX_DAILY_ROWS
    ):
        raise ValueError("Projection recorded-load rows are invalid.")
    anchor = None
    seen = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("Projection recorded-load row is invalid.")
        day = _day(row.get("date"))
        if day in seen or day > through:
            raise ValueError("Projection recorded-load dates are invalid.")
        seen.add(day)
        if day == as_of:
            anchor = row
    if not summary["available"] or anchor is None:
        return None, "no_recorded_baseline", through.isoformat(), incomplete
    first = _day(model.get("history_start"))
    history_days = _count(anchor.get("history_days"), MAX_HISTORY_DAYS)
    if first > as_of or history_days != (as_of - first).days + 1:
        raise ValueError("Projection recorded history span is invalid.")
    if type(anchor.get("history_incomplete")) is not bool:
        raise ValueError("Projection recorded history quality is invalid.")
    incomplete = incomplete or anchor["history_incomplete"]
    result = {
        "date": as_of.isoformat(),
        "ctl": _number(anchor.get("ctl"), MAX_RECORDED_TSS),
        "atl": _number(anchor.get("atl"), MAX_RECORDED_TSS),
        "history_start": first.isoformat(),
        "history_days": history_days,
        "history_incomplete": incomplete,
        "seed_weight_ctl": _number(anchor.get("seed_weight_ctl"), 1),
        "seed_weight_atl": _number(anchor.get("seed_weight_atl"), 1),
        "recent_incomplete_days_42": _count(anchor.get("recent_incomplete_days_42"), CTL_DAYS),
        "recent_incomplete_days_7": _count(anchor.get("recent_incomplete_days_7"), ATL_DAYS),
    }
    return result, "complete", through.isoformat(), incomplete


def build_training_load_projection(
    recorded_model: Mapping[str, Any],
    daily_targets: Sequence[Mapping[str, Any]],
    *,
    as_of: date,
    end: date,
) -> dict[str, Any]:
    """Project tomorrow onward; never add today's full plan to recorded load.

    Input rows contain only date, target_tss, tss_source, and status. The caller
    resolves source conflicts and excludes stale plans before invoking this model.
    A missing day stops the scenario. No weekly total is allocated or interpolated.
    """
    if type(as_of) is not date or type(end) is not date:
        raise ValueError("Projection boundaries must be calendar dates.")
    horizon = (end - as_of).days
    if not 0 <= horizon <= MAX_PROJECTION_DAYS:
        raise ValueError("Projection horizon exceeds its supported range.")
    targets = _targets(daily_targets)
    anchor, reason, recorded_through, incomplete = _baseline(recorded_model, as_of)
    summary = {
        "available": False,
        "projected_days": 0,
        "provisional_days": 0,
        "stop_reason": reason,
        "stop_date": None,
        "history_incomplete": incomplete,
    }
    result = {
        "method": METHOD,
        "time_constants": {"ctl": CTL_DAYS, "atl": ATL_DAYS},
        "as_of": as_of.isoformat(),
        "recorded_through": recorded_through,
        "start_date": (as_of + timedelta(days=1)).isoformat() if horizon else None,
        "requested_through": end.isoformat(),
        "through_date": as_of.isoformat() if anchor is not None else None,
        "anchor": anchor,
        "rows": [],
        "summary": summary,
    }
    if anchor is None:
        return result
    ctl, atl = anchor["ctl"], anchor["atl"]
    seed_ctl, seed_atl = anchor["seed_weight_ctl"], anchor["seed_weight_atl"]
    provisional = False
    for offset in range(1, horizon + 1):
        day = as_of + timedelta(days=offset)
        prescription = targets.get(day)
        if prescription is None:
            summary.update(stop_reason="missing_daily_target", stop_date=day.isoformat())
            break
        score = prescription["target_tss"]
        tsb = ctl - atl
        ctl += (score - ctl) / CTL_DAYS
        atl += (score - atl) / ATL_DAYS
        seed_ctl *= (CTL_DAYS - 1) / CTL_DAYS
        seed_atl *= (ATL_DAYS - 1) / ATL_DAYS
        is_provisional = prescription["status"] == "provisional"
        provisional = provisional or is_provisional
        summary["provisional_days"] += int(is_provisional)
        result["rows"].append(
            {
                "date": day.isoformat(),
                **prescription,
                "ctl": round(ctl, 6),
                "atl": round(atl, 6),
                "tsb": round(tsb, 6),
                "projected": True,
                "projection_provisional": provisional,
                "history_incomplete": incomplete,
                "recorded_history_days": anchor["history_days"],
                "seed_weight_ctl": round(seed_ctl, 6),
                "seed_weight_atl": round(seed_atl, 6),
            }
        )
        result["through_date"] = day.isoformat()
    summary.update(available=bool(result["rows"]), projected_days=len(result["rows"]))
    return result
