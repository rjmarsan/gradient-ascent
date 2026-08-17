from __future__ import annotations

import re
import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .storage import read_json, write_json
from .activity_titles import is_placeholder_title
from .power_metrics import _valid_estimate, enrich_recording_power
from .canonical import (
    canonical_activity_records,
    canonical_recovery_records,
    choose_primary_recovery,
    recovery_by_date,
    resolve_activity_records,
)


MEANINGFUL_RIDE_MIN_SECONDS = 30 * 60
MEANINGFUL_RIDE_MIN_KILOJOULES = 300.0
MEANINGFUL_RIDE_MIN_TSS = 10.0
MEANINGFUL_RIDE_MIN_SUFFER_SCORE = 20.0


def _is_cycling_activity(sport_type: Any) -> bool:
    normalized = re.sub(r"[^a-z]", "", str(sport_type or "").lower())
    return normalized in {
        "cycling",
        "ebikeride",
        "emountainbikeride",
        "gravelride",
        "handcycle",
        "hkworkoutactivitytypecycling",
        "indoorcycling",
        "mountainbikeride",
        "ride",
        "velomobile",
        "virtualride",
    }


@dataclass
class AggregateTotals:
    activity_count: int = 0
    moving_time_s: float = 0.0
    elapsed_time_s: float = 0.0
    distance_m: float = 0.0
    elevation_gain_m: float = 0.0
    kilojoules: float = 0.0
    meaningful_ride_time_s: float = 0.0
    meaningful_ride_kilojoules: float = 0.0
    meaningful_ride_count: int = 0
    excluded_short_ride_time_s: float = 0.0
    excluded_short_ride_count: int = 0
    estimated_tss: float = 0.0
    estimated_tss_activity_count: int = 0
    estimated_tss_estimated_activity_count: int = 0
    estimated_tss_partial_activity_count: int = 0
    avg_hr_sum: float = 0.0
    avg_hr_weight: float = 0.0
    avg_power_sum: float = 0.0
    avg_power_weight: float = 0.0
    by_sport: Dict[str, int] = None

    def __post_init__(self) -> None:
        if self.by_sport is None:
            self.by_sport = {}

    def add_activity(self, activity: Dict[str, Any]) -> None:
        self.activity_count += 1
        moving_time = float(activity.get("moving_time_s") or 0)
        elapsed_time = float(activity.get("elapsed_time_s") or 0)
        distance = float(activity.get("distance_m") or 0)
        elevation = float(activity.get("elevation_gain_m") or 0)
        kilojoules = float(activity.get("kilojoules") or 0)
        avg_hr = activity.get("average_heartrate")
        avg_power = activity.get("average_watts")
        estimated_tss = activity.get("estimated_tss")
        sport = activity.get("sport_type") or "Unknown"

        self.moving_time_s += moving_time
        self.elapsed_time_s += elapsed_time
        self.distance_m += distance
        self.elevation_gain_m += elevation
        self.kilojoules += kilojoules
        if estimated_tss is not None:
            self.estimated_tss += float(estimated_tss)
            self.estimated_tss_activity_count += 1
            if str(activity.get("estimated_tss_source") or "").startswith("estimated_"):
                self.estimated_tss_estimated_activity_count += 1
            if (
                activity.get("estimated_tss_source") == "estimated_power_stream"
                and (activity.get("power_load_estimate") or {}).get("scope") == "recorded_power"
            ):
                self.estimated_tss_partial_activity_count += 1

        if _is_cycling_activity(sport):
            if activity.get("is_meaningful_ride"):
                self.meaningful_ride_time_s += moving_time
                self.meaningful_ride_kilojoules += kilojoules
                self.meaningful_ride_count += 1
            elif moving_time > 0:
                self.excluded_short_ride_time_s += moving_time
                self.excluded_short_ride_count += 1

        if avg_hr is not None and moving_time > 0:
            self.avg_hr_sum += float(avg_hr) * moving_time
            self.avg_hr_weight += moving_time
        if avg_power is not None and moving_time > 0:
            self.avg_power_sum += float(avg_power) * moving_time
            self.avg_power_weight += moving_time

        self.by_sport[sport] = self.by_sport.get(sport, 0) + 1

    def merge(self, other: "AggregateTotals") -> None:
        self.activity_count += other.activity_count
        self.moving_time_s += other.moving_time_s
        self.elapsed_time_s += other.elapsed_time_s
        self.distance_m += other.distance_m
        self.elevation_gain_m += other.elevation_gain_m
        self.kilojoules += other.kilojoules
        self.meaningful_ride_time_s += other.meaningful_ride_time_s
        self.meaningful_ride_kilojoules += other.meaningful_ride_kilojoules
        self.meaningful_ride_count += other.meaningful_ride_count
        self.excluded_short_ride_time_s += other.excluded_short_ride_time_s
        self.excluded_short_ride_count += other.excluded_short_ride_count
        self.estimated_tss += other.estimated_tss
        self.estimated_tss_activity_count += other.estimated_tss_activity_count
        self.estimated_tss_estimated_activity_count += other.estimated_tss_estimated_activity_count
        self.estimated_tss_partial_activity_count += other.estimated_tss_partial_activity_count
        self.avg_hr_sum += other.avg_hr_sum
        self.avg_hr_weight += other.avg_hr_weight
        self.avg_power_sum += other.avg_power_sum
        self.avg_power_weight += other.avg_power_weight
        for sport, count in other.by_sport.items():
            self.by_sport[sport] = self.by_sport.get(sport, 0) + count

    def finalize(self) -> Dict[str, Any]:
        avg_hr = self.avg_hr_sum / self.avg_hr_weight if self.avg_hr_weight > 0 else None
        avg_power = (
            self.avg_power_sum / self.avg_power_weight if self.avg_power_weight > 0 else None
        )
        return {
            "activity_count": self.activity_count,
            "moving_time_s": round(self.moving_time_s, 1),
            "elapsed_time_s": round(self.elapsed_time_s, 1),
            "distance_m": round(self.distance_m, 1),
            "elevation_gain_m": round(self.elevation_gain_m, 1),
            "kilojoules": round(self.kilojoules, 1),
            "meaningful_ride_time_s": round(self.meaningful_ride_time_s, 1),
            "meaningful_ride_kilojoules": round(self.meaningful_ride_kilojoules, 1),
            "meaningful_ride_count": self.meaningful_ride_count,
            "excluded_short_ride_time_s": round(self.excluded_short_ride_time_s, 1),
            "excluded_short_ride_count": self.excluded_short_ride_count,
            "estimated_tss": round(self.estimated_tss, 1)
            if self.estimated_tss_activity_count
            else None,
            "estimated_tss_activity_count": self.estimated_tss_activity_count,
            "estimated_tss_estimated_activity_count": self.estimated_tss_estimated_activity_count,
            "estimated_tss_partial_activity_count": self.estimated_tss_partial_activity_count,
            "average_heartrate": round(avg_hr, 1) if avg_hr is not None else None,
            "average_watts": round(avg_power, 1) if avg_power is not None else None,
            "by_sport": dict(sorted(self.by_sport.items())),
        }


