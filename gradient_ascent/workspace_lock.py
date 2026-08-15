from __future__ import annotations

import errno
import hashlib
import os
import secrets
import stat as stat_module
import tempfile
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from threading import RLock, local

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows uses msvcrt below.
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX uses fcntl above.
    msvcrt = None


_LOCAL_WORKSPACE_LOCK = RLock()
_WORKSPACE_LOCK_STATE = local()
_WORKSPACE_GENERATION_FILENAME = "workspace-generation"
_WORKSPACE_GENERATION_BYTES = 32


def cross_process_locking_available() -> bool:
    return fcntl is not None or msvcrt is not None


def _lock_directory() -> Path:
    temporary_root = Path(tempfile.gettempdir()).expanduser().resolve()
    if hasattr(os, "getuid"):
        user_key = f"uid-{os.getuid()}"
    else:  # pragma: no cover - exercised on Windows.
        home_key = str(Path.home().expanduser().resolve()).encode("utf-8")
        user_key = hashlib.sha256(home_key).hexdigest()[:16]
    namespace_directory = temporary_root / f".gradient-ascent-{user_key}"
    lock_directory = namespace_directory / "locks"
    current = temporary_root
    for part in lock_directory.relative_to(temporary_root).parts:
        current /= part
        if current.is_symlink():
            raise RuntimeError(f"Gradient Ascent lock directory cannot be a symlink: {current}")
    namespace_directory.mkdir(mode=0o700, exist_ok=True)
    lock_directory.mkdir(mode=0o700, exist_ok=True)
    for directory in (namespace_directory, lock_directory):
        if directory.is_symlink() or not directory.is_dir():
            raise RuntimeError(f"Invalid Gradient Ascent lock directory: {directory}")
        try:
            directory.chmod(0o700)
        except OSError as exc:
            if hasattr(os, "getuid"):
                raise RuntimeError(
                    f"Could not secure Gradient Ascent lock directory: {directory}"
                ) from exc
        directory_stat = directory.stat(follow_symlinks=False)
        if hasattr(os, "getuid") and (
            directory_stat.st_uid != os.getuid()
            or stat_module.S_IMODE(directory_stat.st_mode) & 0o077
        ):
            raise RuntimeError(
                f"Gradient Ascent lock directory is not private to this user: {directory}"
            )
    return lock_directory


def _workspace_path_and_key(data_dir: Path) -> tuple[Path, tuple[int, int, str]]:
    expanded = data_dir.expanduser()
    if expanded.exists():
        workspace = expanded.resolve()
    else:
        parent = expanded.parent.resolve()
        workspace = parent / expanded.name
    parent_stat = workspace.parent.stat()
    normalized_name = unicodedata.normalize("NFC", workspace.name.casefold())
    key = (parent_stat.st_dev, parent_stat.st_ino, normalized_name)
    return workspace, key


def _lock_path(workspace_key: tuple[int, int, str]) -> Path:
    identity = f"{workspace_key[0]}:{workspace_key[1]}:{workspace_key[2]}".encode("utf-8")
    return _lock_directory() / f"{hashlib.sha256(identity).hexdigest()}.lock"


def _acquire_file_lock(handle) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return
    if msvcrt is None:  # pragma: no cover - supported runtimes provide one backend.
        raise RuntimeError("This platform does not provide cross-process file locking.")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    while True:  # pragma: no cover - exercised on Windows.
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise
            time.sleep(0.05)


def _release_file_lock(handle) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    if msvcrt is not None:  # pragma: no cover - exercised on Windows.
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


