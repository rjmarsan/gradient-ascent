from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from .storage import write_json


def _safe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _date_key(value: str | None) -> str | None:
    return value[:10] if value else None


def _duration_seconds(value: Any, unit: str | None) -> float | None:
    numeric = _safe_float(value)
    if numeric is None:
        return None
    normalized = str(unit or "min").strip().lower()
    if normalized in {"s", "sec", "second", "seconds"}:
        return numeric
    if normalized in {"h", "hr", "hour", "hours"}:
        return numeric * 3600.0
    return numeric * 60.0


def _distance_meters(value: Any, unit: str | None) -> float | None:
    numeric = _safe_float(value)
    if numeric is None:
        return None
    normalized = str(unit or "m").strip().lower()
    if normalized in {"km", "kilometer", "kilometers"}:
        return numeric * 1000.0
    if normalized in {"mi", "mile", "miles"}:
        return numeric * 1609.344
    return numeric


def _energy_kilojoules(value: Any, unit: str | None) -> float | None:
    numeric = _safe_float(value)
    if numeric is None:
        return None
    normalized = str(unit or "kcal").strip().lower()
    if normalized in {"kj", "kilojoule", "kilojoules"}:
        return numeric
    return numeric * 4.184


def resolve_apple_health_export_xml(export_path: Path) -> Path:
    xml_path = export_path
    if export_path.is_dir():
        xml_path = export_path / "apple_health_export" / "export.xml"
        if not xml_path.exists():
            xml_path = export_path / "export.xml"
    if not xml_path.exists():
        raise FileNotFoundError(f"Could not find Apple Health export XML at {xml_path}")
    return xml_path


def import_apple_health_export(data_dir: Path, export_path: Path) -> dict[str, Any]:
    xml_path = resolve_apple_health_export_xml(export_path)

    workouts: list[dict[str, Any]] = []
    daily: dict[str, dict[str, list[float] | float | None]] = defaultdict(
        lambda: {
            "resting_hr": [],
            "hrv_ms": [],
            "sleep_duration_s": 0.0,
        }
    )
    for _, elem in ElementTree.iterparse(xml_path, events=("end",)):
        if elem.tag == "Workout":
            workouts.append(
                {
                    "id": elem.attrib.get("uuid") or f"workout-{len(workouts) + 1}",
                    "workout_type": elem.attrib.get("workoutActivityType"),
                    "start_date": elem.attrib.get("startDate"),
                    "end_date": elem.attrib.get("endDate"),
                    "duration_s": _duration_seconds(
                        elem.attrib.get("duration"), elem.attrib.get("durationUnit")
                    ),
                    "distance_m": _distance_meters(
                        elem.attrib.get("totalDistance"), elem.attrib.get("totalDistanceUnit")
                    ),
                    "energy_kj": _energy_kilojoules(
                        elem.attrib.get("totalEnergyBurned"),
                        elem.attrib.get("totalEnergyBurnedUnit"),
                    ),
                }
            )
        elif elem.tag == "Record":
            record_type = elem.attrib.get("type")
            day = _date_key(elem.attrib.get("startDate"))
            if not day:
                elem.clear()
                continue
            value = _safe_float(elem.attrib.get("value"))
            if record_type == "HKQuantityTypeIdentifierRestingHeartRate" and value is not None:
                daily[day]["resting_hr"].append(value)
            elif record_type == "HKQuantityTypeIdentifierHeartRateVariabilitySDNN" and value is not None:
                daily[day]["hrv_ms"].append(value)
            elif (
                record_type == "HKCategoryTypeIdentifierSleepAnalysis"
                and "Asleep" in elem.attrib.get("value", "")
            ):
                try:
                    start = datetime.strptime(elem.attrib["startDate"], "%Y-%m-%d %H:%M:%S %z")
                    end = datetime.strptime(elem.attrib["endDate"], "%Y-%m-%d %H:%M:%S %z")
                except (KeyError, ValueError):
                    pass
                else:
                    daily[day]["sleep_duration_s"] += max((end - start).total_seconds(), 0.0)
        elem.clear()

    recovery: list[dict[str, Any]] = []
    for day, values in sorted(daily.items()):
        resting_values = values["resting_hr"]
        hrv_values = values["hrv_ms"]
        recovery.append(
            {
                "id": f"apple_health:{day}",
                "date": day,
                "resting_hr": (
                    round(sum(resting_values) / len(resting_values), 2)
                    if resting_values
                    else None
                ),
                "hrv_ms": (
                    round(sum(hrv_values) / len(hrv_values), 2)
                    if hrv_values
                    else None
                ),
                "sleep_duration_s": values["sleep_duration_s"] or None,
                "sleep_score": None,
                "readiness_score": None,
                "recovery_score": None,
                "stress_avg": None,
                "raw": {},
            }
        )
    write_json(data_dir / "apple_health" / "workouts.json", workouts)
    write_json(data_dir / "apple_health" / "recovery.json", recovery)
    return {
        "export_path": str(xml_path),
        "workouts": len(workouts),
        "recovery_days": len(recovery),
    }
