from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .storage import read_json, write_json


DASHBOARD_LABELS_VERSION = 1


def _labels_path(data_dir: Path) -> Path:
    return data_dir / "plan" / "dashboard_labels.json"


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse_date(value: str) -> str:
    return date.fromisoformat(value).isoformat()


def _clean_label(entry: Any) -> dict[str, str] | None:
    if isinstance(entry, str):
        label = entry.strip()
        if not label:
            return None
        return {"label": label, "short": label[:1], "title": label}
    if not isinstance(entry, dict):
        return None
    label = str(entry.get("label") or "").strip()
    if not label:
        return None
    short = str(entry.get("short") or label[:1]).strip() or label[:1]
    title = str(entry.get("title") or label).strip() or label
    return {"label": label, "short": short, "title": title}


def _clean_labels(entries: Any) -> list[dict[str, str]]:
    if not isinstance(entries, list):
        return []
    labels: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for entry in entries:
        clean = _clean_label(entry)
        if clean is None:
            continue
        key = (clean["label"], clean["short"], clean["title"])
        if key in seen:
            continue
        labels.append(clean)
        seen.add(key)
    return labels


def _load_payload(path: Path) -> dict[str, Any]:
    payload = read_json(path, default={"version": DASHBOARD_LABELS_VERSION, "days": {}, "rides": {}})
    if not isinstance(payload, dict):
        return {"version": DASHBOARD_LABELS_VERSION, "days": {}, "rides": {}}

    raw_days = payload.get("days")
    raw_rides = payload.get("rides")
    days: dict[str, list[dict[str, str]]] = {}
    rides: dict[str, dict[str, Any]] = {}

    if isinstance(raw_days, dict):
        for day_key, entries in raw_days.items():
            labels = _clean_labels(entries)
            if labels:
                days[str(day_key)] = labels

    if isinstance(raw_rides, dict):
        for ride_id, entry in raw_rides.items():
            if not isinstance(entry, dict):
                continue
            labels = _clean_labels(entry.get("labels"))
            reaction = str(entry.get("reaction") or "").strip()
            updated_at = str(entry.get("updated_at") or "").strip()
            ride_entry: dict[str, Any] = {}
            if labels:
                ride_entry["labels"] = labels
            if reaction:
                ride_entry["reaction"] = reaction
            if updated_at:
                ride_entry["updated_at"] = updated_at
            if ride_entry:
                rides[str(ride_id)] = ride_entry

    return {
        "version": DASHBOARD_LABELS_VERSION,
        "days": days,
        "rides": rides,
    }


def load_dashboard_labels(data_dir: Path) -> dict[str, Any]:
    return _load_payload(_labels_path(data_dir))


def day_labels_by_date(data_dir: Path) -> dict[str, list[dict[str, str]]]:
    return load_dashboard_labels(data_dir)["days"]


def ride_annotations_by_id(data_dir: Path) -> dict[str, dict[str, Any]]:
    return load_dashboard_labels(data_dir)["rides"]


def add_dashboard_label(
    data_dir: Path,
    *,
    label: str,
    day: str | None = None,
    ride_id: str | None = None,
    short: str | None = None,
    title: str | None = None,
) -> dict[str, Any]:
    if bool(day) == bool(ride_id):
        raise ValueError("Provide exactly one of day or ride_id.")
    clean = _clean_label({"label": label, "short": short or "", "title": title or ""})
    if clean is None:
        raise ValueError("Dashboard label text is required.")

    path = _labels_path(data_dir)
    payload = _load_payload(path)
    if day:
        day_key = _parse_date(day)
        labels = payload["days"].setdefault(day_key, [])
        if clean not in labels:
            labels.append(clean)
        target = {"kind": "day", "id": day_key}
    else:
        ride_key = str(ride_id or "").strip()
        if not ride_key:
            raise ValueError("Ride id is required.")
        ride_entry = payload["rides"].setdefault(ride_key, {})
        labels = ride_entry.setdefault("labels", [])
        if clean not in labels:
            labels.append(clean)
        ride_entry["updated_at"] = _now_utc()
        target = {"kind": "ride", "id": ride_key}

    write_json(path, payload)
    return {"path": str(path), "target": target, "label": clean}


def react_to_ride(data_dir: Path, *, ride_id: str, emoji: str) -> dict[str, Any]:
    ride_key = str(ride_id or "").strip()
    reaction = str(emoji or "").strip()
    if not ride_key:
        raise ValueError("Ride id is required.")
    if not reaction:
        raise ValueError("Reaction emoji is required.")

    path = _labels_path(data_dir)
    payload = _load_payload(path)
    ride_entry = payload["rides"].setdefault(ride_key, {})
    ride_entry["reaction"] = reaction
    ride_entry["updated_at"] = _now_utc()
    write_json(path, payload)
    return {"path": str(path), "ride_id": ride_key, "reaction": reaction}
