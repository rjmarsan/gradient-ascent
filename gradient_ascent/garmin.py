from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone, tzinfo
from pathlib import Path
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .storage import read_json, write_json


@dataclass
class GarminImportResult:
    export_path: str
    start_date: str | None
    end_date: str | None
    wellness_records: int
    sleep_records: int
    days_seen: int
    days_written: int
    created: int
    updated: int


def resolve_garmin_export_dir(export_path: Path) -> Path:
    if export_path.name == "DI_CONNECT" and export_path.is_dir():
        return export_path
    connect_dir = export_path / "DI_CONNECT"
    if connect_dir.is_dir():
        return connect_dir
    raise FileNotFoundError(
        f"Could not find DI_CONNECT inside export path: {export_path}"
    )


def _date_in_range(
    day: date,
    *,
    start_date: date | None,
    end_date: date | None,
) -> bool:
    if start_date and day < start_date:
        return False
    if end_date and day > end_date:
        return False
    return True


def _load_export_records(
    root: Path,
    pattern: str,
    *,
    start_date: date | None,
    end_date: date | None,
) -> Dict[str, Dict[str, Any]]:
    records: Dict[str, Dict[str, Any]] = {}
    for path in sorted(root.glob(pattern)):
        payload = read_json(path, default=[]) or []
        if not isinstance(payload, list):
            continue
        for item in payload:
            if not isinstance(item, dict):
                continue
            date_key = item.get("calendarDate")
            if not isinstance(date_key, str):
                continue
            try:
                day = date.fromisoformat(date_key)
            except ValueError:
                continue
            if not _date_in_range(day, start_date=start_date, end_date=end_date):
                continue
            records[date_key] = item
    return records


def _workspace_timezone(data_dir: Path) -> tzinfo:
    athlete = read_json(data_dir / "plan" / "athlete.json", default={}) or {}
    timezone_name = str(athlete.get("timezone") or "").strip() if isinstance(athlete, dict) else ""
    if timezone_name:
        try:
            return ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            pass
    return datetime.now().astimezone().tzinfo or timezone.utc


def _parse_garmin_timestamp_ms(
    value: Any,
    *,
    as_local: bool,
    local_timezone: tzinfo,
) -> Optional[int]:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if as_local:
        dt = dt.astimezone(local_timezone).replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _sleep_seconds(sleep_record: Dict[str, Any]) -> Optional[int]:
    total = 0
    found = False
    for key in ("deepSleepSeconds", "lightSleepSeconds", "remSleepSeconds"):
        value = sleep_record.get(key)
        if value is None:
            continue
        try:
            total += int(value)
            found = True
        except (TypeError, ValueError):
            continue
    return total if found else None


def _merge_resting_heart_rate_metric(
    existing: Any,
    *,
    date_key: str,
    value: Any,
) -> Dict[str, Any]:
    payload = dict(existing) if isinstance(existing, dict) else {}
    all_metrics = payload.get("allMetrics")
    if not isinstance(all_metrics, dict):
        all_metrics = {}
    metrics_map = all_metrics.get("metricsMap")
    if not isinstance(metrics_map, dict):
        metrics_map = {}
    current = metrics_map.get("WELLNESS_RESTING_HEART_RATE")
    if (not isinstance(current, list) or not current) and value is not None:
        metrics_map["WELLNESS_RESTING_HEART_RATE"] = [
            {
                "calendarDate": date_key,
                "value": value,
            }
        ]
    all_metrics["metricsMap"] = metrics_map
    payload["allMetrics"] = all_metrics
    payload.setdefault("groupedMetrics", {})
    payload.setdefault("statisticsStartDate", date_key)
    payload.setdefault("statisticsEndDate", date_key)
    return payload


def _body_battery_stat_value(body_battery: Any, stat_type: str) -> Optional[float]:
    if not isinstance(body_battery, dict):
        return None
    stats = body_battery.get("bodyBatteryStatList")
    if not isinstance(stats, list):
        return None
    for item in stats:
        if not isinstance(item, dict):
            continue
        if item.get("bodyBatteryStatType") != stat_type:
            continue
        value = item.get("statsValue")
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None
    return None


