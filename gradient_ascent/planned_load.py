"""Bounded, display-only cycling load forecasts; never device prescriptions.

The formula is TSS = hours * IF**2 * 100. See TrainingPeaks' published
https://www.trainingpeaks.com/learn/articles/the-science-of-the-performance-manager/
The whole-session IF ranges below are conservative application assumptions, not
measured intensity, athlete-specific predictions, or prescribed interval targets.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

from .planned_workouts import _steps


MAX_DAILY_HOURS = 24.0
MAX_WEEKLY_HOURS = 168.0
MAX_DAILY_TSS = 21600.0
MAX_WEEKLY_TSS = MAX_DAILY_TSS * 7
WHOLE_SESSION_IF = {
    "recovery": (0.45, 0.60),
    "endurance": (0.60, 0.75),
    "openers": (0.55, 0.75),
    "tempo": (0.70, 0.85),
    "sweet_spot": (0.75, 0.90),
    "threshold": (0.75, 0.95),
    "vo2": (0.75, 0.95),
    "race_crit": (0.75, 1.00),
    "race_road": (0.70, 0.95),
    "race_dirt": (0.65, 0.90),
}
WEEKLY_BUDGET_IF = (0.55, 0.85)
_NUMBER = r"(?:\d+(?:\.\d+)?|\.\d+)"
_SOURCE_RANGE = re.compile(
    rf"\s*({_NUMBER})(?:\s*(?:-|–|—|to)\s*({_NUMBER}))?"
    r"\s*(hours?|hrs?|h|minutes?|mins?|min|m|tss)?\s*\Z",
    re.IGNORECASE,
)
_REPEATED_UNIT_RANGE = re.compile(
    rf"\s*({_NUMBER})\s*(hours?|hrs?|h|minutes?|mins?|min|m|tss)"
    rf"\s*(?:-|–|—|to)\s*({_NUMBER})\s*(hours?|hrs?|h|minutes?|mins?|min|m|tss)\s*\Z",
    re.IGNORECASE,
)


def _number(value: Any, maximum: float) -> float | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) and 0 <= result <= maximum else None


def _range(low: Any, high: Any, maximum: float) -> tuple[float, float] | None:
    if low is None and high is None:
        return None
    if low is None:
        low = high
    if high is None:
        high = low
    first, last = _number(low, maximum), _number(high, maximum)
    if first is None or last is None or first > last:
        return None
    return first, last


def parse_source_range(
    value: Any, *, unit: str, maximum: float
) -> tuple[float | None, float | None]:
    """Parse an entire explicit column value; duration results are in hours."""
    if unit not in {"hours", "minutes", "duration", "tss"} or isinstance(value, bool):
        return None, None
    text = str(value if value is not None else "").strip()
    if len(text) > 100:
        return None, None
    if unit != "tss" and re.fullmatch(r"\d{1,3}:[0-5]\d(?::[0-5]\d)?", text):
        parts = [int(part) for part in text.split(":")]
        hours = parts[0] + parts[1] / 60 + (parts[2] / 3600 if len(parts) == 3 else 0)
        return _range(hours, hours, maximum) or (None, None)
    repeated = _REPEATED_UNIT_RANGE.fullmatch(text)
    if repeated is not None:
        first, _ = parse_source_range(
            repeated.group(1) + repeated.group(2), unit=unit, maximum=maximum
        )
        last, _ = parse_source_range(
            repeated.group(3) + repeated.group(4), unit=unit, maximum=maximum
        )
        if first is None or last is None:
            return None, None
        return _range(first, last, maximum) or (None, None)
    match = _SOURCE_RANGE.fullmatch(text)
    if match is None:
        return None, None
    suffix = (match.group(3) or "").lower()
    if unit == "tss":
        if suffix not in {"", "tss"}:
            return None, None
        factor = 1.0
    elif suffix == "tss" or (unit == "duration" and not suffix):
        return None, None
    else:
        factor = 1 / 60 if suffix.startswith("m") or (not suffix and unit == "minutes") else 1.0
    first = float(match.group(1)) * factor
    last = float(match.group(2) or match.group(1)) * factor
    return _range(first, last, maximum) or (None, None)


def _mid(bounds: tuple[float, float] | None) -> float | None:
    return sum(bounds) / 2 if bounds is not None else None


def _load(
    hours: tuple[float, float] | None = None,
    tss: tuple[float, float] | None = None,
    *,
    duration_source: str = "missing",
    tss_source: str = "missing",
    method: str = "missing",
    estimated: bool = False,
    partial: bool = False,
    assumed_if: tuple[float, float] | None = None,
    tss_value: float | None = None,
    note: str = "No supported planned load is recorded.",
) -> dict[str, Any]:
    return {
        "hours": round(_mid(hours), 4) if hours is not None else None,
        "hours_min": round(hours[0], 4) if hours is not None else None,
        "hours_max": round(hours[1], 4) if hours is not None else None,
        "estimated_tss": round(tss_value if tss_value is not None else _mid(tss), 1)
        if tss is not None
        else None,
        "estimated_tss_min": round(tss[0], 1) if tss is not None else None,
        "estimated_tss_max": round(tss[1], 1) if tss is not None else None,
        "duration_source": duration_source if hours is not None else "missing",
        "tss_source": tss_source if tss is not None else "missing",
        "method": method if tss is not None else "missing",
        "estimated": bool(tss is not None and estimated),
        "partial": bool(partial),
        "assumed_if_min": assumed_if[0] if assumed_if is not None else None,
        "assumed_if_max": assumed_if[1] if assumed_if is not None else None,
        "known_hours_days": int(hours is not None),
        "known_tss_days": int(tss is not None),
        "total_days": 1,
        "note": note,
    }


def _forecast(
    hours: tuple[float, float], intensity: tuple[float, float]
) -> tuple[tuple[float, float], float]:
    low, high = intensity
    return (100 * hours[0] * low**2, 100 * hours[1] * high**2), 100 * _mid(hours) * _mid(
        intensity
    ) ** 2


def day_planned_load(
    *,
    hours_min: Any = None,
    hours_max: Any = None,
    tss_min: Any = None,
    tss_max: Any = None,
    intensity: str | None = None,
    is_rest: bool = False,
    duration_source: str = "plan_text",
) -> dict[str, Any]:
    hours = _range(hours_min, hours_max, MAX_DAILY_HOURS)
    tss = _range(tss_min, tss_max, MAX_DAILY_TSS)
    if is_rest and hours is None:
        hours, duration_source = (0.0, 0.0), "explicit_rest"
    if tss is not None:
        return _load(
            hours,
            tss,
            duration_source=duration_source,
            tss_source="source_target",
            method="source",
            note="Explicit source planned-TSS target.",
        )
    if is_rest and hours == (0.0, 0.0):
        return _load(
            hours,
            (0.0, 0.0),
            duration_source=duration_source,
            tss_source="explicit_rest",
            method="source",
            note="Explicit rest/off day.",
        )
    assumed = WHOLE_SESSION_IF.get(intensity) if isinstance(intensity, str) else None
    if hours is None or assumed is None:
        return _load(
            hours,
            duration_source=duration_source,
            note="A full session duration and supported cycling intensity are needed for a forecast.",
        )
    bounds, value = _forecast(hours, assumed)
    return _load(
        hours,
        bounds,
        duration_source=duration_source,
        tss_source="session_if_forecast",
        method="whole_session_if_v1",
        estimated=True,
        assumed_if=assumed,
        tss_value=value,
        note=f"Display-only forecast using whole-session IF {assumed[0]:g}–{assumed[1]:g}; not a device prescription.",
    )


def _mapping_range(value: Any, maximum: float) -> tuple[float, float] | None:
    return (
        _range(value.get("min"), value.get("max"), maximum) if isinstance(value, Mapping) else None
    )


def week_planned_load(
    day_loads: Sequence[Mapping[str, Any]],
    *,
    hours_target: Any = None,
    tss_target: Any = None,
) -> dict[str, Any]:
    if len(day_loads) > 7:
        raise ValueError("A weekly forecast accepts at most seven day loads.")
    hours_values = [
        _range(day.get("hours_min"), day.get("hours_max"), MAX_DAILY_HOURS) for day in day_loads
    ]
    tss_values = [
        _range(day.get("estimated_tss_min"), day.get("estimated_tss_max"), MAX_DAILY_TSS)
        for day in day_loads
    ]
    known_hours, known_tss = (
        sum(value is not None for value in hours_values),
        sum(value is not None for value in tss_values),
    )
    total = len(day_loads)
    explicit_hours = _mapping_range(hours_target, MAX_WEEKLY_HOURS)
    hours = explicit_hours
    duration_source = "source_weekly_hours"
    if hours is None and total and known_hours == total:
        hours = tuple(sum(value[index] for value in hours_values) for index in (0, 1))
        duration_source = "complete_daily_sum"
    source_tss = _mapping_range(tss_target, MAX_WEEKLY_TSS)
    if source_tss is not None:
        result = _load(
            hours,
            source_tss,
            duration_source=duration_source,
            tss_source="source_target",
            method="source",
            note="Explicit source weekly planned-TSS target.",
        )
    elif total and known_tss == total:
        tss = tuple(sum(value[index] for value in tss_values) for index in (0, 1))
        value = sum(
            _number(day.get("estimated_tss"), MAX_DAILY_TSS)
            if _number(day.get("estimated_tss"), MAX_DAILY_TSS) is not None
            else _mid(bounds)
            for day, bounds in zip(day_loads, tss_values)
        )
        result = _load(
            hours,
            tss,
            duration_source=duration_source,
            tss_source="complete_daily_sum",
            method="complete_daily_sum",
            estimated=any(day.get("estimated") is True for day in day_loads),
            tss_value=value,
            note="Sum of all available daily source targets and labeled forecasts.",
        )
    elif explicit_hours is not None:
        bounds, value = _forecast(explicit_hours, WEEKLY_BUDGET_IF)
        result = _load(
            hours,
            bounds,
            duration_source=duration_source,
            tss_source="weekly_hours_budget",
            method="weekly_hours_budget_if_v1",
            estimated=True,
            partial=known_tss < total,
            assumed_if=WEEKLY_BUDGET_IF,
            tss_value=value,
            note="Broad weekly-hours-budget forecast using whole-week IF 0.55–0.85; no missing daily durations or workouts are assigned.",
        )
    else:
        result = _load(
            hours,
            duration_source=duration_source,
            partial=0 < known_tss < total,
            note="Daily planned load is incomplete and no explicit weekly budget is recorded.",
        )
    result.update(known_hours_days=known_hours, known_tss_days=known_tss, total_days=total)
    return result


def structured_workout_load(workout: Mapping[str, Any], *, ftp_w: Any = None) -> dict[str, Any]:
    """Model one independent explicit workout; callers must not auto-merge prose."""
    try:
        if workout.get("sport") != "cycling":
            raise ValueError("Not a cycling workout")
        steps = _steps(workout.get("steps"))
    except (TypeError, ValueError):
        return _load(note="A valid explicit cycling workout is required.")
    seconds = sum(step["duration_s"] for step in steps)
    hours = (seconds / 3600.0,) * 2
    ftp = _number(ftp_w, 3000)
    fourth = [0.0, 0.0, 0.0]
    for step in steps:
        target = step["target"]
        if target["type"] != "power" or (target["unit"] == "watts" and (ftp is None or ftp < 1)):
            return _load(
                hours,
                duration_source="structured_steps",
                note="Exact structured duration; open targets or missing FTP prevent a complete power-model forecast.",
            )
        divisor = 100.0 if target["unit"] == "percent_ftp" else ftp
        low, high = target["low"] / divisor, target["high"] / divisor
        if high > 3:
            return _load(
                hours,
                duration_source="structured_steps",
                note="Structured power intensity exceeds the model's supported range.",
            )
        for index, intensity in enumerate((low, (low + high) / 2, high)):
            fourth[index] += step["duration_s"] * intensity**4
    values = [100 * hours[0] * math.sqrt(value / seconds) for value in fourth]
    return _load(
        hours,
        (values[0], values[2]),
        duration_source="structured_steps",
        tss_source="structured_power_model",
        method="structured_power_fourth_moment_v1",
        estimated=True,
        tss_value=values[1],
        note="Independent structured-workout forecast from explicit step power targets; no telemetry, completion, or exact device TSS is implied.",
    )
