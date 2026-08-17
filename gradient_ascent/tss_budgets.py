"""Explicit, revisioned coach budgets. No hours-to-TSS conversion or provider I/O."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from . import recording_repair as _recording_files
from .planned_load import MAX_WEEKLY_TSS, _mapping_range
from .planned_workouts import MAX_STRUCTURED_WORKOUTS, _structured
from .recording_repair import (
    _assert_generation,
    _directory,
    _read,
    _stat,
    _write,
)
from .workspace_lock import workspace_identity, workspace_lock


MAX_BUDGETS = 520
MAX_BUDGET_BYTES = 2 * 1024 * 1024
MAX_CONTEXT_BYTES = 8 * 1024 * 1024
MAX_RATIONALE_BYTES = 4096
MAX_CONDITIONS = 16
MAX_CONDITION_BYTES = 1024
_FILE = "tss_budgets.json"
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_DRAFT_KEYS = {
    "start_date",
    "end_date",
    "target_tss",
    "range",
    "ceiling_tss",
    "status",
    "rationale",
    "conditions",
    "override_source",
    "expected_plan_fingerprint",
}
_STORED_KEYS = (_DRAFT_KEYS - {"expected_plan_fingerprint"}) | {
    "revision",
    "authored_at",
    "plan_fingerprint",
}
_WEEK_KEYS = (
    "start_date",
    "end_date",
    "phase",
    "primary_focus",
    "hours_target",
    "tss_target",
    "day_loads",
    "days",
    "strength_rehab",
    "notes",
    "notes_2",
    "events",
)
_PROFILE_KEYS = (
    "ftp_w",
    "constraints",
    "weekly_availability",
    "disciplines",
    "experience_level",
    "race_category",
)


def _secure_files_supported() -> bool:
    return _recording_files._secure_files_supported()


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("TSS budget JSON contains duplicate keys.")
        result[key] = value
    return result


def _decode(body: bytes | None, default: Any) -> Any:
    if body is None:
        return default
    try:
        return json.loads(body, object_pairs_hook=_object, parse_constant=_reject_constant)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("TSS budget JSON is invalid.") from exc


def _reject_constant(value: str) -> None:
    raise ValueError("TSS budget JSON contains a nonfinite number.")


def _encode(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def _number(value: Any) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0 <= value <= MAX_WEEKLY_TSS
        or not math.isfinite(value)
    ):
        raise ValueError("TSS budget numbers must be finite, nonnegative and within bounds.")
    return float(value)


def _text(value: Any, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise ValueError("TSS budget text must be nonempty.")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        raise ValueError("TSS budget text must be valid UTF-8.") from None
    if size > limit or any(
        (ord(char) < 32 and char not in "\n\r\t") or ord(char) == 127 for char in value
    ):
        raise ValueError("TSS budget text exceeds its limits.")
    return value.strip()


def _day(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("TSS budget dates must be ISO dates.")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise ValueError("TSS budget dates must be ISO dates.") from None


def _key(entry: dict[str, Any]) -> tuple[str, str]:
    start, end = _day(entry.get("start_date")), _day(entry.get("end_date"))
    if not 0 <= (date.fromisoformat(end) - date.fromisoformat(start)).days <= 6:
        raise ValueError("TSS budget must cover a valid source week.")
    return start, end


def _entry(value: Any, *, stored: bool) -> dict[str, Any]:
    allowed = _STORED_KEYS if stored else _DRAFT_KEYS
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError("TSS budget entry contains unsupported fields.")
    start, end = _key(value)
    target = _number(value.get("target_tss"))
    bounds = value.get("range", {"min": target, "max": target})
    if not isinstance(bounds, dict) or set(bounds) != {"min", "max"}:
        raise ValueError("TSS budget range requires min and max.")
    low, high = _number(bounds["min"]), _number(bounds["max"])
    if not low <= target <= high:
        raise ValueError("TSS budget range must contain its target.")
    status = value.get("status", "provisional")
    if (
        status not in ("provisional", "confirmed")
        or type(value.get("override_source", False)) is not bool
    ):
        raise ValueError("TSS budget status or override_source is invalid.")
    conditions = value.get("conditions", [])
    if not isinstance(conditions, list) or len(conditions) > MAX_CONDITIONS:
        raise ValueError("TSS budget conditions must be a bounded list.")
    result = {
        "start_date": start,
        "end_date": end,
        "target_tss": target,
        "range": {"min": low, "max": high},
        "status": status,
        "rationale": _text(value.get("rationale"), MAX_RATIONALE_BYTES),
        "conditions": [_text(item, MAX_CONDITION_BYTES) for item in conditions],
        "override_source": value.get("override_source", False),
    }
    if "ceiling_tss" in value:
        ceiling = _number(value["ceiling_tss"])
        if ceiling < high:
            raise ValueError("TSS budget ceiling must cover the entire range.")
        result["ceiling_tss"] = ceiling
    if stored:
        revision = value.get("revision")
        authored = value.get("authored_at")
        fingerprint = value.get("plan_fingerprint")
        if (
            type(revision) is not int
            or not 1 <= revision <= 1_000_000_000
            or not isinstance(fingerprint, str)
            or not _SHA.fullmatch(fingerprint)
        ):
            raise ValueError("Stored TSS budget revision or fingerprint is invalid.")
        try:
            timestamp = datetime.fromisoformat(authored.replace("Z", "+00:00"))
            if timestamp.utcoffset() is None:
                raise ValueError
        except (AttributeError, TypeError, ValueError):
            raise ValueError("Stored TSS budget authored_at is invalid.") from None
        result.update(revision=revision, authored_at=authored, plan_fingerprint=fingerprint)
    elif "expected_plan_fingerprint" in value:
        fingerprint = value["expected_plan_fingerprint"]
        if not isinstance(fingerprint, str) or not _SHA.fullmatch(fingerprint):
            raise ValueError("Expected plan fingerprint is invalid.")
        result["expected_plan_fingerprint"] = fingerprint
    return result


def _collection(document: Any, *, stored: bool) -> dict[tuple[str, str], dict[str, Any]]:
    if (
        not isinstance(document, dict)
        or set(document) != {"version", "budgets"}
        or type(document.get("version")) is not int
        or document["version"] != 1
        or not isinstance(document.get("budgets"), list)
        or len(document["budgets"]) > MAX_BUDGETS
    ):
        raise ValueError("TSS budgets require the supported version 1 schema.")
    entries = {}
    for value in document["budgets"]:
        entry = _entry(value, stored=stored)
        key = _key(entry)
        if key in entries:
            raise ValueError("TSS budget dates must be unique.")
        entries[key] = entry
    ordered = sorted(entries)
    if any(current[0] <= previous[1] for previous, current in zip(ordered, ordered[1:])):
        raise ValueError("TSS budget weeks cannot overlap.")
    return entries


def _read_plan(root: int, name: str, default: Any) -> Any:
    try:
        with _directory(root, "plan") as plan:
            return _decode(_read(plan, name, MAX_CONTEXT_BYTES), default)
    except FileNotFoundError:
        return default


def _stored(root: int) -> dict[tuple[str, str], dict[str, Any]]:
    try:
        with _directory(root, "plan") as plan:
            info = _stat(plan, _FILE, MAX_BUDGET_BYTES)
            if info is not None and stat.S_IMODE(info.st_mode) & 0o077:
                raise ValueError("Stored TSS budgets must be owner-private.")
            body = _read(plan, _FILE, MAX_BUDGET_BYTES)
    except FileNotFoundError:
        body = None
    return _collection(_decode(body, {"version": 1, "budgets": []}), stored=True)


def _context(root: int) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, Any]]:
    raw_weeks = _read_plan(root, "weeks.json", [])
    if not isinstance(raw_weeks, list) or len(raw_weeks) > 10000:
        raise ValueError("Source planning weeks are invalid.")
    weeks = {}
    for value in raw_weeks:
        if not isinstance(value, dict):
            raise ValueError("Source planning week is invalid.")
        key = _key(value)
        if key in weeks:
            raise ValueError("Source planning weeks have duplicate dates.")
        weeks[key] = value
    events = _read_plan(root, "events.json", [])
    phases = _read_plan(root, "phases.json", [])
    athlete = _read_plan(root, "athlete.json", {})
    workouts_doc = _read_plan(root, "workouts.json", {"version": 1, "workouts": []})
    if (
        not isinstance(events, list)
        or not isinstance(phases, list)
        or not isinstance(athlete, dict)
        or not isinstance(workouts_doc, dict)
        or set(workouts_doc) != {"version", "workouts"}
        or type(workouts_doc.get("version")) is not int
        or workouts_doc["version"] != 1
        or not isinstance(workouts_doc.get("workouts"), list)
        or len(workouts_doc["workouts"]) > MAX_STRUCTURED_WORKOUTS
    ):
        raise ValueError("TSS budget planning context is invalid.")
    workouts = [_structured(value) for value in workouts_doc["workouts"]]
    if len({value["id"] for value in workouts}) != len(workouts):
        raise ValueError("Structured workout ids must be unique.")
    try:
        with _directory(root, "plan") as plan:
            goals = _read(plan, "goals.md", MAX_CONTEXT_BYTES) or b""
    except FileNotFoundError:
        goals = b""
    return weeks, {
        "events": events,
        "phases": phases,
        "workouts": workouts,
        "athlete": {key: athlete.get(key) for key in _PROFILE_KEYS},
        "goals_sha256": hashlib.sha256(goals).hexdigest(),
    }


def _overlaps(value: Any, start: str, end: str) -> bool:
    if not isinstance(value, dict):
        return False
    try:
        first = _day(value.get("date") or value.get("start_date"))
        last = _day(value.get("end_date") or first)
    except ValueError:
        return False
    return first <= end and last >= start


def _fingerprint(week: dict[str, Any], context: dict[str, Any]) -> str:
    start, end = _key(week)
    value = {
        "version": 1,
        "week": {key: week.get(key) for key in _WEEK_KEYS},
        "athlete": context["athlete"],
        "goals_sha256": context["goals_sha256"],
        **{
            key: sorted(
                (item for item in context[key] if _overlaps(item, start, end)),
                key=lambda item: json.dumps(item, sort_keys=True, allow_nan=False),
            )
            for key in ("events", "phases", "workouts")
        },
    }
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _resolve(
    entries: dict[tuple[str, str], dict[str, Any]],
    weeks: dict[tuple[str, str], dict[str, Any]],
    context: dict[str, Any],
) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        key: {
            **entry,
            "state": "orphaned"
            if key not in weeks
            else "current"
            if entry["plan_fingerprint"] == _fingerprint(weeks[key], context)
            else "needs_review",
        }
        for key, entry in entries.items()
    }


def _empty_portable(data_dir: Path) -> bool:
    path = data_dir / "plan" / _FILE
    try:
        info = path.lstat()
    except FileNotFoundError:
        return True
    if (
        any(part.is_symlink() for part in (data_dir, data_dir / "plan", path))
        or not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_size > MAX_BUDGET_BYTES
        or (
            hasattr(os, "getuid")
            and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077)
        )
    ):
        raise ValueError("Could not safely read private TSS budgets.")
    with path.open("rb") as handle:
        opened = os.fstat(handle.fileno())
        if (opened.st_dev, opened.st_ino, opened.st_size) != (
            info.st_dev,
            info.st_ino,
            info.st_size,
        ):
            raise ValueError("TSS budget file changed while opening.")
        body = handle.read(MAX_BUDGET_BYTES + 1)
    return not _collection(_decode(body, None), stored=True)


def load_tss_budgets(data_dir: Path) -> dict[tuple[str, str], dict[str, Any]]:
    data_dir = Path(data_dir).expanduser()
    if not (data_dir / "plan" / _FILE).exists() and not (data_dir / "plan" / _FILE).is_symlink():
        return {}
    if not _secure_files_supported():
        if _empty_portable(data_dir):
            return {}
        raise RuntimeError("This platform cannot safely read nonempty TSS budgets.")
    with _directory(data_dir) as root:
        entries = _stored(root)
        if not entries:
            return {}
        weeks, context = _context(root)
        _assert_generation(data_dir, root, None)
        return _resolve(entries, weeks, context)


def plan_tss_budget_fingerprints(data_dir: Path) -> dict[tuple[str, str], str]:
    if not _secure_files_supported():
        raise RuntimeError("This platform cannot safely inspect TSS budget context.")
    with _directory(Path(data_dir)) as root:
        weeks, context = _context(root)
        _assert_generation(Path(data_dir), root, None)
        return {key: _fingerprint(week, context) for key, week in weeks.items()}


def _summary(entries: dict[tuple[str, str], dict[str, Any]]) -> dict[str, int]:
    result = {
        "total": len(entries),
        "current": 0,
        "needs_review": 0,
        "orphaned": 0,
        "provisional": 0,
        "confirmed": 0,
    }
    for entry in entries.values():
        result[entry["state"]] += 1
        result[entry["status"]] += 1
    return result


def tss_budget_summary(data_dir: Path) -> dict[str, int]:
    return _summary(load_tss_budgets(data_dir))


def update_tss_budgets(
    data_dir: Path,
    draft_path: Path,
    *,
    replace: bool = False,
    expected_identity: tuple[int, int] | None = None,
) -> dict[str, int]:
    if type(replace) is not bool or not _secure_files_supported():
        raise RuntimeError("This platform cannot safely write TSS budgets.")
    data_dir = Path(data_dir).expanduser()
    draft_path = Path(draft_path).expanduser().absolute()
    source = os.open(draft_path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        draft = _collection(
            _decode(_read(source, draft_path.name, MAX_BUDGET_BYTES), None), stored=False
        )
    finally:
        os.close(source)
    identity = expected_identity if expected_identity is not None else workspace_identity(data_dir)
    with workspace_lock(data_dir, expected_identity=identity), _directory(data_dir) as root:
        old = _stored(root)
        weeks, context = _context(root)
        resolved = _resolve(old, weeks, context)
        result = {"created": 0, "updated": 0, "unchanged": 0, "removed": 0}
        merged = {} if replace else dict(old)
        for key, entry in draft.items():
            if key not in weeks:
                raise ValueError("TSS budget dates must exactly match an existing source week.")
            fingerprint = _fingerprint(weeks[key], context)
            expected = entry.pop("expected_plan_fingerprint", None)
            if expected is not None and expected != fingerprint:
                raise ValueError("Expected plan fingerprint changed; review the current plan.")
            if key in resolved and resolved[key]["state"] == "needs_review" and expected is None:
                raise ValueError(
                    "Reapproving a changed plan requires its expected_plan_fingerprint."
                )
            source_target = _mapping_range(weeks[key].get("tss_target"), MAX_WEEKLY_TSS)
            if (
                source_target is not None
                and not entry["override_source"]
                and (
                    source_target != (entry["range"]["min"], entry["range"]["max"])
                    or entry["target_tss"] != sum(source_target) / 2
                )
            ):
                raise ValueError("A conflicting source TSS target requires override_source:true.")
            previous = old.get(key)
            semantic_previous = {
                name: value
                for name, value in (previous or {}).items()
                if name not in {"revision", "authored_at", "plan_fingerprint"}
            }
            if (
                previous is not None
                and semantic_previous == entry
                and previous["plan_fingerprint"] == fingerprint
            ):
                merged[key] = previous
                result["unchanged"] += 1
                continue
            revision = previous["revision"] + 1 if previous else 1
            merged[key] = {
                **entry,
                "revision": revision,
                "authored_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                "plan_fingerprint": fingerprint,
            }
            result["updated" if previous else "created"] += 1
        result["removed"] = len(set(old) - set(merged))
        document = {"version": 1, "budgets": [merged[key] for key in sorted(merged)]}
        merged = _collection(document, stored=True)
        _assert_generation(data_dir, root, identity)
        latest_weeks, latest_context = _context(root)
        if any(
            key not in latest_weeks
            or merged[key]["plan_fingerprint"] != _fingerprint(latest_weeks[key], latest_context)
            for key in draft
        ):
            raise ValueError("Plan fingerprint changed during validation; review and retry.")
        _assert_generation(data_dir, root, identity)
        if merged != old:
            with _directory(root, "plan", create=True) as plan:
                _assert_generation(data_dir, root, identity)
                _write(plan, _FILE, _encode(document), MAX_BUDGET_BYTES)
        _assert_generation(data_dir, root, identity)
        return {**result, **_summary(_resolve(merged, latest_weeks, latest_context))}