def _merge_sleep_payload(
    existing: Any,
    *,
    sleep_record: Dict[str, Any] | None,
    user_profile_pk: Any,
    sleep_resting_heart_rate: Any,
    body_battery_change: Optional[float],
    local_timezone: tzinfo,
) -> Dict[str, Any]:
    payload = dict(existing) if isinstance(existing, dict) else {}
    if sleep_record is None:
        return payload

    daily_sleep = dict(payload.get("dailySleepDTO") or {})
    start_gmt_ms = _parse_garmin_timestamp_ms(
        sleep_record.get("sleepStartTimestampGMT"), as_local=False, local_timezone=local_timezone
    )
    end_gmt_ms = _parse_garmin_timestamp_ms(
        sleep_record.get("sleepEndTimestampGMT"), as_local=False, local_timezone=local_timezone
    )
    start_local_ms = _parse_garmin_timestamp_ms(
        sleep_record.get("sleepStartTimestampGMT"), as_local=True, local_timezone=local_timezone
    )
    end_local_ms = _parse_garmin_timestamp_ms(
        sleep_record.get("sleepEndTimestampGMT"), as_local=True, local_timezone=local_timezone
    )
    confirmation = sleep_record.get("sleepWindowConfirmationType")
    sleep_time_seconds = _sleep_seconds(sleep_record)

    updates = {
        "autoSleepEndTimestampGMT": end_gmt_ms,
        "autoSleepStartTimestampGMT": start_gmt_ms,
        "averageRespirationValue": sleep_record.get("averageRespiration"),
        "awakeSleepSeconds": sleep_record.get("awakeSleepSeconds"),
        "calendarDate": sleep_record.get("calendarDate"),
        "deepSleepSeconds": sleep_record.get("deepSleepSeconds"),
        "highestRespirationValue": sleep_record.get("highestRespiration"),
        "id": start_gmt_ms,
        "lightSleepSeconds": sleep_record.get("lightSleepSeconds"),
        "lowestRespirationValue": sleep_record.get("lowestRespiration"),
        "remSleepSeconds": sleep_record.get("remSleepSeconds"),
        "retro": sleep_record.get("retro"),
        "sleepEndTimestampGMT": end_gmt_ms,
        "sleepEndTimestampLocal": end_local_ms,
        "sleepStartTimestampGMT": start_gmt_ms,
        "sleepStartTimestampLocal": start_local_ms,
        "sleepTimeSeconds": sleep_time_seconds,
        "sleepWindowConfirmationType": (
            str(confirmation).lower() if confirmation is not None else None
        ),
        "sleepWindowConfirmed": (
            None
            if confirmation is None
            else str(confirmation).upper() != "OFF_WRIST"
        ),
        "unmeasurableSleepSeconds": sleep_record.get("unmeasurableSeconds"),
        "userProfilePK": user_profile_pk,
    }
    if updates.get("remSleepSeconds") is not None:
        daily_sleep.setdefault("deviceRemCapable", True)
    daily_sleep.setdefault("napTimeSeconds", 0)
    daily_sleep.setdefault("sleepQualityTypePK", None)
    daily_sleep.setdefault("sleepResultTypePK", None)
    for key, value in updates.items():
        if value is not None or key not in daily_sleep:
            daily_sleep[key] = value
    payload["dailySleepDTO"] = daily_sleep
    if sleep_resting_heart_rate is not None:
        payload["restingHeartRate"] = sleep_resting_heart_rate
    if body_battery_change is not None and payload.get("bodyBatteryChange") is None:
        payload["bodyBatteryChange"] = body_battery_change
    payload.setdefault("remSleepData", None)
    payload.setdefault("skinTempDataExists", False)
    payload.setdefault("sleepLevels", None)
    payload.setdefault("sleepMovement", None)
    return payload


