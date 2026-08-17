from __future__ import annotations

import math
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .device_corrections import apply_temperature_correction, load_temperature_corrections
from .storage import read_json, write_json


CANONICAL_DIR = Path("canonical")


def _safe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if result != result else result


def _parse_date(value: Any) -> str | None:
    if not value:
        return None
    text = str(value)
    return text[:10] if len(text) >= 10 else None


def _source(provider: str, record_id: Any, *, confidence: str = "high") -> dict[str, Any]:
    return {
        "provider": provider,
        "record_id": str(record_id),
        "confidence": confidence,
    }


def _activity(
    *,
    provider: str,
    record_id: Any,
    name: Any,
    sport_type: Any,
    start_date: Any,
    start_date_local: Any,
    moving_time_s: Any,
    elapsed_time_s: Any,
    distance_m: Any,
    elevation_gain_m: Any,
    average_heartrate: Any = None,
    max_heartrate: Any = None,
    average_watts: Any = None,
    weighted_average_watts: Any = None,
    kilojoules: Any = None,
    estimated_tss: Any = None,
    intensity_factor: Any = None,
    source_confidence: str = "high",
    raw: dict[str, Any] | None = None,
) -> dict[str, Any]:
    provider_id = f"{provider}:{record_id}"
    local_start = start_date_local or start_date
    return {
        "id": provider_id,
        "provider_id": str(record_id),
        "name": name,
        "sport_type": sport_type or "Unknown",
        "type": sport_type or "Unknown",
        "start_date": start_date,
        "start_date_local": local_start,
        "date": _parse_date(local_start),
        "moving_time_s": _safe_float(moving_time_s),
        "elapsed_time_s": _safe_float(elapsed_time_s),
        "distance_m": _safe_float(distance_m),
        "elevation_gain_m": _safe_float(elevation_gain_m),
        "average_heartrate": _safe_float(average_heartrate),
        "max_heartrate": _safe_float(max_heartrate),
        "average_watts": _safe_float(average_watts),
        "weighted_average_watts": _safe_float(weighted_average_watts),
        "intensity_factor": _safe_float(intensity_factor),
        "estimated_tss": _safe_float(estimated_tss),
        "kilojoules": _safe_float(kilojoules),
        "source": _source(provider, record_id, confidence=source_confidence),
        "raw": raw or {},
    }



