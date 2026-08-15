from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Iterable

from .storage import read_json


def _safe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _activity_date(activity: dict[str, Any]) -> str:
    value = activity.get("start_date_local") or activity.get("start_date") or ""
    return str(value)[:10]


def load_temperature_corrections(data_dir: Path) -> list[dict[str, Any]]:
    payload = read_json(data_dir / "plan" / "device_corrections.json", default={}) or {}
    corrections = payload.get("temperature") or []
    if not isinstance(corrections, list):
        return []
    return [dict(rule) for rule in corrections if isinstance(rule, dict)]


def _matches_temperature_rule(activity: dict[str, Any], rule: dict[str, Any]) -> bool:
    device_name = rule.get("device_name")
    if device_name and activity.get("device_name") != device_name:
        return False

    activity_date = _activity_date(activity)
    start_date = str(rule.get("start_date") or "")
    end_date = str(rule.get("end_date") or "")
    if start_date and (not activity_date or activity_date < start_date):
        return False
    if end_date and (not activity_date or activity_date > end_date):
        return False
    return True


def apply_temperature_correction(
    activity: dict[str, Any],
    rules: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    corrected = dict(activity)
    raw_temp = _safe_float(activity.get("average_temp_raw", activity.get("average_temp")))
    if raw_temp is None:
        return corrected

    for rule in rules:
        offset_c = _safe_float(rule.get("offset_c"))
        if offset_c is None or not _matches_temperature_rule(activity, rule):
            continue
        min_raw_temp_c = _safe_float(rule.get("min_raw_temp_c"))
        max_raw_temp_c = _safe_float(rule.get("max_raw_temp_c"))
        if min_raw_temp_c is not None and raw_temp < min_raw_temp_c:
            continue
        if max_raw_temp_c is not None and raw_temp > max_raw_temp_c:
            continue

        corrected["average_temp_raw"] = raw_temp
        corrected["average_temp"] = raw_temp + offset_c
        metadata = {
            "id": str(rule.get("id") or "temperature-offset"),
            "offset_c": offset_c,
        }
        if min_raw_temp_c is not None:
            metadata["min_raw_temp_c"] = min_raw_temp_c
        if max_raw_temp_c is not None:
            metadata["max_raw_temp_c"] = max_raw_temp_c
        if rule.get("source_device_serial") not in (None, ""):
            metadata["source_device_serial"] = str(rule["source_device_serial"])
        corrected["temperature_correction"] = metadata
        break

    return corrected
