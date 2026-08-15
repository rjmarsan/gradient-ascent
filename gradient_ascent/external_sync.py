from __future__ import annotations

import errno
import json
import math
import os
import re
import stat
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .storage import ensure_text_line
from .workspace_lock import workspace_lock


MANIFEST_VERSION = 1
MAX_MANIFEST_BYTES = 32 * 1024 * 1024

_PROVIDER_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_RECORD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_RESERVED_PROVIDERS = frozenset({"strava", "recording", "recordings", "apple_health", "garmin"})
_MANIFEST_FIELDS = frozenset({"version", "provider", "activities", "recovery", "synced_at"})
_PROVIDER_FIELDS = frozenset({"id", "label"})
_SOURCE_FIELDS = frozenset({"provider", "record_id", "confidence"})
_ACTIVITY_TEXT_FIELDS = frozenset(
    {"id", "provider_id", "name", "sport_type", "start_date", "start_date_local", "date"}
)
_ACTIVITY_NUMERIC_FIELDS = frozenset(
    {
        "moving_time_s",
        "elapsed_time_s",
        "distance_m",
        "elevation_gain_m",
        "average_heartrate",
        "max_heartrate",
        "average_watts",
        "weighted_average_watts",
        "kilojoules",
        "estimated_tss",
        "intensity_factor",
    }
)
_RECOVERY_NUMERIC_FIELDS = frozenset(
    {
        "resting_hr",
        "hrv_ms",
        "sleep_duration_s",
        "sleep_score",
        "readiness_score",
        "recovery_score",
        "stress_avg",
    }
)
_ACTIVITY_FIELDS = _ACTIVITY_TEXT_FIELDS | _ACTIVITY_NUMERIC_FIELDS | {"source"}
_RECOVERY_FIELDS = _RECOVERY_NUMERIC_FIELDS | {"id", "date", "source"}
_CREDENTIAL_FIELDS = frozenset(
    {
        "accesstoken",
        "apikey",
        "authorization",
        "bearer",
        "clientid",
        "clientsecret",
        "cookie",
        "cookies",
        "credential",
        "credentials",
        "mfa",
        "otp",
        "passwd",
        "password",
        "privatekey",
        "refreshtoken",
        "secret",
        "session",
        "sessionid",
        "sessiontoken",
        "token",
    }
)


def _reject_credentials(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = "".join(character for character in str(key).lower() if character.isalnum())
            if normalized in _CREDENTIAL_FIELDS:
                raise ValueError(f"Sync manifest contains a credential-like field: {key}")
            _reject_credentials(child)
    elif isinstance(value, list):
        for child in value:
            _reject_credentials(child)


def _reject_unknown_fields(value: dict[str, Any], allowed: frozenset[str], label: str) -> None:
    unexpected = sorted(set(value) - allowed)
    if unexpected:
        raise ValueError(f"Sync manifest {label} contains unknown fields: {', '.join(unexpected)}")


def _valid_date(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Sync manifest {label} must be an ISO calendar date.")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Sync manifest {label} must be an ISO calendar date.") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"Sync manifest {label} must be an ISO calendar date.")
    return value


