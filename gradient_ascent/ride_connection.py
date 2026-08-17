"""Non-secret settings and explicit consent for the vendor-owned ``ride`` CLI."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from typing import Any

from .config import ensure_private_data_dir
from .ride_cli import (
    CURRENT_USER_PATH,
    RIDE_VERSION,
    RideCLI,
    RideCLIError,
    find_ride_cli,
    install_ride_cli,
)
from .ridewithgps import DEFAULT_SYNC_DAYS, MAX_SYNC_DAYS, sync_ridewithgps
from .storage import ensure_text_line
from .workspace_lock import workspace_identity, workspace_lock


SETTINGS_PATH = Path("connections") / "ridewithgps.json"
MAX_SETTINGS_BYTES = 64 * 1024
_SETTINGS_KEYS = frozenset(
    {
        "version",
        "enabled",
        "executable",
        "config_dir",
        "days",
        "connected_at",
        "last_checked_at",
        "last_sync_at",
        "last_sync",
        "last_error",
        "account_salt",
        "account_fingerprint",
    }
)
_COUNT_KEYS = frozenset(
    {
        "pages",
        "listed",
        "eligible",
        "imported",
        "updated",
        "existing",
        "skipped",
        "streams",
        "laps",
        "next_offset",
    }
)
_SYNC_KEYS = _COUNT_KEYS | {
    "provider",
    "mode",
    "complete",
    "next_page",
    "has_more",
}


class RideConnectionError(RuntimeError):
    """An actionable, provider-output-free error safe for a local dashboard."""


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _check_generation(data_dir: Path, identity: tuple[int, int]) -> None:
    # Reentrant acquisition still checks identity before yielding. This catches
    # manual filesystem replacement during a long vendor operation.
    with workspace_lock(data_dir, expected_identity=identity):
        pass


def _checked_get_json(
    client: RideCLI,
    data_dir: Path,
    identity: tuple[int, int],
    path: str,
    params: dict[str, int | str] | None = None,
) -> dict[str, Any]:
    _check_generation(data_dir, identity)
    try:
        return client.get_json(path) if params is None else client.get_json(path, params)
    finally:
        _check_generation(data_dir, identity)


def _save_for_generation(
    data_dir: Path, settings: dict[str, Any], identity: tuple[int, int]
) -> None:
    with workspace_lock(data_dir, expected_identity=identity):
        _save_settings(data_dir, settings)


def _days(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_SYNC_DAYS:
        raise RideConnectionError(
            f"Choose a Ride with GPS lookback between 1 and {MAX_SYNC_DAYS} days."
        )
    return value


def _account_fingerprint(payload: Any, salt: str) -> str:
    user = payload.get("user") if isinstance(payload, dict) else None
    if not isinstance(user, dict):
        raise RideConnectionError("Ride with GPS did not return a valid account check.")
    identifier = user.get("id")
    if (
        isinstance(identifier, bool)
        or not isinstance(identifier, (int, str))
        or not re.fullmatch(r"[1-9][0-9]{0,31}", str(identifier))
    ):
        raise RideConnectionError("Ride with GPS did not return a valid account check.")
    return hashlib.sha256(f"gradient-ascent:ridewithgps:{salt}:{identifier}".encode()).hexdigest()


def _bind_account(settings: dict[str, Any], payload: Any, *, allow_initial: bool) -> None:
    salt = settings.get("account_salt")
    if not salt:
        if not allow_initial:
            raise RideConnectionError(
                "Run Ride with GPS setup to confirm the account for this workspace."
            )
        salt = secrets.token_hex(16)
    fingerprint = _account_fingerprint(payload, salt)
    previous = settings.get("account_fingerprint")
    if previous and previous != fingerprint:
        raise RideConnectionError(
            "The ride CLI is signed into a different Ride with GPS account. Sign back into the original account or use a separate coaching workspace."
        )
    if not previous and not allow_initial:
        raise RideConnectionError(
            "Run Ride with GPS setup to confirm the account for this workspace."
        )
    settings["account_salt"] = salt
    settings["account_fingerprint"] = fingerprint


def _owned(metadata: os.stat_result, *, private: bool = False) -> None:
    mask = 0o077 if private else 0o022
    if (hasattr(os, "getuid") and metadata.st_uid != os.getuid()) or stat.S_IMODE(
        metadata.st_mode
    ) & mask:
        raise RideConnectionError("Ride with GPS settings must be private to the current user.")


@contextmanager
def _settings_directory(data_dir: Path, *, create: bool) -> Iterator[int | None]:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        if not create and not (data_dir / SETTINGS_PATH).exists():
            yield None
            return
        raise RideConnectionError("Ride with GPS CLI support requires macOS or Linux.")
    root = data_dir.expanduser()
    if ".." in root.parts or root.is_symlink():
        raise RideConnectionError("Ride with GPS settings require a real private workspace.")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        descriptor = os.open(root, flags)
    except FileNotFoundError:
        if create:
            raise RideConnectionError("Initialize the private coaching workspace first.") from None
        yield None
        return
    try:
        _owned(os.fstat(descriptor))
        try:
            child = os.open("connections", flags, dir_fd=descriptor)
        except FileNotFoundError:
            if not create:
                yield None
                return
            os.mkdir("connections", mode=0o700, dir_fd=descriptor)
            child = os.open("connections", flags, dir_fd=descriptor)
        os.close(descriptor)
        descriptor = child
        _owned(os.fstat(descriptor))
        if create:
            os.fchmod(descriptor, 0o700)
        yield descriptor
    finally:
        os.close(descriptor)


def _settings_file_stat(directory: int) -> os.stat_result | None:
    try:
        metadata = os.stat(SETTINGS_PATH.name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size > MAX_SETTINGS_BYTES
    ):
        raise RideConnectionError("Ride with GPS settings are not a safe regular file.")
    _owned(metadata, private=True)
    return metadata


def _sync_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _SYNC_KEYS:
        raise RideConnectionError("Ride with GPS returned an invalid sync summary.")
    if value.get("provider") != "ridewithgps" or value.get("mode") not in {"recent", "history"}:
        raise RideConnectionError("Ride with GPS returned an invalid sync summary.")
    for key in _COUNT_KEYS:
        number = value.get(key)
        if isinstance(number, bool) or not isinstance(number, int) or not 0 <= number <= 10_000_000:
            raise RideConnectionError("Ride with GPS returned an invalid sync summary.")
    page = value.get("next_page")
    if page is not None and (
        isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= 100_000
    ):
        raise RideConnectionError("Ride with GPS returned an invalid sync summary.")
    if type(value.get("complete")) is not bool or type(value.get("has_more")) is not bool:
        raise RideConnectionError("Ride with GPS returned an invalid sync summary.")
    return dict(value)


def _validate_settings(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - _SETTINGS_KEYS or value.get("version") != 1:
        raise RideConnectionError("Ride with GPS connection settings are invalid; run setup again.")
    if type(value.get("enabled", False)) is not bool:
        raise RideConnectionError("Ride with GPS connection settings are invalid; run setup again.")
    result = {
        **value,
        "enabled": value.get("enabled", False),
        "days": _days(value.get("days", DEFAULT_SYNC_DAYS)),
    }
    for key in ("executable", "config_dir"):
        if key not in result:
            continue
        text = result[key]
        if (
            not isinstance(text, str)
            or not text
            or len(text) > 4096
            or any(ord(char) < 32 for char in text)
            or not Path(text).is_absolute()
        ):
            raise RideConnectionError(
                "Ride with GPS connection settings are invalid; run setup again."
            )
    for key in ("connected_at", "last_checked_at", "last_sync_at"):
        if key not in result:
            continue
        text = result[key]
        if not isinstance(text, str) or len(text) > 64:
            raise RideConnectionError(
                "Ride with GPS connection settings are invalid; run setup again."
            )
        try:
            datetime.fromisoformat(text)
        except ValueError:
            raise RideConnectionError(
                "Ride with GPS connection settings are invalid; run setup again."
            ) from None
    if "last_sync" in result:
        result["last_sync"] = _sync_summary(result["last_sync"])
    for key, size in (("account_salt", 32), ("account_fingerprint", 64)):
        if key in result and (
            not isinstance(result[key], str)
            or not re.fullmatch(rf"[a-f0-9]{{{size}}}", result[key])
        ):
            raise RideConnectionError(
                "Ride with GPS connection settings are invalid; run setup again."
            )
    if result.get("last_error") not in {None, "check_failed", "sync_failed", "account_changed"}:
        raise RideConnectionError("Ride with GPS connection settings are invalid; run setup again.")
    return result


def load_ride_settings(data_dir: Path) -> dict[str, Any]:
    """Read only Gradient Ascent's own allowlisted, non-secret settings file."""
    default = {"version": 1, "enabled": False, "days": DEFAULT_SYNC_DAYS}
    with _settings_directory(data_dir, create=False) as directory:
        if directory is None:
            return default
        expected = _settings_file_stat(directory)
        if expected is None:
            return default
        descriptor = os.open(SETTINGS_PATH.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        try:
            actual = os.fstat(descriptor)
            if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
                raise RideConnectionError("Ride with GPS settings changed while opening.")
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = -1
                body = handle.read(MAX_SETTINGS_BYTES + 1)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    if len(body) > MAX_SETTINGS_BYTES:
        raise RideConnectionError("Ride with GPS connection settings are too large.")
    try:
        return _validate_settings(json.loads(body))
    except (UnicodeError, ValueError):
        raise RideConnectionError(
            "Ride with GPS connection settings are invalid; run setup again."
        ) from None


def _save_settings(data_dir: Path, settings: dict[str, Any]) -> None:
    _ensure_private_ignores(data_dir)
    body = (
        json.dumps(_validate_settings(settings), sort_keys=True, indent=2, allow_nan=False) + "\n"
    ).encode()
    if len(body) > MAX_SETTINGS_BYTES:
        raise RideConnectionError("Ride with GPS connection settings are too large.")
    with _settings_directory(data_dir, create=True) as directory:
        assert directory is not None
        _settings_file_stat(directory)
        temporary = f".ridewithgps.{secrets.token_hex(12)}.tmp"
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            _settings_file_stat(directory)
            os.replace(temporary, SETTINGS_PATH.name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass


def managed_ride_path(data_dir: Path) -> Path:
    return data_dir.expanduser().absolute() / ".runtime" / "tools" / "ride" / RIDE_VERSION / "ride"


def _ensure_private_ignores(data_dir: Path) -> None:
    for pattern in (".runtime/", "connections/ridewithgps.json", "integrations/"):
        ensure_text_line(data_dir / ".gitignore", pattern)


def _command(data_dir: Path, settings: dict[str, Any], explicit: Path | None = None) -> Path:
    if explicit is not None:
        return find_ride_cli(explicit.expanduser())
    if settings.get("executable"):
        return find_ride_cli(Path(settings["executable"]))
    managed = managed_ride_path(data_dir)
    if managed.exists():
        return find_ride_cli(managed)
    return find_ride_cli()


def _client(settings: dict[str, Any], executable: Path) -> RideCLI:
    config_dir = Path(settings["config_dir"]) if settings.get("config_dir") else None
    return RideCLI(executable, config_dir=config_dir)


def ride_status(data_dir: Path) -> dict[str, Any]:
    """Offline, path-free status. It never inspects the vendor's token store."""
    try:
        settings = load_ride_settings(data_dir)
    except (RideConnectionError, OSError):
        return {
            "installed": False,
            "enabled": False,
            "status": "needs_attention",
            "version": RIDE_VERSION,
            "days": DEFAULT_SYNC_DAYS,
            "last_sync_at": None,
            "last_sync": None,
            "issue": "Run Ride with GPS setup again.",
        }
    try:
        _command(data_dir, settings)
        installed = True
    except (RideCLIError, OSError, ValueError):
        installed = False
    enabled = bool(settings["enabled"])
    status = "connected" if enabled and installed else "disabled" if installed else "needs_setup"
    if enabled and (not installed or settings.get("last_error")):
        status = "needs_attention"
    return {
        "installed": installed,
        "enabled": enabled,
        "status": status,
        "version": RIDE_VERSION,
        "days": settings["days"],
        "last_checked_at": settings.get("last_checked_at"),
        "last_sync_at": settings.get("last_sync_at"),
        "last_sync": settings.get("last_sync"),
        "issue": "Check the Ride with GPS connection and try again."
        if settings.get("last_error")
        else None,
    }


def connect_ride(
    data_dir: Path,
    *,
    install: bool = False,
    executable: Path | None = None,
    config_dir: Path | None = None,
    days: int | None = None,
    force_login: bool = False,
    on_authorization_url: Callable[[str], None] | None = None,
    cancel: Event | None = None,
    expected_identity: tuple[int, int] | None = None,
) -> dict[str, Any]:
    """Enable sync only after an explicit vendor-owned authentication check."""
    data_dir = ensure_private_data_dir(data_dir, action="connect Ride with GPS")
    if type(install) is not bool or type(force_login) is not bool:
        raise RideConnectionError("Ride with GPS setup requires explicit consent.")
    identity = expected_identity if expected_identity is not None else workspace_identity(data_dir)
    with workspace_lock(data_dir, expected_identity=identity):
        settings = load_ride_settings(data_dir)
        _ensure_private_ignores(data_dir)
        if days is not None:
            settings["days"] = _days(days)
        if config_dir is not None:
            settings["config_dir"] = str(config_dir.expanduser().absolute())
        try:
            command = _command(data_dir, settings, executable)
        except RideCLIError:
            if not install:
                raise RideConnectionError(
                    "Install Ride with GPS's official ride CLI, or choose Install and connect."
                ) from None
            command = install_ride_cli(managed_ride_path(data_dir), confirmed=True)
        _check_generation(data_dir, identity)
        settings["executable"] = str(command)
        client = _client(settings, command)
        authenticated = False
        if not force_login:
            try:
                account = _checked_get_json(client, data_dir, identity, CURRENT_USER_PATH)
                authenticated = True
            except RideCLIError:
                pass
        if not authenticated:
            if on_authorization_url is None:
                raise RideConnectionError(
                    "Sign in with the official ride CLI, then run setup again."
                )

            def guarded_authorization_url(url: str) -> None:
                _check_generation(data_dir, identity)
                on_authorization_url(url)

            _check_generation(data_dir, identity)
            try:
                client.login(guarded_authorization_url, cancel=cancel, reauth=force_login)
            finally:
                _check_generation(data_dir, identity)
            account = _checked_get_json(client, data_dir, identity, CURRENT_USER_PATH)
        _bind_account(settings, account, allow_initial=True)
        if cancel is not None and cancel.is_set():
            raise RideConnectionError("Ride with GPS sign-in was cancelled.")
        settings.update(enabled=True, connected_at=_now(), last_checked_at=_now())
        settings.pop("last_error", None)
        _save_for_generation(data_dir, settings, identity)
        # No additional executable discovery is needed after the checked command.
        return {**ride_status(data_dir), "installed": True, "enabled": True, "status": "connected"}


def check_ride(
    data_dir: Path, *, expected_identity: tuple[int, int] | None = None
) -> dict[str, Any]:
    data_dir = ensure_private_data_dir(data_dir, action="check Ride with GPS")
    identity = expected_identity if expected_identity is not None else workspace_identity(data_dir)
    with workspace_lock(data_dir, expected_identity=identity):
        settings = load_ride_settings(data_dir)
        try:
            command = _command(data_dir, settings)
            account = _checked_get_json(
                _client(settings, command), data_dir, identity, CURRENT_USER_PATH
            )
            _bind_account(settings, account, allow_initial=False)
        except RideConnectionError:
            if settings.get("executable"):
                settings["last_error"] = "account_changed"
                _save_for_generation(data_dir, settings, identity)
            raise
        except (RideCLIError, OSError, ValueError):
            if settings.get("executable"):
                settings["last_error"] = "check_failed"
                _save_for_generation(data_dir, settings, identity)
            raise RideConnectionError(
                "Ride with GPS could not verify the session. Choose Reconnect or run ride setup."
            ) from None
        settings["last_checked_at"] = _now()
        settings.pop("last_error", None)
        if settings.get("executable"):
            _save_for_generation(data_dir, settings, identity)
        return {**ride_status(data_dir), "authenticated": True}


def disable_ride(
    data_dir: Path, *, expected_identity: tuple[int, int] | None = None
) -> dict[str, Any]:
    data_dir = ensure_private_data_dir(data_dir, action="disable Ride with GPS sync")
    identity = expected_identity if expected_identity is not None else workspace_identity(data_dir)
    with workspace_lock(data_dir, expected_identity=identity):
        settings = load_ride_settings(data_dir)
        settings["enabled"] = False
        settings.pop("last_error", None)
        _save_for_generation(data_dir, settings, identity)
    return ride_status(data_dir)


def sync_configured_ride(
    data_dir: Path,
    *,
    expected_identity: tuple[int, int] | None = None,
    days: int | None = None,
    full_history: bool = False,
    restart: bool = False,
) -> dict[str, Any]:
    """Fetch approved rides under the workspace lock; never rebuild here."""
    data_dir = ensure_private_data_dir(data_dir, action="sync Ride with GPS")
    identity = expected_identity if expected_identity is not None else workspace_identity(data_dir)
    with workspace_lock(data_dir, expected_identity=identity):
        settings = load_ride_settings(data_dir)
        if not settings["enabled"]:
            return {"provider": "ridewithgps", "status": "not_configured", "external_access": False}
        try:
            command = _command(data_dir, settings)
            client = _client(settings, command)
            _bind_account(
                settings,
                _checked_get_json(client, data_dir, identity, CURRENT_USER_PATH),
                allow_initial=False,
            )
        except RideConnectionError:
            settings["last_error"] = "account_changed"
            _save_for_generation(data_dir, settings, identity)
            raise
        except (RideCLIError, OSError, ValueError):
            settings["last_error"] = "check_failed"
            _save_for_generation(data_dir, settings, identity)
            raise RideConnectionError(
                "Ride with GPS could not verify the account. Check the connection and retry."
            ) from None
        try:
            result = _sync_summary(
                sync_ridewithgps(
                    data_dir,
                    lambda path, params: _checked_get_json(
                        client, data_dir, identity, path, params
                    ),
                    days=_days(days if days is not None else settings["days"]),
                    full_history=full_history,
                    restart=restart,
                )
            )
        except (RideCLIError, RideConnectionError, OSError, ValueError, RuntimeError):
            settings["last_error"] = "sync_failed"
            _save_for_generation(data_dir, settings, identity)
            raise RideConnectionError(
                "Ride with GPS sync failed. Check the connection and retry; previously imported rides are preserved."
            ) from None
        settings.update(last_sync_at=_now(), last_sync=result)
        settings.pop("last_error", None)
        _save_for_generation(data_dir, settings, identity)
        return {**result, "status": "synced", "external_access": True}