@contextmanager
def _generation_cache_directory(workspace: Path, *, create: bool):
    cache_path = workspace / ".codex" / "cache"
    supports_directory_descriptors = (
        hasattr(os, "O_DIRECTORY") and os.open in os.supports_dir_fd
    )
    if not supports_directory_descriptors:  # pragma: no cover - Windows fallback.
        for directory in (workspace / ".codex", cache_path):
            if directory.is_symlink():
                raise OSError(f"Gradient Ascent generation directory cannot be a symlink: {directory}")
            if not directory.exists():
                if not create:
                    yield None, None
                    return
                directory.mkdir(mode=0o700)
            if not directory.is_dir():
                raise NotADirectoryError(f"Invalid Gradient Ascent generation directory: {directory}")
        yield cache_path, None
        return

    flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(workspace, flags)
    try:
        current_path = workspace
        for component in (".codex", "cache"):
            current_path /= component
            if current_path.is_symlink():
                raise OSError(
                    f"Gradient Ascent generation directory cannot be a symlink: {current_path}"
                )
            try:
                next_descriptor = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    yield None, None
                    return
                try:
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                next_descriptor = os.open(component, flags, dir_fd=descriptor)
            except OSError as exc:
                if current_path.is_symlink():
                    raise OSError(
                        "Gradient Ascent generation directory cannot be a symlink: "
                        f"{current_path}"
                    ) from exc
                raise
            os.close(descriptor)
            descriptor = next_descriptor
            directory_stat = os.fstat(descriptor)
            if hasattr(os, "getuid") and directory_stat.st_uid != os.getuid():
                raise PermissionError(
                    "Gradient Ascent generation directory is not owned by this user: "
                    f"{current_path}"
                )
        if create and hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o700)
        yield cache_path, descriptor
    finally:
        os.close(descriptor)


def _workspace_generation_marker(workspace: Path, *, create: bool = False) -> bytes | None:
    with _generation_cache_directory(workspace, create=create) as (cache_path, directory_descriptor):
        if cache_path is None:
            return None
        marker_path = cache_path / _WORKSPACE_GENERATION_FILENAME
        if marker_path.is_symlink():
            raise OSError(f"Gradient Ascent workspace generation marker cannot be a symlink: {marker_path}")
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        marker_name = (
            _WORKSPACE_GENERATION_FILENAME if directory_descriptor is not None else marker_path
        )
        descriptor_kwargs = (
            {"dir_fd": directory_descriptor} if directory_descriptor is not None else {}
        )
        try:
            descriptor = os.open(marker_name, flags, **descriptor_kwargs)
        except FileNotFoundError:
            if not create:
                return None
            temporary_name = f".{_WORKSPACE_GENERATION_FILENAME}.{secrets.token_hex(12)}.tmp"
            temporary_path = cache_path / temporary_name
            temporary_target = (
                temporary_name if directory_descriptor is not None else temporary_path
            )
            temporary_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                temporary_flags |= os.O_NOFOLLOW
            temporary_descriptor = os.open(
                temporary_target,
                temporary_flags,
                0o600,
                **descriptor_kwargs,
            )
            try:
                with os.fdopen(temporary_descriptor, "wb") as handle:
                    handle.write(secrets.token_hex(_WORKSPACE_GENERATION_BYTES).encode("ascii"))
                if directory_descriptor is None:  # pragma: no cover - Windows fallback.
                    os.replace(temporary_path, marker_path)
                else:
                    os.replace(
                        temporary_name,
                        _WORKSPACE_GENERATION_FILENAME,
                        src_dir_fd=directory_descriptor,
                        dst_dir_fd=directory_descriptor,
                    )
            finally:
                try:
                    os.unlink(temporary_target, **descriptor_kwargs)
                except FileNotFoundError:
                    pass
            descriptor = os.open(marker_name, flags, **descriptor_kwargs)
        except OSError as exc:
            if marker_path.is_symlink():
                raise OSError(
                    f"Gradient Ascent workspace generation marker cannot be a symlink: {marker_path}"
                ) from exc
            raise

        with os.fdopen(descriptor, "rb") as handle:
            marker_stat = os.fstat(handle.fileno())
            if not stat_module.S_ISREG(marker_stat.st_mode):
                raise OSError(f"Gradient Ascent workspace generation marker is not a regular file: {marker_path}")
            if hasattr(os, "getuid") and (
                marker_stat.st_uid != os.getuid()
                or stat_module.S_IMODE(marker_stat.st_mode) & 0o077
            ):
                raise PermissionError(
                    f"Gradient Ascent workspace generation marker is not private: {marker_path}"
                )
            marker = handle.read(_WORKSPACE_GENERATION_BYTES * 2 + 1)
        if len(marker) != _WORKSPACE_GENERATION_BYTES * 2 or any(
            character not in b"0123456789abcdef" for character in marker
        ):
            raise OSError(f"Gradient Ascent workspace generation marker is invalid: {marker_path}")
        return marker