def _valid_datetime(value: Any, label: str, *, require_timezone: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Sync manifest {label} must be an ISO timestamp.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Sync manifest {label} must be an ISO timestamp.") from exc
    if "T" not in value or (require_timezone and parsed.tzinfo is None):
        raise ValueError(f"Sync manifest {label} must be an ISO timestamp.")
    return value


def _valid_provider(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError("Sync manifest provider must be an object.")
    _reject_unknown_fields(value, _PROVIDER_FIELDS, "provider")
    provider_id = value.get("id")
    if not isinstance(provider_id, str) or _PROVIDER_ID.fullmatch(provider_id) is None:
        raise ValueError("Sync manifest provider id must be a safe lowercase identifier.")
    if provider_id in _RESERVED_PROVIDERS:
        raise ValueError("Sync manifest provider id is reserved for a built-in import source.")
    label = value.get("label")
    if not isinstance(label, str) or not label.strip() or len(label) > 120:
        raise ValueError("Sync manifest provider label must be a nonempty string.")
    if any(ord(character) < 32 for character in label):
        raise ValueError("Sync manifest provider label cannot contain control characters.")
    return {"id": provider_id, "label": label}


def _valid_source(value: Any, provider_id: str, record_id: str) -> dict[str, str]:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("Sync manifest record source must be an object.")
    _reject_unknown_fields(value, _SOURCE_FIELDS, "record source")
    if value.get("provider", provider_id) != provider_id:
        raise ValueError("Sync manifest record has conflicting provider provenance.")
    if value.get("record_id", record_id) != record_id:
        raise ValueError("Sync manifest record has conflicting record provenance.")
    confidence = value.get("confidence", "high")
    if confidence not in {"high", "medium", "low"}:
        raise ValueError("Sync manifest record source confidence is invalid.")
    return {"provider": provider_id, "record_id": record_id, "confidence": confidence}


def _valid_record(value: Any, provider_id: str, *, activity: bool) -> dict[str, Any]:
    label = "activity" if activity else "recovery"
    if not isinstance(value, dict):
        raise ValueError(f"Sync manifest {label} record must be an object.")
    _reject_unknown_fields(value, _ACTIVITY_FIELDS if activity else _RECOVERY_FIELDS, label)
    record_id = value.get("id")
    if not isinstance(record_id, str) or _RECORD_ID.fullmatch(record_id) is None:
        raise ValueError(f"Sync manifest {label} record id must be a safe provider-local string.")
    if ".." in record_id:
        raise ValueError(f"Sync manifest {label} record id cannot contain path traversal.")

    normalized = dict(value)
    normalized["source"] = _valid_source(value.get("source"), provider_id, record_id)
    if activity:
        provider_record_id = value.get("provider_id", record_id)
        if provider_record_id != record_id:
            raise ValueError("Sync manifest activity record has conflicting record provenance.")
        sport_type = value.get("sport_type")
        if not isinstance(sport_type, str) or not sport_type.strip():
            raise ValueError("Sync manifest activity sport_type must be a nonempty string.")
        if "name" in value and not isinstance(value["name"], str):
            raise ValueError("Sync manifest activity name must be a string.")
        if "start_date" not in value and "start_date_local" not in value:
            raise ValueError("Sync manifest activity requires a start timestamp.")
        for field in ("start_date", "start_date_local"):
            if field in value:
                _valid_datetime(value[field], f"activity {field}")
        if "date" in value:
            _valid_date(value["date"], "activity date")
        numeric_fields = _ACTIVITY_NUMERIC_FIELDS
    else:
        _valid_date(value.get("date"), "recovery date")
        numeric_fields = _RECOVERY_NUMERIC_FIELDS

    for field in numeric_fields & value.keys():
        number = value[field]
        if number is None:
            continue
        if isinstance(number, bool) or not isinstance(number, (int, float)):
            raise ValueError(f"Sync manifest {label} {field} must be a finite number.")
        if not math.isfinite(number) or number < 0:
            raise ValueError(f"Sync manifest {label} {field} must be a nonnegative finite number.")
    return normalized


def _validate_manifest(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Sync manifest must be a JSON object.")
    _reject_credentials(value)
    _reject_unknown_fields(value, _MANIFEST_FIELDS, "root")
    if type(value.get("version")) is not int or value.get("version") != MANIFEST_VERSION:
        raise ValueError(f"Sync manifest version must be {MANIFEST_VERSION}.")
    provider = _valid_provider(value.get("provider"))
    result: dict[str, Any] = {"version": MANIFEST_VERSION, "provider": provider}
    for key, activity in (("activities", True), ("recovery", False)):
        entries = value.get(key)
        if not isinstance(entries, list):
            raise ValueError(f"Sync manifest {key} must be an array.")
        records = [_valid_record(item, provider["id"], activity=activity) for item in entries]
        record_ids = [record["id"] for record in records]
        if len(record_ids) != len(set(record_ids)):
            raise ValueError(f"Sync manifest {key} contains duplicate record ids.")
        result[key] = records
    synced_at = value.get("synced_at")
    if synced_at is None:
        synced_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    result["synced_at"] = _valid_datetime(synced_at, "synced_at", require_timezone=True)
    return result


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Sync manifest contains a duplicate JSON field: {key}")
        result[key] = value
    return result


def _invalid_json_constant(value: str) -> None:
    raise ValueError(f"Sync manifest contains a non-finite JSON number: {value}")


def _read_manifest(path: Path, *, directory_fd: int | None = None) -> dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    target = path.name if directory_fd is not None else os.fspath(path)
    try:
        descriptor = os.open(target, flags, dir_fd=directory_fd)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.EMLINK}:
            raise ValueError(f"Sync manifest cannot be a symbolic link: {path}") from exc
        raise ValueError(f"Sync manifest must be an existing local regular file: {path}") from exc
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise ValueError(f"Sync manifest must be a local regular file: {path}")
        if file_stat.st_size > MAX_MANIFEST_BYTES:
            raise ValueError("Sync manifest exceeds the maximum supported file size.")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            contents = handle.read(MAX_MANIFEST_BYTES + 1)
        if len(contents) > MAX_MANIFEST_BYTES:
            raise ValueError("Sync manifest exceeds the maximum supported file size.")
        try:
            payload = json.loads(
                contents.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_invalid_json_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise ValueError("Sync manifest must contain valid UTF-8 JSON.") from exc
        return _validate_manifest(payload)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _directory_flags() -> int:
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise RuntimeError("This platform cannot safely open local sync directories.")
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _open_workspace(data_dir: Path) -> tuple[Path, int]:
    expanded = data_dir.expanduser()
    if ".." in expanded.parts:
        raise ValueError("Sync workspace path cannot contain path traversal.")
    workspace = Path(os.path.abspath(expanded))
    try:
        descriptor = os.open(workspace, _directory_flags())
    except OSError as exc:
        if workspace.is_symlink():
            raise ValueError(f"Sync workspace cannot be a symbolic link: {workspace}") from exc
        raise ValueError(f"Sync workspace must be an existing directory: {workspace}") from exc
    return workspace, descriptor


def _open_child_directory(
    parent_fd: int,
    name: str,
    display_path: Path,
    *,
    create: bool,
) -> int | None:
    try:
        return os.open(name, _directory_flags(), dir_fd=parent_fd)
    except FileNotFoundError:
        if not create:
            return None
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        return _open_child_directory(parent_fd, name, display_path, create=False)
    except OSError as exc:
        try:
            entry_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError:
            entry_stat = None
        if entry_stat is not None and stat.S_ISLNK(entry_stat.st_mode):
            raise ValueError(f"Sync output cannot contain a symbolic link: {display_path}") from exc
        raise ValueError(f"Sync output must be a directory: {display_path}") from exc


def _atomic_write_manifest(directory_fd: int, destination: Path, payload: dict[str, Any]) -> None:
    temporary_name = f".{destination.name}.{uuid.uuid4().hex}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary_name, flags, 0o600, dir_fd=directory_fd)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(payload, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            destination_stat = os.stat(destination.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            destination_stat = None
        if destination_stat is not None and stat.S_ISLNK(destination_stat.st_mode):
            raise ValueError(f"Sync output cannot replace a symbolic link: {destination}")
        if destination_stat is not None and not stat.S_ISREG(destination_stat.st_mode):
            raise ValueError(f"Sync output must be a regular file: {destination}")
        os.replace(
            temporary_name,
            destination.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def import_sync_manifest(data_dir: Path, manifest_path: Path) -> dict[str, Any]:
    """Import a validated local provider snapshot without handling provider credentials."""

    _directory_flags()
    manifest = _read_manifest(manifest_path.expanduser())
    with workspace_lock(data_dir):
        provider = manifest["provider"]
        workspace, workspace_fd = _open_workspace(data_dir)
        integrations_fd: int | None = None
        provider_fd: int | None = None
        try:
            integrations_path = workspace / "integrations"
            integrations_fd = _open_child_directory(
                workspace_fd, "integrations", integrations_path, create=True
            )
            if integrations_fd is None:
                raise RuntimeError("Could not create the local sync integrations directory.")
            provider_path = integrations_path / provider["id"]
            provider_fd = _open_child_directory(
                integrations_fd, provider["id"], provider_path, create=True
            )
            if provider_fd is None:
                raise RuntimeError("Could not create the local sync provider directory.")
            ensure_text_line(workspace / ".gitignore", "integrations/")
            destination = provider_path / "manifest.json"
            _atomic_write_manifest(provider_fd, destination, manifest)
            return {
                "provider": provider["id"],
                "label": provider["label"],
                "activities": len(manifest["activities"]),
                "recovery": len(manifest["recovery"]),
                "synced_at": manifest["synced_at"],
                "path": str(destination),
            }
        finally:
            if provider_fd is not None:
                os.close(provider_fd)
            if integrations_fd is not None:
                os.close(integrations_fd)
            os.close(workspace_fd)


def load_external_sync_manifests(data_dir: Path) -> list[dict[str, Any]]:
    """Read validated, provider-scoped local snapshots, refusing tampered provenance."""

    try:
        _directory_flags()
    except RuntimeError:
        integrations_path = data_dir.expanduser() / "integrations"
        if integrations_path.is_symlink():
            raise ValueError(
                f"Sync output cannot contain a symbolic link: {integrations_path}"
            ) from None
        if not integrations_path.exists():
            return []
        if integrations_path.is_dir() and not any(integrations_path.iterdir()):
            return []
        raise

    workspace, workspace_fd = _open_workspace(data_dir)
    integrations_fd: int | None = None
    try:
        integrations_path = workspace / "integrations"
        integrations_fd = _open_child_directory(
            workspace_fd, "integrations", integrations_path, create=False
        )
        if integrations_fd is None:
            return []
        manifests: list[dict[str, Any]] = []
        for name in sorted(os.listdir(integrations_fd)):
            provider_path = integrations_path / name
            entry_stat = os.stat(name, dir_fd=integrations_fd, follow_symlinks=False)
            if stat.S_ISLNK(entry_stat.st_mode):
                raise ValueError(f"Sync output cannot contain a symbolic link: {provider_path}")
            if not stat.S_ISDIR(entry_stat.st_mode):
                continue
            if _PROVIDER_ID.fullmatch(name) is None:
                raise ValueError(f"Sync integration has an invalid provider directory: {name}")
            provider_fd = _open_child_directory(integrations_fd, name, provider_path, create=False)
            if provider_fd is None:
                raise ValueError(f"Sync provider directory disappeared: {provider_path}")
            try:
                if "manifest.json" not in os.listdir(provider_fd):
                    continue
                manifest = _read_manifest(provider_path / "manifest.json", directory_fd=provider_fd)
                if manifest["provider"]["id"] != name:
                    raise ValueError("Sync manifest provider does not match its private directory.")
                manifests.append(manifest)
            finally:
                os.close(provider_fd)
        return manifests
    finally:
        if integrations_fd is not None:
            os.close(integrations_fd)
        os.close(workspace_fd)
