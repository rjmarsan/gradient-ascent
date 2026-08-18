"""Effective-dated FTP decisions, with an explicit legacy calculation baseline."""

from __future__ import annotations

import copy
import math
import re
from collections.abc import Mapping
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MAX_FTP_ENTRIES = 1000


class FTPHistoryError(ValueError):
    """Controlled, safe-to-display FTP validation error."""


def _day(value: Any) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is None:
        raise FTPHistoryError("FTP effective date must be YYYY-MM-DD.")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise FTPHistoryError("FTP effective date is invalid.") from None


def _watts(value: Any, *, legacy: bool = False) -> float | None:
    if legacy and value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(
        value, (int, float, str) if legacy else (int, float)
    ):
        raise FTPHistoryError("FTP must be a finite number from 1 to 3000 watts.")
    try:
        result = float(value)
    except (ValueError, OverflowError):
        raise FTPHistoryError("FTP must be a finite number from 1 to 3000 watts.") from None
    if not math.isfinite(result) or not 1 <= result <= 3000:
        raise FTPHistoryError("FTP must be a finite number from 1 to 3000 watts.")
    return result


def _legacy_watts(value: Any) -> float | None:
    try:
        return _watts(value, legacy=True)
    except FTPHistoryError:
        return None


def _history(profile: Mapping[str, Any]) -> dict[str, Any] | None:
    if "ftp_history" not in profile:
        return None
    value = profile["ftp_history"]
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "baseline_w", "entries"}
        or type(value["version"]) is not int
        or value["version"] != 1
    ):
        raise FTPHistoryError("FTP history has an unsupported schema.")
    _watts(value["baseline_w"], legacy=True)
    entries = value["entries"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_FTP_ENTRIES:
        raise FTPHistoryError("FTP history must contain 1 to 1000 dated entries.")
    previous = ""
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"effective_date", "ftp_w"}:
            raise FTPHistoryError("FTP history entry is invalid.")
        when = _day(entry["effective_date"])
        _watts(entry["ftp_w"])
        if when <= previous:
            raise FTPHistoryError("FTP history dates must be unique and sorted.")
        previous = when
    if _legacy_watts(profile.get("ftp_w")) != _watts(entries[-1]["ftp_w"]):
        raise FTPHistoryError("Current FTP does not match its dated history. Use set-ftp.")
    return value


def _selection(profile: Mapping[str, Any], when: Any) -> tuple[Any, str, str | None]:
    history = _history(profile)
    if history is None:
        return profile.get("ftp_w"), "current_profile", None
    try:
        day = _day(when)
    except FTPHistoryError:
        return None, "missing", None
    selected, source, effective = history["baseline_w"], "legacy_baseline", None
    for entry in history["entries"]:
        if entry["effective_date"] > day:
            break
        selected, source, effective = entry["ftp_w"], "dated_history", entry["effective_date"]
    return selected, source, effective


def resolve_ftp(profile: Mapping[str, Any], when: Any) -> dict[str, Any]:
    value, source, effective = _selection(profile, when)
    watts = _legacy_watts(value)
    return {
        "ftp_w": watts,
        "source": source if watts is not None else "missing",
        "effective_date": effective if watts is not None else None,
    }


def ftp_period_context(profile: Mapping[str, Any], start: str, end: str) -> dict[str, Any]:
    """Fingerprint only the FTP values relevant to this exact planning period.

    The old scalar's representation is retained for unchanged legacy fingerprints.
    """
    first, last = _day(start), _day(end)
    if first > last:
        raise FTPHistoryError("FTP planning period is reversed.")
    history = _history(profile)
    if history is None:
        return {"ftp_w": profile.get("ftp_w")}
    raw, _, _ = _selection(profile, first)
    result = {"ftp_w": raw}
    changes = []
    previous = _legacy_watts(raw)
    for entry in history["entries"]:
        if first < entry["effective_date"] <= last:
            watts = _watts(entry["ftp_w"])
            if watts != previous:
                changes.append(dict(entry))
                previous = watts
    if changes:
        result["ftp_changes"] = changes
    return result


def _today(profile: Mapping[str, Any]) -> date:
    try:
        zone = ZoneInfo(profile.get("timezone"))
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        return datetime.now().astimezone().date()
    return datetime.now(timezone.utc).astimezone(zone).date()