def _workspace_identity(workspace: Path) -> tuple[int, int] | None:
    try:
        workspace_stat = workspace.stat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise OSError(f"Could not inspect Gradient Ascent workspace: {workspace}") from exc
    if not stat_module.S_ISDIR(workspace_stat.st_mode):
        raise NotADirectoryError(f"Gradient Ascent workspace is not a directory: {workspace}")
    generation_marker = _workspace_generation_marker(workspace)
    if generation_marker is None:
        return workspace_stat.st_dev, workspace_stat.st_ino
    identity = f"{workspace_stat.st_ino}:".encode("ascii") + generation_marker
    generation = int.from_bytes(hashlib.sha256(identity).digest()[:16], "big")
    return workspace_stat.st_dev, generation


def ensure_workspace_generation(data_dir: Path) -> None:
    workspace, _key = _workspace_path_and_key(data_dir)
    if _workspace_identity(workspace) is None:
        raise FileNotFoundError(f"Gradient Ascent workspace does not exist: {workspace}")
    if _workspace_generation_marker(workspace) is not None:
        return
    with workspace_lock(workspace):
        _workspace_generation_marker(workspace, create=True)


def workspace_identity(data_dir: Path) -> tuple[int, int]:
    workspace, _key = _workspace_path_and_key(data_dir)
    ensure_workspace_generation(workspace)
    identity = _workspace_identity(workspace)
    if identity is None:
        raise FileNotFoundError(f"Gradient Ascent workspace does not exist: {workspace}")
    return identity


@contextmanager
def workspace_lock(
    data_dir: Path,
    *,
    require_existing: bool = True,
    expected_identity: tuple[int, int] | None = None,
):
    """Serialize sanctioned workspace mutations and reject deleted/replaced roots."""

    workspace, workspace_key = _workspace_path_and_key(data_dir)
    initial_identity = _workspace_identity(workspace)
    if expected_identity is not None and initial_identity != expected_identity:
        raise RuntimeError("Gradient Ascent workspace generation changed; restart and retry.")
    lock_path = _lock_path(workspace_key)
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW

    with _LOCAL_WORKSPACE_LOCK:
        process_id = os.getpid()
        if getattr(_WORKSPACE_LOCK_STATE, "process_id", None) != process_id:
            _WORKSPACE_LOCK_STATE.process_id = process_id
            _WORKSPACE_LOCK_STATE.held = {}
        held = _WORKSPACE_LOCK_STATE.held
        if workspace_key in held:
            held[workspace_key] += 1
            try:
                yield
            finally:
                held[workspace_key] -= 1
            return

        descriptor = os.open(lock_path, flags, 0o600)
        with os.fdopen(descriptor, "a+b") as handle:
            if hasattr(os, "getuid"):
                try:
                    os.fchmod(handle.fileno(), 0o600)
                except OSError as exc:
                    raise RuntimeError(f"Could not secure Gradient Ascent lock: {lock_path}") from exc
            lock_stat = os.fstat(handle.fileno())
            if not stat_module.S_ISREG(lock_stat.st_mode):
                raise RuntimeError(f"Gradient Ascent lock is not a regular file: {lock_path}")
            if hasattr(os, "getuid") and (
                lock_stat.st_uid != os.getuid()
                or stat_module.S_IMODE(lock_stat.st_mode) & 0o077
            ):
                raise RuntimeError(f"Gradient Ascent lock is not private to this user: {lock_path}")
            _acquire_file_lock(handle)
            try:
                current_identity = _workspace_identity(workspace)
                if require_existing and current_identity is None:
                    raise FileNotFoundError(
                        f"Gradient Ascent workspace does not exist: {workspace}"
                    )
                if initial_identity is not None and current_identity != initial_identity:
                    raise RuntimeError(
                        "Gradient Ascent workspace was replaced while waiting for its lock; retry."
                    )
                if expected_identity is not None and current_identity != expected_identity:
                    raise RuntimeError(
                        "Gradient Ascent workspace generation changed; restart and retry."
                    )
                held[workspace_key] = 1
                try:
                    yield
                finally:
                    held.pop(workspace_key, None)
            finally:
                _release_file_lock(handle)