def _merge_garmin_export_day(
    existing: Any,
    *,
    date_key: str,
    wellness: Dict[str, Any] | None,
    sleep_record: Dict[str, Any] | None,
    local_timezone: tzinfo,
) -> Dict[str, Any]:
    payload = dict(existing) if isinstance(existing, dict) else {}
    payload["date"] = date_key

    user_profile_pk = None
    if isinstance(wellness, dict):
        user_profile_pk = wellness.get("userProfilePK")
    if user_profile_pk is None and isinstance(payload.get("heartrate"), dict):
        user_profile_pk = payload["heartrate"].get("userProfilePK")
    if user_profile_pk is None and isinstance(payload.get("sleep"), dict):
        user_profile_pk = (
            (payload["sleep"].get("dailySleepDTO") or {}).get("userProfilePK")
            if isinstance(payload["sleep"].get("dailySleepDTO"), dict)
            else None
        )

    if isinstance(wellness, dict):
        payload["wellness_summary"] = wellness

        heartrate = dict(payload.get("heartrate") or {})
        heartrate.setdefault("heartRateValueDescriptors", None)
        heartrate.setdefault("heartRateValues", None)
        heartrate.update(
            {
                "calendarDate": date_key,
                "endTimestampGMT": wellness.get("wellnessEndTimeGmt"),
                "endTimestampLocal": wellness.get("wellnessEndTimeLocal"),
                "startTimestampGMT": wellness.get("wellnessStartTimeGmt"),
                "startTimestampLocal": wellness.get("wellnessStartTimeLocal"),
                "userProfilePK": user_profile_pk,
            }
        )
        if wellness.get("minHeartRate") is not None:
            heartrate["minHeartRate"] = wellness.get("minHeartRate")
        if wellness.get("maxHeartRate") is not None:
            heartrate["maxHeartRate"] = wellness.get("maxHeartRate")
        resting_hr = wellness.get("currentDayRestingHeartRate")
        if resting_hr is None:
            resting_hr = wellness.get("restingHeartRate")
        if resting_hr is not None:
            heartrate["restingHeartRate"] = resting_hr
        payload["heartrate"] = heartrate

        stress = dict(payload.get("stress") or {})
        stress.setdefault("stressValueDescriptorsDTOList", [])
        stress.setdefault("stressValuesArray", [])
        stress.update(
            {
                "calendarDate": date_key,
                "endTimestampGMT": wellness.get("wellnessEndTimeGmt"),
                "endTimestampLocal": wellness.get("wellnessEndTimeLocal"),
                "startTimestampGMT": wellness.get("wellnessStartTimeGmt"),
                "startTimestampLocal": wellness.get("wellnessStartTimeLocal"),
                "userProfilePK": user_profile_pk,
            }
        )
        all_day_stress = wellness.get("allDayStress") or {}
        aggregators = all_day_stress.get("aggregatorList")
        if isinstance(aggregators, list):
            stress["aggregatorList"] = aggregators
            total = next(
                (
                    item
                    for item in aggregators
                    if isinstance(item, dict) and item.get("type") == "TOTAL"
                ),
                None,
            )
            if isinstance(total, dict):
                if total.get("averageStressLevel") is not None:
                    stress["avgStressLevel"] = total.get("averageStressLevel")
                if total.get("maxStressLevel") is not None:
                    stress["maxStressLevel"] = total.get("maxStressLevel")
        payload["stress"] = stress

        payload["resting_heart_rate"] = _merge_resting_heart_rate_metric(
            payload.get("resting_heart_rate"),
            date_key=date_key,
            value=heartrate.get("restingHeartRate"),
        )

        if wellness.get("respiration") is not None:
            payload["respiration"] = wellness.get("respiration")
        if wellness.get("bodyBattery") is not None:
            payload["body_battery"] = wellness.get("bodyBattery")

    body_battery_change = None
    if isinstance(wellness, dict):
        body_battery_change = _body_battery_stat_value(
            wellness.get("bodyBattery"),
            "DURINGSLEEP",
        )

    payload["sleep"] = _merge_sleep_payload(
        payload.get("sleep"),
        sleep_record=sleep_record,
        user_profile_pk=user_profile_pk,
        sleep_resting_heart_rate=((payload.get("heartrate") or {}).get("restingHeartRate")),
        body_battery_change=body_battery_change,
        local_timezone=local_timezone,
    )
    payload.setdefault("training_readiness", [])

    return payload


def import_garmin_export(
    data_dir: Path,
    export_path: Path,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
) -> GarminImportResult:
    connect_dir = resolve_garmin_export_dir(export_path)
    wellness_records = _load_export_records(
        connect_dir / "DI-Connect-Aggregator",
        "UDSFile_*.json",
        start_date=start_date,
        end_date=end_date,
    )
    sleep_records = _load_export_records(
        connect_dir / "DI-Connect-Wellness",
        "*_sleepData.json",
        start_date=start_date,
        end_date=end_date,
    )

    garmin_dir = Path(data_dir) / "garmin"
    created = 0
    updated = 0
    written = 0
    local_timezone = _workspace_timezone(data_dir)

    for date_key in sorted(set(wellness_records) | set(sleep_records)):
        path = garmin_dir / f"{date_key}.json"
        existing = read_json(path, default={}) or {}
        merged = _merge_garmin_export_day(
            existing,
            date_key=date_key,
            wellness=wellness_records.get(date_key),
            sleep_record=sleep_records.get(date_key),
            local_timezone=local_timezone,
        )
        if merged == existing:
            continue
        write_json(path, merged)
        written += 1
        if path.exists() and existing:
            updated += 1
        else:
            created += 1

    return GarminImportResult(
        export_path=str(export_path),
        start_date=start_date.isoformat() if start_date else None,
        end_date=end_date.isoformat() if end_date else None,
        wellness_records=len(wellness_records),
        sleep_records=len(sleep_records),
        days_seen=len(set(wellness_records) | set(sleep_records)),
        days_written=written,
        created=created,
        updated=updated,
    )