def strava_raw_to_activity(
    item: dict[str, Any],
    temperature_corrections: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    corrected_item = apply_temperature_correction(item, temperature_corrections)
    record = _activity(
        provider="strava",
        record_id=item.get("id"),
        name=item.get("name"),
        sport_type=item.get("sport_type") or item.get("type"),
        start_date=item.get("start_date"),
        start_date_local=item.get("start_date_local"),
        moving_time_s=item.get("moving_time"),
        elapsed_time_s=item.get("elapsed_time"),
        distance_m=item.get("distance"),
        elevation_gain_m=item.get("total_elevation_gain"),
        average_heartrate=item.get("average_heartrate"),
        max_heartrate=item.get("max_heartrate"),
        average_watts=item.get("average_watts"),
        weighted_average_watts=item.get("weighted_average_watts"),
        kilojoules=item.get("kilojoules"),
        raw=item,
    )
    average_temp_c = _safe_float(corrected_item.get("average_temp"))
    if average_temp_c is not None:
        record["average_temp_c"] = average_temp_c
    correction = corrected_item.get("temperature_correction")
    if isinstance(correction, dict):
        record["average_temp_c_raw"] = _safe_float(corrected_item.get("average_temp_raw"))
        record["temperature_correction"] = correction
    return record



def _strava_activities(data_dir: Path) -> list[dict[str, Any]]:
    payload = read_json(data_dir / "strava" / "activities.json", default={}) or {}
    activities = payload.values() if isinstance(payload, dict) else payload if isinstance(payload, list) else []
    temperature_corrections = load_temperature_corrections(data_dir)
    records: list[dict[str, Any]] = []
    for item in activities:
        if not isinstance(item, dict) or item.get("id") is None:
            continue
        records.append(strava_raw_to_activity(item, temperature_corrections))
    return records


def _recording_activities(data_dir: Path) -> list[dict[str, Any]]:
    payload = read_json(data_dir / "recordings" / "activities.json", default={}) or {}
    activities = payload.values() if isinstance(payload, dict) else []
    return [
        _activity(
            provider="recording",
            record_id=item.get("id"),
            name=item.get("name"),
            sport_type=item.get("sport_type") or item.get("type"),
            start_date=item.get("start_date"),
            start_date_local=item.get("start_date_local"),
            moving_time_s=item.get("moving_time"),
            elapsed_time_s=item.get("elapsed_time"),
            distance_m=item.get("distance"),
            elevation_gain_m=item.get("total_elevation_gain"),
            average_heartrate=item.get("average_heartrate"),
            max_heartrate=item.get("max_heartrate"),
            average_watts=item.get("average_watts"),
            weighted_average_watts=item.get("weighted_average_watts"),
            kilojoules=item.get("kilojoules"),
            raw=item,
        )
        for item in activities
        if isinstance(item, dict) and item.get("id")
    ]


def _garmin_recovery(data_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted((data_dir / "garmin").glob("*.json")):
        payload = read_json(path, default={}) or {}
        if not isinstance(payload, dict):
            continue
        heartrate = payload.get("heartrate") or {}
        sleep = payload.get("sleep") or {}
        daily_sleep = sleep.get("dailySleepDTO") or {}
        stress = payload.get("stress") or {}
        readiness = payload.get("training_readiness") or {}
        if not isinstance(readiness, dict):
            readiness = {}
        sleep_scores = daily_sleep.get("sleepScores") or {}
        overall_sleep_score = sleep_scores.get("overall") or {}
        records.append(
            {
                "id": f"garmin:{path.stem}",
                "date": path.stem,
                "resting_hr": _safe_float(heartrate.get("restingHeartRate")),
                "hrv_ms": None,
                "sleep_duration_s": _safe_float(daily_sleep.get("sleepTimeSeconds")),
                "sleep_score": _safe_float(overall_sleep_score.get("value")),
                "readiness_score": _safe_float(readiness.get("score")),
                "recovery_score": None,
                "stress_avg": _safe_float(stress.get("avgStressLevel")),
                "source": _source("garmin", path.stem),
            }
        )
    return records



def _apple_health_records(data_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    workouts = read_json(data_dir / "apple_health" / "workouts.json", default=[]) or []
    recovery = read_json(data_dir / "apple_health" / "recovery.json", default=[]) or []
    activities: list[dict[str, Any]] = []
    for item in workouts if isinstance(workouts, list) else []:
        if not isinstance(item, dict):
            continue
        workout_type = str(item.get("workout_type") or "Workout")
        workout_type = workout_type.removeprefix("HKWorkoutActivityType") or "Workout"
        workout_label = re.sub(r"(?<!^)(?=[A-Z])", " ", workout_type)
        activities.append(
            _activity(
                provider="apple_health",
                record_id=item.get("id"),
                name=workout_label,
                sport_type=workout_label,
                start_date=item.get("start_date"),
                start_date_local=item.get("start_date"),
                moving_time_s=item.get("duration_s"),
                elapsed_time_s=item.get("duration_s"),
                distance_m=item.get("distance_m"),
                elevation_gain_m=None,
                kilojoules=item.get("energy_kj"),
                source_confidence="medium",
                raw=item,
            )
        )
    recovery_records = [
        {
            **item,
            "source": _source("apple_health", item.get("date"), confidence="medium"),
        }
        for item in recovery
        if isinstance(item, dict)
    ]
    return activities, recovery_records


def canonical_activity_records(data_dir: Path) -> list[dict[str, Any]]:
    from .external_sync import load_external_sync_manifests

    records = _strava_activities(data_dir)
    records.extend(_recording_activities(data_dir))
    apple_activities, _ = _apple_health_records(data_dir)
    records.extend(apple_activities)
    for manifest in load_external_sync_manifests(data_dir):
        provider = manifest["provider"]["id"]
        label = manifest["provider"]["label"]
        for item in manifest["activities"]:
            records.append(
                _activity(
                    provider=provider,
                    record_id=item["id"],
                    name=item.get("name") or f"{label} activity",
                    sport_type=item.get("sport_type"),
                    start_date=item.get("start_date") or item.get("start_date_local"),
                    start_date_local=item.get("start_date_local") or item.get("start_date"),
                    moving_time_s=item.get("moving_time_s"),
                    elapsed_time_s=item.get("elapsed_time_s") or item.get("moving_time_s"),
                    distance_m=item.get("distance_m"),
                    elevation_gain_m=item.get("elevation_gain_m"),
                    average_heartrate=item.get("average_heartrate"),
                    max_heartrate=item.get("max_heartrate"),
                    average_watts=item.get("average_watts"),
                    weighted_average_watts=item.get("weighted_average_watts"),
                    kilojoules=item.get("kilojoules"),
                    estimated_tss=item.get("estimated_tss"),
                    intensity_factor=item.get("intensity_factor"),
                    source_confidence="medium",
                )
            )
    return sorted(records, key=lambda item: (item.get("start_date_local") or "", item["id"]))


def _activity_start(record: dict[str, Any]) -> datetime | None:
    raw_value = record.get("start_date_local") or record.get("start_date")
    if not raw_value:
        return None
    try:
        return datetime.fromisoformat(str(raw_value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _sport_family(value: Any) -> str:
    text = str(value or "").lower()
    if any(token in text for token in ("ride", "bike", "cycling")):
        return "bike"
    if "run" in text:
        return "run"
    if "walk" in text or "hike" in text:
        return "walk"
    if "swim" in text:
        return "swim"
    return text or "unknown"


def _close_enough(left: float | None, right: float | None, *, absolute: float, ratio: float) -> bool:
    if left is None or right is None:
        return True
    return abs(left - right) <= max(absolute, max(left, right) * ratio)


def _is_duplicate_activity(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_provider = ((left.get("source") or {}).get("provider")) or ""
    right_provider = ((right.get("source") or {}).get("provider")) or ""
    same_provider_distinct_ids = left_provider == right_provider and left.get("id") != right.get("id")
    if left.get("date") != right.get("date"):
        return False
    if _sport_family(left.get("sport_type")) != _sport_family(right.get("sport_type")):
        return False
    left_start = _activity_start(left)
    right_start = _activity_start(right)
    if same_provider_distinct_ids:
        left_sport = left.get("sport_type")
        right_sport = right.get("sport_type")
        measurements = (
            left.get("moving_time_s"),
            right.get("moving_time_s"),
            left.get("distance_m"),
            right.get("distance_m"),
        )
        if (
            not left_provider
            or not isinstance(left_sport, str)
            or not isinstance(right_sport, str)
            or left_sport.strip().casefold() != right_sport.strip().casefold()
            or not left_sport.strip()
            or left_start is None
            or right_start is None
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
                for value in measurements
            )
        ):
            return False
        if abs((left_start - right_start).total_seconds()) > 30:
            return False
        return _close_enough(
            left.get("moving_time_s"), right.get("moving_time_s"), absolute=30, ratio=0.01
        ) and _close_enough(left.get("distance_m"), right.get("distance_m"), absolute=200, ratio=0.01)
    if left_start and right_start and abs((left_start - right_start).total_seconds()) > 15 * 60:
        return False
    if not _close_enough(left.get("moving_time_s"), right.get("moving_time_s"), absolute=20 * 60, ratio=0.2):
        return False
    if not _close_enough(left.get("distance_m"), right.get("distance_m"), absolute=2000, ratio=0.2):
        return False
    return True


def _activity_preference(record: dict[str, Any]) -> tuple[int, int]:
    provider = ((record.get("source") or {}).get("provider")) or ""
    provider_rank = {
        "strava": 3,
        "recording": 2,
        "apple_health": 1,
    }
    signal_count = sum(
        record.get(key) is not None
        for key in (
            "moving_time_s",
            "distance_m",
            "elevation_gain_m",
            "average_heartrate",
            "average_watts",
            "weighted_average_watts",
        )
    )
    return provider_rank.get(provider, 0), signal_count


def resolve_activity_records(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    clusters: list[list[dict[str, Any]]] = []
    clusters_by_date: dict[Any, list[list[dict[str, Any]]]] = {}
    for record in records:
        try:
            date_clusters = clusters_by_date.setdefault(record.get("date"), [])
        except TypeError:
            date_clusters = clusters
        for cluster in date_clusters:
            if any(_is_duplicate_activity(record, existing) for existing in cluster):
                provider = ((record.get("source") or {}).get("provider")) or ""
                if provider and any(
                    (((existing.get("source") or {}).get("provider")) or "") == provider
                    and existing.get("id") != record.get("id")
                    and not _is_duplicate_activity(record, existing)
                    for existing in cluster
                ):
                    continue
                cluster.append(record)
                break
        else:
            cluster = [record]
            clusters.append(cluster)
            if date_clusters is not clusters:
                date_clusters.append(cluster)

    resolved: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []
    for cluster in clusters:
        primary = max(cluster, key=_activity_preference)
        resolved.append(primary)
        links.append(
            {
                "primary_id": primary["id"],
                "candidate_ids": [item["id"] for item in cluster],
                "duplicate_count": max(len(cluster) - 1, 0),
            }
        )
    return (
        sorted(resolved, key=lambda item: (item.get("start_date_local") or "", item["id"])),
        links,
    )


def canonical_recovery_records(data_dir: Path) -> list[dict[str, Any]]:
    from .external_sync import load_external_sync_manifests

    _, records = _apple_health_records(data_dir)
    records.extend(_garmin_recovery(data_dir))
    for manifest in load_external_sync_manifests(data_dir):
        provider = manifest["provider"]["id"]
        for item in manifest["recovery"]:
            records.append(
                {
                    "id": f"{provider}:{item['id']}",
                    "date": item["date"],
                    "resting_hr": _safe_float(item.get("resting_hr")),
                    "hrv_ms": _safe_float(item.get("hrv_ms")),
                    "sleep_duration_s": _safe_float(item.get("sleep_duration_s")),
                    "sleep_score": _safe_float(item.get("sleep_score")),
                    "readiness_score": _safe_float(item.get("readiness_score")),
                    "recovery_score": _safe_float(item.get("recovery_score")),
                    "stress_avg": _safe_float(item.get("stress_avg")),
                    "source": _source(provider, item["id"], confidence="medium"),
                }
            )
    return sorted(records, key=lambda item: (item.get("date") or "", item["id"]))


def canonical_planned_workouts(data_dir: Path) -> list[dict[str, Any]]:
    return []


def recovery_by_date(records: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in records:
        date_key = item.get("date")
        if date_key:
            grouped[str(date_key)].append(item)
    return dict(grouped)


def _record_signal_count(record: dict[str, Any]) -> int:
    return sum(
        record.get(key) is not None
        for key in (
            "resting_hr",
            "hrv_ms",
            "sleep_duration_s",
            "sleep_score",
            "readiness_score",
            "recovery_score",
            "stress_avg",
        )
    )


def choose_primary_recovery(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not records:
        return None
    return max(
        records,
        key=lambda item: (
            _record_signal_count(item),
            ((item.get("source") or {}).get("provider") == "apple_health"),
        ),
    )


def build_canonical_files(data_dir: Path) -> dict[str, Any]:
    activities = canonical_activity_records(data_dir)
    resolved_activities, activity_links = resolve_activity_records(activities)
    recovery = canonical_recovery_records(data_dir)
    planned = canonical_planned_workouts(data_dir)
    canonical_dir = data_dir / CANONICAL_DIR
    write_json(canonical_dir / "activities.json", activities)
    write_json(canonical_dir / "resolved_activities.json", resolved_activities)
    write_json(canonical_dir / "activity_links.json", activity_links)
    write_json(canonical_dir / "recovery.json", recovery)
    write_json(canonical_dir / "planned_workouts.json", planned)
    return {
        "activities": len(activities),
        "resolved_activities": len(resolved_activities),
        "recovery": len(recovery),
        "planned_workouts": len(planned),
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
