"""Validated local plan edits and the common sanctioned-write history adapter."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import uuid
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any

from . import coaching_history as history
from . import recording_repair as files
from .planned_load import MAX_DAILY_HOURS, MAX_DAILY_TSS
from .planned_workouts import (
    MAX_DESCRIPTION_BYTES,
    MAX_STRUCTURED_WORKOUTS,
    _day,
    _events,
    _identifier,
    _legacy,
    _structured,
    _text,
    _unique_object,
)
from .storage import write_binary
from .workspace_lock import workspace_identity, workspace_lock


_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_LONG_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
MAX_EDIT_DAYS = 3660


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")


def file_digest(body: bytes | None) -> str | None:
    return hashlib.sha256(body).hexdigest() if body is not None else None


def _reject_constant(_value: str) -> None:
    raise ValueError("Plan edit JSON contains a nonfinite number.")


def _decode(body: bytes | None, default: Any) -> Any:
    if body is None:
        return copy.deepcopy(default)
    try:
        return json.loads(body, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("Plan edit JSON is invalid.") from None


def read_private_draft(path: Path, *, limit: int = history.MAX_FILE_BYTES) -> Any:
    """Read an explicitly selected bounded input; never follow its final symlink."""
    if not files._secure_files_supported():
        raise RuntimeError("This platform cannot safely read a plan-change draft.")
    source = Path(path).expanduser().absolute()
    descriptor = os.open(source.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        body = files._read(descriptor, source.name, limit)
    finally:
        os.close(descriptor)
    if body is None:
        raise ValueError("Plan-change draft is missing.")
    return _decode(body, None)


def plan_file_fingerprints(data_dir: Path) -> dict[str, str | None]:
    """Return only fixed relative names and digests, without reading arbitrary paths."""
    data_dir = Path(data_dir)
    if not history.history_write_available(data_dir):
        raise RuntimeError("Plan history is unavailable on this platform.")
    with files._directory(data_dir) as root:
        result = {
            name: file_digest(history._read_target(root, name))
            for name in sorted(history.ALLOWED_PLAN_FILES)
        }
        files._assert_generation(data_dir, root, None)
        return result


def scopes_for_dates(values: list[str], *, prefer_days: bool = False) -> list[dict[str, str]]:
    days: list[date] = []
    for value in values:
        try:
            parsed = _day(value)
        except (ValueError, TypeError):
            continue
        if parsed not in days:
            days.append(parsed)
    days.sort()
    if not days:
        return []
    if prefer_days and len(days) <= 16:
        return [
            {"kind": "day", "start_date": day.isoformat(), "end_date": day.isoformat()}
            for day in days
        ]
    first, last = days[0], days[-1]
    if (last - first).days <= 365:
        return [{"kind": "season", "start_date": first.isoformat(), "end_date": last.isoformat()}]
    if last.year - first.year >= 16:
        # A global change is honest; inventing or truncating a long source range is not.
        return []
    return [
        {
            "kind": "season",
            "start_date": max(first, date(year, 1, 1)).isoformat(),
            "end_date": min(last, date(year, 12, 31)).isoformat(),
        }
        for year in range(first.year, last.year + 1)
    ]


def change_request(
    operation: str,
    *,
    title: str,
    rationale: str,
    scopes: list[dict[str, str]] | None = None,
    supplied: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "idempotency_key": f"{operation}-{uuid.uuid4().hex}",
        "title": title,
        "rationale": rationale,
        "scopes": scopes or [],
    }
    thread_id = os.environ.get("CODEX_THREAD_ID", "")
    if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", thread_id):
        result["thread_id"] = thread_id
    if supplied is not None:
        if not isinstance(supplied, Mapping):
            raise ValueError("Plan change metadata must be an object.")
        result.update(supplied)
    return result


def commit_plan_files(
    data_dir: Path,
    updates: Mapping[str, bytes | None],
    *,
    request: dict[str, Any],
    expected_identity: tuple[int, int] | None = None,
    expected_hashes: Mapping[str, str | None] | None = None,
    legacy_fallback: bool = False,
    retry_from_current: bool = False,
    inferred_scopes: bool = False,
) -> dict[str, Any]:
    """Only existing commands may retain their old unsupported-platform behavior."""
    data_dir = Path(data_dir)
    if history.history_write_available(data_dir):
        if {"plan/weeks.json", "plan/events.json", "plan/workouts.json"}.intersection(updates):
            with files._directory(data_dir) as root:
                _validate_calendar_identities(root, updates, preserve_existing=legacy_fallback)
                files._assert_generation(data_dir, root, expected_identity)
        if retry_from_current:
            prior = history.plan_change_by_key(data_dir, request.get("idempotency_key"))
            if prior is not None:
                if inferred_scopes:
                    request = {**request, "scopes": prior["request"]["scopes"]}
                if set(prior["files"]) == set(updates) and all(
                    file_digest(body) == prior["files"][name]["after"]
                    for name, body in updates.items()
                ):
                    expected_hashes = {name: prior["files"][name]["before"] for name in updates}
        return history.apply_plan_change(
            data_dir,
            updates=updates,
            request=request,
            expected_identity=expected_identity,
            expected_hashes=expected_hashes,
        )
    if not legacy_fallback:
        raise RuntimeError("Plan history is unavailable on this platform.")
    if (
        not updates
        or set(updates) - history.ALLOWED_PLAN_FILES
        or any(body is None for body in updates.values())
    ):
        raise ValueError("Legacy plan update is not supported.")
    identity = expected_identity if expected_identity is not None else workspace_identity(data_dir)
    previous_values: dict[str, bytes | None] = {}
    for name, body in updates.items():
        target = data_dir / name
        if target.is_symlink():
            raise ValueError("Plan source cannot be a symbolic link.")
        previous = target.read_bytes() if target.exists() else None
        if expected_hashes is not None and file_digest(previous) != expected_hashes[name]:
            raise ValueError("Expected plan file changed; reread and retry.")
        previous_values[name] = previous
    changed = 0
    for name, body in updates.items():
        target = data_dir / name
        previous = previous_values[name]
        if previous != body:
            with workspace_lock(data_dir, expected_identity=identity):
                write_binary(target, body)
            changed += 1
    with workspace_lock(data_dir, expected_identity=identity):
        pass
    return {"id": None, "status": "unavailable", "created": False, "changed_files": changed}


def _source_load(value: Any) -> dict[str, float | None] | None:
    if value is None:
        return None
    fields = {"hours_min", "hours_max", "tss_min", "tss_max"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("Day load must contain explicit hours and TSS bounds.")
    result: dict[str, float | None] = {}
    for low_name, high_name, maximum in (
        ("hours_min", "hours_max", MAX_DAILY_HOURS),
        ("tss_min", "tss_max", MAX_DAILY_TSS),
    ):
        low, high = value[low_name], value[high_name]
        if low is None and high is None:
            result.update({low_name: None, high_name: None})
            continue
        if any(
            isinstance(item, bool) or not isinstance(item, (int, float)) or not 0 <= item <= maximum
            for item in (low, high)
        ):
            raise ValueError("Day load bounds must be finite and nonnegative.")
        if not math.isfinite(low) or not math.isfinite(high) or low > high:
            raise ValueError("Day load bounds must be ordered and finite.")
        result.update({low_name: low, high_name: high})
    return result


def _edit_days(document: Any, edits: Any) -> tuple[list[dict], list[str]]:
    if (
        not isinstance(document, list)
        or not isinstance(edits, list)
        or not 1 <= len(edits) <= MAX_EDIT_DAYS
    ):
        raise ValueError("Plan day edits must be a bounded nonempty list.")
    result = copy.deepcopy(document)
    dates: list[str] = []
    for edit in edits:
        if (
            not isinstance(edit, dict)
            or not {"date", "workout"} <= set(edit)
            or set(edit) - {"date", "workout", "load"}
        ):
            raise ValueError("Plan day edit fields are invalid.")
        day = _day(edit["date"])
        day_text = day.isoformat()
        if day_text in dates:
            raise ValueError("A plan day can be edited only once per change.")
        text = _text(edit["workout"], MAX_DESCRIPTION_BYTES, "Workout description")
        matching = []
        for week in result:
            if not isinstance(week, dict):
                continue
            try:
                start, end = _day(week.get("start_date")), _day(week.get("end_date"))
            except ValueError:
                continue
            if start <= day <= end:
                if (end - start).days > 6:
                    raise ValueError("A source week must span at most seven days.")
                matching.append(week)
        if len(matching) != 1:
            raise ValueError("A plan day must belong to exactly one source week.")
        week = matching[0]
        if not isinstance(week.get("days", {}), dict) or not isinstance(
            week.get("day_loads", {}), dict
        ):
            raise ValueError("Source week day fields are invalid.")
        weekday = _WEEKDAYS[day.weekday()]
        week.setdefault("days", {})[weekday] = text
        source_loads = week.setdefault("day_loads", {})
        load = _source_load(edit.get("load"))
        if load is None:
            source_loads.pop(weekday, None)
        else:
            source_loads[weekday] = load
        raw = week.get("raw")
        if isinstance(raw, dict):
            for name in (weekday, _LONG_WEEKDAYS[day.weekday()]):
                if name in raw:
                    raw[name] = text
        dates.append(day_text)
    return result, dates


def _structured_document(value: Any) -> dict[str, dict]:
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "workouts"}
        or type(value["version"]) is not int
        or value["version"] != 1
        or not isinstance(value["workouts"], list)
        or len(value["workouts"]) > MAX_STRUCTURED_WORKOUTS
    ):
        raise ValueError("Structured workout file must use the supported version 1 schema.")
    result = {}
    for item in value["workouts"]:
        normalized = _structured(item)
        if normalized["id"] in result:
            raise ValueError("Structured workout ids must be unique.")
        result[normalized["id"]] = copy.deepcopy(item)
    return result


def _edit_workouts(document: Any, edit: Any) -> tuple[dict, list[str]]:
    if not isinstance(edit, dict) or set(edit) - {"upsert", "remove"}:
        raise ValueError("Structured workout edit fields are invalid.")
    upserts, removals = edit.get("upsert", []), edit.get("remove", [])
    if (
        not isinstance(upserts, list)
        or not isinstance(removals, list)
        or not 1 <= len(upserts) + len(removals) <= MAX_STRUCTURED_WORKOUTS
    ):
        raise ValueError("Structured workout edits must be a bounded nonempty list.")
    result = _structured_document(document)
    touched: set[str] = set()
    dates: list[str] = []
    for identifier in removals:
        identifier = _identifier(identifier)
        if identifier in touched:
            raise ValueError("A structured workout can be edited only once per change.")
        touched.add(identifier)
        previous = result.pop(identifier, None)
        if previous is not None:
            dates.append(previous["date"])
    for item in upserts:
        normalized = _structured(item)
        identifier = normalized["id"]
        if identifier in touched:
            raise ValueError("A structured workout can be edited only once per change.")
        touched.add(identifier)
        if identifier in result:
            dates.append(result[identifier]["date"])
        dates.append(normalized["date"])
        result[identifier] = copy.deepcopy(item)
    if len(result) > MAX_STRUCTURED_WORKOUTS:
        raise ValueError("Structured workout file exceeds its supported limits.")
    return {
        "version": 1,
        "workouts": sorted(result.values(), key=lambda item: (item["date"], item["id"])),
    }, dates


def _calendar_identity_conflicts(root: int, updates: Mapping[str, bytes | None]) -> set[str]:
    def candidate(name: str, default: Any) -> Any:
        return _decode(updates.get(name, history._read_target(root, name)), default)

    structured = _structured_document(
        candidate("plan/workouts.json", {"version": 1, "workouts": []})
    )
    reserved: set[str] = set()
    for name, parser in (("plan/weeks.json", _legacy), ("plan/events.json", _events)):
        rows = candidate(name, [])
        if not isinstance(rows, list):
            continue
        for row in rows:
            try:
                reserved.update(item["id"] for item in parser([row]))
            except ValueError:
                # The dashboard intentionally tolerates unrelated malformed
                # legacy rows. Do not turn a structured-only edit into a full
                # legacy-export validation, but protect every valid source ID.
                continue
    return reserved.intersection(structured)


def _validate_calendar_identities(
    root: int,
    updates: Mapping[str, bytes | None],
    *,
    preserve_existing: bool = False,
) -> None:
    conflicts = _calendar_identity_conflicts(root, updates)
    if preserve_existing and conflicts:
        conflicts -= _calendar_identity_conflicts(root, {})
    if conflicts:
        raise ValueError("Structured workout ids must not collide with another plan source.")


def update_plan_from_draft(
    data_dir: Path,
    draft_path: Path,
    *,
    expected_identity: tuple[int, int] | None = None,
) -> dict[str, Any]:
    """Apply only reviewed day text/load or independently structured workout edits."""
    draft = read_private_draft(draft_path)
    required = {"version", "change", "expected_files"}
    if (
        not isinstance(draft, dict)
        or not required <= set(draft)
        or set(draft) - required - {"days", "workouts"}
        or type(draft["version"]) is not int
        or draft["version"] != 1
        or not {"days", "workouts"}.intersection(draft)
    ):
        raise ValueError("Plan edit must use the supported version 1 schema.")
    expected = draft["expected_files"]
    if (
        not isinstance(expected, dict)
        or set(expected) - history.ALLOWED_PLAN_FILES
        or any(
            value is not None and (not isinstance(value, str) or _SHA.fullmatch(value) is None)
            for value in expected.values()
        )
    ):
        raise ValueError("Plan edit expected file hashes are invalid.")
    if not isinstance(draft["change"], dict) or not {
        "idempotency_key",
        "title",
        "rationale",
    } <= set(draft["change"]):
        raise ValueError("Plan edit requires an explicit change reason and retry key.")
    data_dir = Path(data_dir)
    identity = expected_identity if expected_identity is not None else workspace_identity(data_dir)
    with workspace_lock(data_dir, expected_identity=identity), files._directory(data_dir) as root:
        updates: dict[str, bytes] = {}
        dates: list[str] = []
        if "days" in draft:
            name = "plan/weeks.json"
            value, changed_dates = _edit_days(
                _decode(history._read_target(root, name), []), draft["days"]
            )
            updates[name] = json_bytes(value)
            dates.extend(changed_dates)
        if "workouts" in draft:
            name = "plan/workouts.json"
            value, changed_dates = _edit_workouts(
                _decode(history._read_target(root, name), {"version": 1, "workouts": []}),
                draft["workouts"],
            )
            updates[name] = json_bytes(value)
            dates.extend(changed_dates)
        if not set(updates) <= set(expected):
            raise ValueError("Plan edit requires expected hashes for every edited file.")
        _validate_calendar_identities(root, updates)
        inferred_scopes = scopes_for_dates(dates, prefer_days=True)
        if "scopes" not in draft["change"]:
            prior = history.plan_change_by_key(data_dir, draft["change"]["idempotency_key"])
            if prior is not None:
                # A deletion or date move no longer has its old date in the
                # current file. Reuse the original reviewed scope on a retry;
                # the journal still checks request, after bytes and before hashes.
                inferred_scopes = prior["request"]["scopes"]
        request = {"scopes": inferred_scopes, **draft["change"]}
        files._assert_generation(data_dir, root, identity)
        return commit_plan_files(
            data_dir,
            updates,
            request=request,
            expected_identity=identity,
            expected_hashes={name: expected[name] for name in updates},
        )
