"""Bounded, local-only repair from immutable, digest-verified recordings."""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .activity_files import RECORDING_STREAM_VERSION, parse_activity_recording


MAX_ORIGINAL_BYTES = 64 * 1024 * 1024
MAX_JSON_BYTES = 128 * 1024 * 1024
MAX_REPAIRS = 100
MAX_REPAIR_BYTES = 256 * 1024 * 1024
_ID = re.compile(r"recording-([a-f0-9]{64})\Z")
_TRIP = re.compile(r"[1-9][0-9]{0,31}\Z")


def _secure_files_supported() -> bool:
    return hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY")


def _owner(info: os.stat_result) -> None:
    if (hasattr(os, "getuid") and info.st_uid != os.getuid()) or stat.S_IMODE(info.st_mode) & 0o022:
        raise ValueError("Recording files must be owned and not writable by other users.")


@contextmanager
def _directory(root: Path | int, *parts: str, create: bool = False) -> Iterator[int]:
    if not _secure_files_supported() or (isinstance(root, Path) and ".." in root.parts):
        raise ValueError("Cannot safely open the recording directory.")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.dup(root) if isinstance(root, int) else os.open(root, flags)
    try:
        _owner(os.fstat(descriptor))
        for part in parts:
            if Path(part).name != part or part in {"", ".", ".."}:
                raise ValueError("Recording path escaped its workspace.")
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = os.open(part, flags, dir_fd=descriptor)
            try:
                _owner(os.fstat(child))
            except Exception:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def _stat(directory: int, name: str, limit: int) -> os.stat_result | None:
    if Path(name).name != name or name in {"", ".", ".."}:
        raise ValueError("Invalid recording filename.")
    try:
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None
    _owner(info)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
        raise ValueError("Recording file is not a bounded regular file.")
    return info


def _read(directory: int, name: str, limit: int) -> bytes | None:
    expected = _stat(directory, name, limit)
    if expected is None:
        return None
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(handle.fileno())
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            expected.st_dev,
            expected.st_ino,
            expected.st_size,
            expected.st_mtime_ns,
        ):
            raise ValueError("Recording file changed while opening.")
        body = handle.read(limit + 1)
        after = os.fstat(handle.fileno())
    if len(body) > limit or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise ValueError("Recording file changed while reading.")
    return body


def _write(directory: int, name: str, body: bytes, limit: int) -> None:
    if len(body) > limit:
        raise ValueError("Recording file exceeds its size limit.")
    _stat(directory, name, limit)
    temporary = f".{name}.{secrets.token_hex(12)}.tmp"
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        _stat(directory, name, limit)
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass


def _json(body: bytes | None, default: Any) -> Any:
    return default if body is None else json.loads(body)


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def retain_recording_original(
    data_dir: Path, path: Path, identifier: str, format_name: str
) -> None:
    """Retain only an explicitly imported original, never arbitrary source paths."""
    if not _secure_files_supported():
        return
    match = _ID.fullmatch(identifier)
    if match is None or format_name not in {"fit", "tcx", "gpx"}:
        raise ValueError("Invalid recording identity.")
    # The explicitly selected input may live in a shared upload/temp directory.
    # Its opened file, ownership, link count, size and expected digest remain
    # checked; unlike managed destinations, its parent need not be private.
    source = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = _stat(source, path.name, (1 << 63) - 1)
        if info is not None and info.st_size > MAX_ORIGINAL_BYTES:
            # Retention is an optional repair capability, not a new limit on
            # the existing standalone/upload recording importer.
            return
        body = _read(source, path.name, MAX_ORIGINAL_BYTES)
    finally:
        os.close(source)
    if body is None or hashlib.sha256(body).hexdigest() != match[1]:
        raise ValueError("Recording original does not match its identity.")
    with _directory(data_dir, "recordings", "files", create=True) as files:
        name = f"{match[1]}.{format_name}"
        existing = _read(files, name, MAX_ORIGINAL_BYTES)
        if existing is None:
            _write(files, name, body, MAX_ORIGINAL_BYTES)
        elif existing != body:
            raise ValueError("Retained recording original has changed.")


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def _old_lap_np(laps: Any) -> float | None:
    if not isinstance(laps, list):
        return None
    weighted = []
    for lap in laps:
        if not isinstance(lap, dict):
            return None
        value = _number(lap.get("weighted_average_watts"))
        duration = _number(lap.get("moving_time") or lap.get("elapsed_time"))
        if value is not None and duration is not None and duration > 0:
            weighted.append((value, duration))
    total = sum(duration for _, duration in weighted)
    return sum(value * duration for value, duration in weighted) / total if total else None


def merge_recording_metrics(
    existing: dict[str, Any], parsed: dict[str, Any], stored_laps: Any
) -> dict[str, Any]:
    """Replace only proven old parser NP; never overwrite a marked/user source value."""
    result = dict(existing)
    summary = parsed.get("summary") or {}
    original_laps = (parsed.get("laps") or {}).get("laps")
    old_np = _old_lap_np(original_laps)
    prior_np = _number(existing.get("weighted_average_watts"))
    proven_old_np = (
        not existing.get("weighted_average_watts_source")
        and old_np is not None
        and prior_np is not None
        and stored_laps == original_laps
        and math.isclose(prior_np, old_np, rel_tol=1e-12, abs_tol=1e-9)
    )
    if proven_old_np:
        result.pop("weighted_average_watts", None)
    for key in ("weighted_average_watts", "estimated_tss", "intensity_factor", "timer_time"):
        if _number(result.get(key)) is None and _number(summary.get(key)) is not None:
            result[key] = summary[key]
            if key == "weighted_average_watts":
                result["weighted_average_watts_source"] = "fit_session"
    return result