def updated_ftp_profile(
    profile: Mapping[str, Any],
    watts: Any,
    effective_date: str,
    *,
    replace: bool = False,
    today: date | None = None,
) -> dict[str, Any]:
    if not isinstance(profile, Mapping):
        raise FTPHistoryError("Athlete profile must be an object.")
    value = _watts(watts)
    value = int(value) if value.is_integer() else value
    when = _day(effective_date)
    if when > (today or _today(profile)).isoformat():
        raise FTPHistoryError("FTP changes cannot take effect in the future.")
    existing = _history(profile)
    baseline = existing["baseline_w"] if existing is not None else profile.get("ftp_w")
    if _legacy_watts(baseline) is None:
        baseline = None
    entries = copy.deepcopy(existing["entries"] if existing is not None else [])
    matching = next((entry for entry in entries if entry["effective_date"] == when), None)
    if matching is not None:
        if _watts(matching["ftp_w"]) != value and not replace:
            raise FTPHistoryError(
                "That effective date already has a different FTP. Use --replace-date to correct it."
            )
        matching["ftp_w"] = value
    else:
        entries.append({"effective_date": when, "ftp_w": value})
    entries.sort(key=lambda entry: entry["effective_date"])
    result = {
        **copy.deepcopy(dict(profile)),
        "ftp_w": entries[-1]["ftp_w"],
        "ftp_history": {"version": 1, "baseline_w": baseline, "entries": entries},
    }
    _history(result)
    return result


def set_ftp(
    data_dir: Path,
    watts: Any,
    effective_date: str,
    *,
    replace: bool = False,
    expected_profile_sha256: str | None = None,
    history_request: dict[str, Any] | None = None,
    expected_identity: tuple[int, int] | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    from . import coaching_history, recording_repair as files
    from .plan_changes import _decode, change_request, commit_plan_files, file_digest, json_bytes
    from .workspace_lock import workspace_identity, workspace_lock

    data_dir = Path(data_dir)
    identity = expected_identity if expected_identity is not None else workspace_identity(data_dir)
    with workspace_lock(data_dir, expected_identity=identity):
        if not coaching_history.history_write_available(data_dir):
            raise RuntimeError("Dated FTP changes require supported plan history.")
        with files._directory(data_dir) as root:
            before = coaching_history._read_target(root, "plan/athlete.json")
            profile = _decode(before, {})
            after = updated_ftp_profile(
                profile, watts, effective_date, replace=replace, today=today
            )
            after_bytes = json_bytes(after)
            if expected_profile_sha256 is not None:
                prior = (
                    coaching_history.plan_change_by_key(
                        data_dir, (history_request or {}).get("idempotency_key")
                    )
                    if (history_request or {}).get("idempotency_key")
                    else None
                )
                prior_file = (prior or {}).get("files", {}).get("plan/athlete.json", {})
                exact_retry = prior_file.get(
                    "before"
                ) == expected_profile_sha256 and prior_file.get("after") == file_digest(
                    before
                ) == file_digest(after_bytes)
                if (
                    not isinstance(expected_profile_sha256, str)
                    or re.fullmatch(r"[a-f0-9]{64}", expected_profile_sha256) is None
                    or (file_digest(before) != expected_profile_sha256 and not exact_retry)
                ):
                    raise FTPHistoryError(
                        "Athlete profile changed; reread its fingerprint and retry."
                    )
            files._assert_generation(data_dir, root, identity)
            result = commit_plan_files(
                data_dir,
                {"plan/athlete.json": after_bytes},
                request=change_request(
                    "set-ftp",
                    title="Update effective-dated FTP",
                    rationale="Record an explicitly supplied FTP and effective date.",
                    supplied=history_request,
                ),
                expected_identity=identity,
                expected_hashes={"plan/athlete.json": file_digest(before)},
                retry_from_current=True,
            )
            files._assert_generation(data_dir, root, identity)
    return {
        "current_ftp_w": after["ftp_w"],
        "effective_date": effective_date,
        "ftp_history_entries": len(after["ftp_history"]["entries"]),
        "history": result,
        "status": result["status"],
        "external_access": False,
    }


def ftp_history_status(data_dir: Path, *, on_date: str | None = None) -> dict[str, Any]:
    from . import coaching_history, recording_repair as files
    from .plan_changes import _decode, file_digest

    if not coaching_history.history_write_available(data_dir):
        raise RuntimeError("Dated FTP inspection is unavailable on this platform.")
    with files._directory(data_dir) as root:
        body = coaching_history._read_target(root, "plan/athlete.json")
        profile = _decode(body, {})
        if not isinstance(profile, dict):
            raise FTPHistoryError("Athlete profile must be an object.")
        history = _history(profile)
        when = _day(on_date) if on_date is not None else _today(profile).isoformat()
        result = {
            "date": when,
            **resolve_ftp(profile, when),
            "current_ftp_w": _legacy_watts(profile.get("ftp_w")),
            "baseline_w": _legacy_watts(history["baseline_w"])
            if history is not None
            else _legacy_watts(profile.get("ftp_w")),
            "entries": copy.deepcopy(history["entries"]) if history is not None else [],
            "profile_sha256": file_digest(body),
            "external_access": False,
        }
        files._assert_generation(data_dir, root, None)
        return result