def _parse_local_date(value: str | None) -> Optional[str]:
    if not value:
        return None
    return value.split("T")[0]


def _safe_float(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(result):
        return None
    return result


def _training_load_fields(
    moving_time_s: Any,
    weighted_average_watts: Any,
    ftp_w: Optional[float],
) -> Tuple[Optional[float], Optional[float]]:
    moving_s = _safe_float(moving_time_s)
    np_w = _safe_float(weighted_average_watts)
    ftp = _safe_float(ftp_w)
    if moving_s is None or moving_s <= 0 or np_w is None or np_w < 0 or ftp is None or not 1 <= ftp <= 3000:
        return None, None
    try:
        intensity_factor = np_w / ftp
        estimated_tss = (moving_s / 3600.0) * (intensity_factor**2) * 100.0
    except OverflowError:
        return None, None
    if not math.isfinite(intensity_factor) or not math.isfinite(estimated_tss):
        return None, None
    return round(intensity_factor, 3), round(estimated_tss, 1)


def _meaningful_ride_fields(
    sport_type: Any,
    moving_time_s: Any,
    kilojoules: Any,
    estimated_tss: Optional[float],
    suffer_score: Any,
) -> Tuple[bool, Optional[str]]:
    if not _is_cycling_activity(sport_type):
        return False, "not_ride"

    moving_s = _safe_float(moving_time_s) or 0.0
    kj = _safe_float(kilojoules) or 0.0
    tss = _safe_float(estimated_tss)
    suffer = _safe_float(suffer_score) or 0.0

    if moving_s <= 0:
        return False, "no_moving_time"
    if moving_s < MEANINGFUL_RIDE_MIN_SECONDS:
        return False, "short_ride"
    if tss is not None:
        if tss < MEANINGFUL_RIDE_MIN_TSS:
            return False, "low_tss_ride"
        return True, None
    if kj >= MEANINGFUL_RIDE_MIN_KILOJOULES:
        return True, None
    if suffer >= MEANINGFUL_RIDE_MIN_SUFFER_SCORE:
        return True, None
    return False, "low_load_ride"


def _normalize_activity(activity: Dict[str, Any], ftp_w: Optional[float]) -> Dict[str, Any]:
    raw = activity.get("raw") if isinstance(activity.get("raw"), dict) else activity
    source = activity.get("source") if isinstance(activity.get("source"), dict) else {}
    source_provider = source.get("provider")
    legacy_id = raw.get("id") if source_provider == "strava" and isinstance(raw, dict) else None
    sport_type = activity.get("sport_type") or activity.get("type")
    moving_time_s = (
        activity.get("moving_time_s")
        if "moving_time_s" in activity
        else activity.get("moving_time")
    )
    average_watts = _safe_float(activity.get("average_watts"))
    kilojoules = _safe_float(activity.get("kilojoules"))
    moving_seconds = _safe_float(moving_time_s)
    if (
        kilojoules is None
        and average_watts
        and average_watts > 0
        and moving_seconds
        and moving_seconds > 0
    ):
        kilojoules = round(average_watts * moving_seconds / 1000.0, 1)
    source_np = _safe_float(activity.get("weighted_average_watts"))
    source_np = source_np if source_np is not None and source_np >= 0 else None
    power_estimate = activity.get("power_load_estimate")
    uses_stream_estimate = source_np is None and _valid_estimate(power_estimate)
    weighted_watts = (
        power_estimate["estimated_normalized_power_w"] if uses_stream_estimate else source_np
    )
    load_duration = moving_time_s
    estimate_details = None
    if uses_stream_estimate:
        observed = float(power_estimate["observed_duration_s"])
        load_duration = (
            min(moving_seconds, observed) if moving_seconds and moving_seconds > 0 else None
        )
        coverage = (
            min(1.0, observed / moving_seconds) if moving_seconds and moving_seconds > 0 else None
        )
        estimate_details = {
            **power_estimate,
            "load_duration_s": load_duration,
            "coverage_ratio": round(coverage, 6) if coverage is not None else None,
            "scope": "full_duration"
            if moving_seconds and observed >= moving_seconds - 1
            else "recorded_power",
        }
    intensity_factor, estimated_tss = _training_load_fields(load_duration, weighted_watts, ftp_w)
    source_tss = _safe_float(activity.get("estimated_tss"))
    source_tss = source_tss if source_tss is not None and source_tss >= 0 else None
    source_if = _safe_float(activity.get("intensity_factor"))
    source_if = source_if if source_if is not None and source_if >= 0 else None
    effective_tss = source_tss if source_tss is not None else estimated_tss
    tss_source = (
        "source"
        if source_tss is not None
        else "estimated_power_stream"
        if uses_stream_estimate and estimated_tss is not None
        else "estimated_source_np"
        if estimated_tss is not None
        else None
    )
    is_meaningful_ride, exclusion_reason = _meaningful_ride_fields(
        sport_type,
        moving_time_s,
        kilojoules,
        effective_tss,
        raw.get("suffer_score") if isinstance(raw, dict) else None,
    )

    source_details = {}
    source_activity_id = raw.get("source_activity_id") if isinstance(raw, dict) else None
    if (
        source_provider == "recording"
        and raw.get("source_provider") == "ridewithgps"
        and isinstance(source_activity_id, str)
        and re.fullmatch(r"[1-9][0-9]{0,31}", source_activity_id)
    ):
        provider_name = raw.get("source_provider_name")
        explicit_authored = (
            activity.get("name_is_authored") is True or raw.get("name_is_authored") is True
        )
        inferred_authored = (
            isinstance(provider_name, str)
            and bool(provider_name.strip())
            and provider_name != activity.get("name")
            and not is_placeholder_title(activity.get("name"), source_ids=(source_activity_id,))
        )
        source_details = {
            "source_provider": "ridewithgps",
            "source_activity_id": source_activity_id,
            "name_is_authored": explicit_authored or inferred_authored,
        }
    return {
        "id": legacy_id if legacy_id is not None else activity.get("id"),
        "provider_id": activity.get("provider_id"),
        "name": activity.get("name"),
        "sport_type": sport_type,
        "type": activity.get("type"),
        "start_date": activity.get("start_date"),
        "start_date_local": activity.get("start_date_local"),
        "date": activity.get("date") or _parse_local_date(activity.get("start_date_local")),
        "moving_time_s": moving_time_s,
        "elapsed_time_s": activity.get("elapsed_time_s")
        if "elapsed_time_s" in activity
        else activity.get("elapsed_time"),
        "distance_m": activity.get("distance_m")
        if "distance_m" in activity
        else activity.get("distance"),
        "elevation_gain_m": activity.get("elevation_gain_m")
        if "elevation_gain_m" in activity
        else activity.get("total_elevation_gain"),
        "average_speed_mps": raw.get("average_speed") if isinstance(raw, dict) else None,
        "average_heartrate": activity.get("average_heartrate"),
        "max_heartrate": activity.get("max_heartrate"),
        "average_watts": activity.get("average_watts"),
        "weighted_average_watts": weighted_watts,
        "weighted_average_watts_source": "source"
        if source_np is not None
        else "estimated_power_stream"
        if uses_stream_estimate
        else None,
        "estimated_normalized_power_w": weighted_watts if uses_stream_estimate else None,
        "power_load_estimate": estimate_details,
        "intensity_factor": source_if if source_if is not None else intensity_factor,
        "estimated_tss": effective_tss,
        "estimated_tss_source": tss_source,
        "is_meaningful_ride": is_meaningful_ride,
        "meaningful_exclusion_reason": exclusion_reason,
        "kilojoules": kilojoules,
        "suffer_score": raw.get("suffer_score") if isinstance(raw, dict) else None,
        "trainer": raw.get("trainer") if isinstance(raw, dict) else None,
        "commute": raw.get("commute") if isinstance(raw, dict) else None,
        "private": raw.get("private") if isinstance(raw, dict) else None,
        "device_name": raw.get("device_name") if isinstance(raw, dict) else None,
        "source": source or None,
        **source_details,
    }


def _load_strava_activities(path: Path, ftp_w: Optional[float]) -> List[Dict[str, Any]]:
    data = read_json(path, default={}) or {}
    activities: List[Dict[str, Any]] = []
    for activity in data.values():
        activities.append(_normalize_activity(activity, ftp_w))
    activities.sort(key=lambda item: item.get("start_date_local") or "")
    return activities



def _load_garage_plan(data_dir: Path) -> Optional[Dict[str, Any]]:
    path = data_dir / "plan" / "garage.json"
    if not path.exists():
        return None
    return read_json(path, default={}) or {}


def _load_strava_details(data_dir: Path) -> List[Dict[str, Any]]:
    details_dir = data_dir / "strava" / "details"
    if not details_dir.exists():
        return []
    details: List[Dict[str, Any]] = []
    for path in details_dir.glob("*.json"):
        payload = read_json(path, default={}) or {}
        detail = payload.get("detail")
        if detail:
            details.append(detail)
    return details


def _aggregate_bike_gear(details: List[Dict[str, Any]]) -> Tuple[Dict[str, Any], Dict[str, List[Dict[str, Any]]], Dict[str, Any]]:
    usage: Dict[str, Dict[str, Any]] = {}
    activities: Dict[str, List[Dict[str, Any]]] = {}
    meta: Dict[str, Dict[str, Any]] = {}
    for detail in details:
        gear_obj = detail.get("gear") or {}
        gear_id = detail.get("gear_id") or gear_obj.get("id")
        if not gear_id or not str(gear_id).startswith("b"):
            continue
        date_key = _parse_local_date(
            detail.get("start_date_local") or detail.get("start_date")
        )
        distance_m = float(detail.get("distance") or 0)
        moving_time_s = float(detail.get("moving_time") or 0)
        record = usage.setdefault(
            gear_id,
            {
                "activity_count": 0,
                "distance_m": 0.0,
                "moving_time_s": 0.0,
                "last_activity_date": None,
            },
        )
        record["activity_count"] += 1
        record["distance_m"] += distance_m
        record["moving_time_s"] += moving_time_s
        if date_key and (record["last_activity_date"] is None or date_key > record["last_activity_date"]):
            record["last_activity_date"] = date_key
        activities.setdefault(gear_id, []).append(
            {
                "date": date_key,
                "distance_m": distance_m,
                "moving_time_s": moving_time_s,
            }
        )
        if gear_obj and gear_id not in meta:
            meta[gear_id] = {
                "name": gear_obj.get("name"),
                "nickname": gear_obj.get("nickname"),
                "primary": gear_obj.get("primary"),
                "retired": gear_obj.get("retired"),
            }
    return usage, activities, meta


def _sum_activities_since(activities: List[Dict[str, Any]], since_date: str | None) -> Tuple[float, float]:
    if not since_date:
        return 0.0, 0.0
    distance_m = 0.0
    moving_time_s = 0.0
    for activity in activities:
        date_key = activity.get("date")
        if not date_key or date_key < since_date:
            continue
        distance_m += float(activity.get("distance_m") or 0)
        moving_time_s += float(activity.get("moving_time_s") or 0)
    return distance_m, moving_time_s


def _days_between(start: str | None, end: str | None) -> Optional[int]:
    if not start or not end:
        return None
    try:
        start_date = date.fromisoformat(start)
        end_date = date.fromisoformat(end)
    except ValueError:
        return None
    return (end_date - start_date).days


def _build_garage_summary(data_dir: Path, output_dir: Path) -> Optional[Dict[str, Any]]:
    plan = _load_garage_plan(data_dir)
    if plan is None:
        return None

    details = _load_strava_details(data_dir)
    usage_by_gear, activities_by_gear, gear_meta = _aggregate_bike_gear(details)
    bikes = plan.get("bikes", []) or []
    as_of_date = datetime.now(UTC).date().isoformat()

    summaries: List[Dict[str, Any]] = []
    mapped_gear_ids: set[str] = set()

    for bike in bikes:
        gear_ids = bike.get("strava_gear_ids") or []
        mapped_gear_ids.update(gear_ids)
        total_distance_m = 0.0
        total_moving_time_s = 0.0
        activity_count = 0
        last_activity_date = None
        combined_activities: List[Dict[str, Any]] = []
        for gear_id in gear_ids:
            record = usage_by_gear.get(gear_id)
            if record:
                total_distance_m += float(record.get("distance_m") or 0)
                total_moving_time_s += float(record.get("moving_time_s") or 0)
                activity_count += int(record.get("activity_count") or 0)
                record_date = record.get("last_activity_date")
                if record_date and (last_activity_date is None or record_date > last_activity_date):
                    last_activity_date = record_date
            combined_activities.extend(activities_by_gear.get(gear_id, []))

        distance_km = total_distance_m / 1000.0 if total_distance_m else 0.0
        hours = total_moving_time_s / 3600.0 if total_moving_time_s else 0.0

        maintenance = bike.get("maintenance") or {}
        components = maintenance.get("components") or []
        component_status: List[Dict[str, Any]] = []
        for component in components:
            interval_distance_km = component.get("interval_distance_km")
            interval_hours = component.get("interval_hours")
            interval_days = component.get("interval_days")
            last_service_date = component.get("last_service_date")
            last_service_distance_km = component.get("last_service_distance_km")
            last_service_hours = component.get("last_service_hours")

            distance_since_km = None
            hours_since = None
            if last_service_distance_km is not None:
                distance_since_km = max(distance_km - float(last_service_distance_km), 0.0)
            elif last_service_date:
                distance_since_m, _ = _sum_activities_since(combined_activities, last_service_date)
                distance_since_km = distance_since_m / 1000.0

            if last_service_hours is not None:
                hours_since = max(hours - float(last_service_hours), 0.0)
            elif last_service_date:
                _, moving_since_s = _sum_activities_since(combined_activities, last_service_date)
                hours_since = moving_since_s / 3600.0

            days_since = _days_between(last_service_date, as_of_date)

            due_reasons: List[str] = []
            if interval_distance_km is not None and distance_since_km is not None:
                if distance_since_km >= float(interval_distance_km):
                    due_reasons.append("distance")
            if interval_hours is not None and hours_since is not None:
                if hours_since >= float(interval_hours):
                    due_reasons.append("hours")
            if interval_days is not None and days_since is not None:
                if days_since >= int(interval_days):
                    due_reasons.append("days")

            component_status.append(
                {
                    "component_id": component.get("component_id"),
                    "name": component.get("name"),
                    "interval": {
                        "distance_km": interval_distance_km,
                        "hours": interval_hours,
                        "days": interval_days,
                    },
                    "since": {
                        "distance_km": round(distance_since_km, 2)
                        if distance_since_km is not None
                        else None,
                        "hours": round(hours_since, 2) if hours_since is not None else None,
                        "days": days_since,
                    },
                    "due": bool(due_reasons),
                    "due_reasons": due_reasons,
                }
            )

        summaries.append(
            {
                "bike_id": bike.get("bike_id"),
                "name": bike.get("name"),
                "category": bike.get("category"),
                "status": bike.get("status"),
                "strava_gear_ids": gear_ids,
                "usage": {
                    "activity_count": activity_count,
                    "distance_km": round(distance_km, 2),
                    "distance_m": round(total_distance_m, 1),
                    "moving_time_hours": round(hours, 2),
                    "moving_time_s": round(total_moving_time_s, 1),
                    "last_activity_date": last_activity_date,
                },
                "maintenance": component_status,
            }
        )

    unmapped = []
    for gear_id, record in usage_by_gear.items():
        if gear_id in mapped_gear_ids:
            continue
        meta = gear_meta.get(gear_id, {})
        unmapped.append(
            {
                "gear_id": gear_id,
                "name": meta.get("name"),
                "nickname": meta.get("nickname"),
                "primary": meta.get("primary"),
                "retired": meta.get("retired"),
                "usage": {
                    "activity_count": record.get("activity_count"),
                    "distance_km": round(float(record.get("distance_m") or 0) / 1000.0, 2),
                    "distance_m": round(float(record.get("distance_m") or 0), 1),
                    "moving_time_hours": round(float(record.get("moving_time_s") or 0) / 3600.0, 2),
                    "moving_time_s": round(float(record.get("moving_time_s") or 0), 1),
                    "last_activity_date": record.get("last_activity_date"),
                },
            }
        )

    summary = {
        "as_of_date": as_of_date,
        "bikes": summaries,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "meta": plan.get("meta"),
        "unmapped_strava_bikes": unmapped,
    }
    write_json(output_dir / "garage.json", summary)
    return summary


def _date_range(start: str, end: str) -> Iterable[str]:
    start_dt = datetime.fromisoformat(start)
    end_dt = datetime.fromisoformat(end)
    current = start_dt
    while current <= end_dt:
        yield current.date().isoformat()
        current += timedelta(days=1)


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


def _load_plan_weeks(data_dir: Path) -> Optional[List[Dict[str, Any]]]:
    plan_weeks_path = data_dir / "plan" / "weeks.json"
    if plan_weeks_path.exists():
        return read_json(plan_weeks_path, default=[]) or []
    return None


def build_insights(data_dir: Path, calendar_path: Optional[Path], output_dir: Path) -> Dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)

    athlete = read_json(data_dir / "plan" / "athlete.json", default={}) or {}
    ftp_w = _safe_float(athlete.get("ftp_w"))
    resolved_activity_records, _ = resolve_activity_records(canonical_activity_records(data_dir))
    resolved_activity_records = enrich_recording_power(data_dir, resolved_activity_records, output_dir)
    activities = [
        _normalize_activity(activity, ftp_w)
        for activity in resolved_activity_records
    ]
    write_json(output_dir / "activities.json", activities)

    daily_totals: Dict[str, AggregateTotals] = {}
    daily_entries: Dict[str, Dict[str, Any]] = {}

    for activity in activities:
        date_key = activity.get("date")
        if not date_key:
            continue
        if date_key not in daily_totals:
            daily_totals[date_key] = AggregateTotals()
            daily_entries[date_key] = {
                "date": date_key,
                "activity_ids": [],
            }
        daily_entries[date_key]["activity_ids"].append(activity["id"])
        daily_totals[date_key].add_activity(activity)

    recovery_records = canonical_recovery_records(data_dir)
    recovery_by_day = recovery_by_date(recovery_records)
    daily_list: List[Dict[str, Any]] = []

    for date_key in sorted(set(daily_entries.keys()) | set(recovery_by_day.keys())):
        entry = daily_entries.get(date_key, {"date": date_key, "activity_ids": []})
        entry["totals"] = (
            daily_totals[date_key].finalize() if date_key in daily_totals else None
        )
        entry["recovery_observations"] = recovery_by_day.get(date_key, [])
        entry["primary_recovery"] = choose_primary_recovery(entry["recovery_observations"])
        daily_list.append(entry)

    write_json(output_dir / "daily.json", daily_list)

    weeks_summary: List[Dict[str, Any]] = []
    plan_weeks = _load_plan_weeks(data_dir)
    if plan_weeks is not None:
        for week in plan_weeks:
            start = week.get("start_date")
            end = week.get("end_date")
            if not start or not end:
                continue
            totals = AggregateTotals()
            activity_ids: List[int] = []
            for date_key in _date_range(start, end):
                if date_key in daily_totals:
                    totals.merge(daily_totals[date_key])
                if date_key in daily_entries:
                    activity_ids.extend(daily_entries[date_key]["activity_ids"])

            hours_target = week.get("hours_target") or {}
            hours_min = hours_target.get("min")
            hours_max = hours_target.get("max")
            actual_hours = totals.moving_time_s / 3600.0
            meaningful_ride_hours = totals.meaningful_ride_time_s / 3600.0
            excluded_short_ride_hours = totals.excluded_short_ride_time_s / 3600.0
            status = None
            status_meaningful = None
            if hours_min is not None and hours_max is not None:
                if actual_hours < hours_min:
                    status = "below"
                elif actual_hours > hours_max:
                    status = "above"
                else:
                    status = "within"
                if meaningful_ride_hours < hours_min:
                    status_meaningful = "below"
                elif meaningful_ride_hours > hours_max:
                    status_meaningful = "above"
                else:
                    status_meaningful = "within"

            weeks_summary.append(
                {
                    "range_label": week.get("range_label"),
                    "start_date": start,
                    "end_date": end,
                    "plan": week,
                    "activity_ids": activity_ids,
                    "totals": totals.finalize(),
                    "target_hours": {"min": hours_min, "max": hours_max},
                    "actual_hours": round(actual_hours, 2),
                    "meaningful_ride_hours": round(meaningful_ride_hours, 2),
                    "excluded_short_ride_hours": round(excluded_short_ride_hours, 2),
                    "status": status,
                    "status_meaningful": status_meaningful,
                }
            )
    elif calendar_path and calendar_path.exists():
        calendar = read_json(calendar_path, default={}) or {}
        for week in calendar.get("weeks", []):
            start = week.get("start_date")
            end = week.get("end_date")
            if not start or not end:
                continue
            totals = AggregateTotals()
            activity_ids: List[int] = []
            for date_key in _date_range(start, end):
                if date_key in daily_totals:
                    totals.merge(daily_totals[date_key])
                if date_key in daily_entries:
                    activity_ids.extend(daily_entries[date_key]["activity_ids"])

            hours_min, hours_max = _parse_hours_target(
                (week.get("plan") or {}).get("Hours Target")
            )
            actual_hours = totals.moving_time_s / 3600.0
            meaningful_ride_hours = totals.meaningful_ride_time_s / 3600.0
            excluded_short_ride_hours = totals.excluded_short_ride_time_s / 3600.0
            status = None
            status_meaningful = None
            if hours_min is not None and hours_max is not None:
                if actual_hours < hours_min:
                    status = "below"
                elif actual_hours > hours_max:
                    status = "above"
                else:
                    status = "within"
                if meaningful_ride_hours < hours_min:
                    status_meaningful = "below"
                elif meaningful_ride_hours > hours_max:
                    status_meaningful = "above"
                else:
                    status_meaningful = "within"

            weeks_summary.append(
                {
                    "range_label": week.get("range_label"),
                    "start_date": start,
                    "end_date": end,
                    "plan": week.get("plan"),
                    "actual": week.get("actual"),
                    "activity_ids": activity_ids,
                    "totals": totals.finalize(),
                    "target_hours": {"min": hours_min, "max": hours_max},
                    "actual_hours": round(actual_hours, 2),
                    "meaningful_ride_hours": round(meaningful_ride_hours, 2),
                    "excluded_short_ride_hours": round(excluded_short_ride_hours, 2),
                    "status": status,
                    "status_meaningful": status_meaningful,
                }
            )

    covered_dates = {
        date_key
        for week in weeks_summary
        if week.get("start_date") and week.get("end_date")
        for date_key in _date_range(str(week["start_date"]), str(week["end_date"]))
    }
    uncovered_observation_dates = {
        str(entry["date"])
        for entry in daily_list
        if entry.get("date") and str(entry["date"]) not in covered_dates
    }
    if uncovered_observation_dates:
        observation_week_starts = sorted(
            {
                (
                    datetime.fromisoformat(date_key).date()
                    - timedelta(days=datetime.fromisoformat(date_key).date().weekday())
                )
                for date_key in uncovered_observation_dates
            }
        )
        for week_start in observation_week_starts:
            week_end = week_start + timedelta(days=6)
            start = week_start.isoformat()
            end = week_end.isoformat()
            totals = AggregateTotals()
            activity_ids: List[Any] = []
            for date_key in _date_range(start, end):
                if date_key in daily_totals:
                    totals.merge(daily_totals[date_key])
                if date_key in daily_entries:
                    activity_ids.extend(daily_entries[date_key]["activity_ids"])
            actual_hours = totals.moving_time_s / 3600.0
            meaningful_ride_hours = totals.meaningful_ride_time_s / 3600.0
            excluded_short_ride_hours = totals.excluded_short_ride_time_s / 3600.0
            has_activities = bool(activity_ids)
            weeks_summary.append(
                {
                    "range_label": f"{start} to {end}",
                    "start_date": start,
                    "end_date": end,
                    "plan": {
                        "source": "activity_history" if has_activities else "recovery_history",
                        "phase": "Observed history",
                        "primary_focus": "Recorded rides" if has_activities else "Recovery context",
                        "notes": "No training plan loaded; showing observed local data.",
                        "days": {},
                        "events": [],
                    },
                    "activity_ids": activity_ids,
                    "totals": totals.finalize(),
                    "target_hours": {"min": None, "max": None},
                    "actual_hours": round(actual_hours, 2),
                    "meaningful_ride_hours": round(meaningful_ride_hours, 2),
                    "excluded_short_ride_hours": round(excluded_short_ride_hours, 2),
                    "status": None,
                    "status_meaningful": None,
                }
            )

    weeks_summary.sort(key=lambda week: str(week.get("start_date") or ""))

    write_json(output_dir / "weekly.json", weeks_summary)

    garage_summary = _build_garage_summary(data_dir, output_dir)

    return {
        "activities": len(activities),
        "daily": len(daily_list),
        "garage": 0 if garage_summary is None else len(garage_summary.get("bikes", [])),
        "weeks": len(weeks_summary),
        "output_dir": str(output_dir),
    }