def _original(
    data_dir: Path | int, row: dict[str, Any], digest: str, budget: list[int]
) -> bytes | None:
    format_name = row.get("recording_format")
    if format_name not in {"fit", "tcx", "gpx"}:
        return None
    candidates = [(("recordings", "files"), f"{digest}.{format_name}")]
    trip_id = row.get("source_activity_id")
    if (
        row.get("source_provider") == "ridewithgps"
        and isinstance(trip_id, str)
        and _TRIP.fullmatch(trip_id)
        and format_name == "tcx"
    ):
        candidates.append((("integrations", "ridewithgps", "files"), f"{trip_id}-{digest}.tcx"))
    for parts, name in candidates:
        try:
            with _directory(data_dir, *parts) as directory:
                info = _stat(directory, name, MAX_ORIGINAL_BYTES)
                if info is None:
                    continue
                if info.st_size > budget[0]:
                    return None
                # Invalid hashes/parses still consume the bounded I/O budget.
                budget[0] -= info.st_size
                body = _read(directory, name, info.st_size)
        except FileNotFoundError:
            continue
        if body is not None:
            if hashlib.sha256(body).hexdigest() != digest:
                raise ValueError("Recording original does not match its identity.")
            return body
    return None


def _assert_generation(
    data_dir: Path, root: int, expected_identity: tuple[int, int] | None
) -> None:
    try:
        current = data_dir.stat(follow_symlinks=False)
        pinned = os.fstat(root)
    except OSError as exc:
        raise RuntimeError(
            "Gradient Ascent workspace generation changed; restart and retry."
        ) from exc
    if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (
        pinned.st_dev,
        pinned.st_ino,
    ):
        raise RuntimeError("Gradient Ascent workspace generation changed; restart and retry.")
    if expected_identity is not None:
        from .workspace_lock import workspace_lock

        with workspace_lock(data_dir, expected_identity=expected_identity):
            pass


def repair_recordings(
    data_dir: Path, *, expected_identity: tuple[int, int] | None = None
) -> dict[str, int]:
    """Caller holds the workspace-generation lock; only aggregate results escape."""
    result = {"repaired": 0, "current": 0, "unavailable": 0, "errors": 0, "unsupported": 0}
    if not _secure_files_supported():
        result["unsupported"] = 1
        return result
    with _directory(data_dir) as root:
        _assert_generation(data_dir, root, expected_identity)
        result = _repair_recordings(data_dir, root, expected_identity, result)
        _assert_generation(data_dir, root, expected_identity)
        return result


def _repair_recordings(
    data_dir: Path, root: int, expected_identity: tuple[int, int] | None, result: dict[str, int]
) -> dict[str, int]:
    try:
        with _directory(root, "recordings") as directory:
            activities = _json(_read(directory, "activities.json", MAX_JSON_BYTES), {})
    except FileNotFoundError:
        return result
    if not isinstance(activities, dict):
        raise ValueError("Recording index is invalid.")
    budget = [MAX_REPAIR_BYTES]
    attempted = 0
    changed = False
    for identifier, row in activities.items():
        match = _ID.fullmatch(identifier) if isinstance(identifier, str) else None
        if (
            not match
            or not isinstance(row, dict)
            or row.get("id") != identifier
            or row.get("import_source") != "local_recording"
        ):
            continue
        if row.get("recording_parser_version") == RECORDING_STREAM_VERSION:
            result["current"] += 1
            continue
        if attempted >= MAX_REPAIRS or budget[0] <= 0:
            result["unavailable"] += 1
            continue
        try:
            body = _original(root, row, match[1], budget)
            if body is None:
                result["unavailable"] += 1
                continue
            attempted += 1
            try:
                parsed = parse_activity_recording(
                    io.BytesIO(body), f"original.{row['recording_format']}"
                )
            except Exception:
                # Decoder diagnostics can contain recording contents/paths.
                result["errors"] += 1
                continue
            _assert_generation(data_dir, root, expected_identity)
            try:
                with _directory(root, "recordings", "laps") as directory:
                    stored = _json(_read(directory, f"{identifier}.json", MAX_JSON_BYTES), {})
            except FileNotFoundError:
                stored = {}
            updated = merge_recording_metrics(
                row, parsed, stored.get("laps") if isinstance(stored, dict) else None
            )
            streams = {**parsed["streams"], "source": "local_recording"}
            _assert_generation(data_dir, root, expected_identity)
            with _directory(root, "recordings", "streams", create=True) as directory:
                _assert_generation(data_dir, root, expected_identity)
                _write(directory, f"{identifier}.json", _json_bytes(streams), MAX_JSON_BYTES)
            updated["recording_parser_version"] = RECORDING_STREAM_VERSION
            activities[identifier] = updated
            changed = True
            result["repaired"] += 1
        except (OSError, ValueError, TypeError, KeyError, UnicodeError):
            result["errors"] += 1
    if changed:
        _assert_generation(data_dir, root, expected_identity)
        with _directory(root, "recordings") as directory:
            _assert_generation(data_dir, root, expected_identity)
            _write(directory, "activities.json", _json_bytes(activities), MAX_JSON_BYTES)
    return result
