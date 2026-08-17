"""A narrow boundary around the independently authenticated, official ride CLI.

The vendor owns OAuth and its credential store. This module never reads that store.
Release provenance: https://github.com/ridewithgps/ride-cli/releases/tag/v0.1.0
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import selectors
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPRedirectHandler, build_opener
import uuid

RIDE_VERSION = "0.1.0"
RIDE_API_ORIGIN = "https://ridewithgps.com"
CURRENT_USER_PATH = "/api/v1/users/current"
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_LOGIN_BYTES = 64 * 1024
API_TIMEOUT_SECONDS = 120
LOGIN_TIMEOUT_SECONDS = 300
INSTALL_TIMEOUT_SECONDS = 300


class RideCLIError(RuntimeError):
    """A safe, user-facing error without vendor output or credentials."""


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    size: int
    sha256: str


RELEASE_ASSETS = {
    ("Darwin", "arm64"): ReleaseAsset(
        "ride-darwin-arm64",
        61317008,
        "929d5474702c8de588b01b3bf022a881ca7045a5a4978f027b15421ac232fa9a",
    ),
    ("Darwin", "x86_64"): ReleaseAsset(
        "ride-darwin-x64",
        66327968,
        "4be839c4a6e1873871d6cacdb2f598ce23eff300f86b2c81ad8843b21c0f42e7",
    ),
    ("Linux", "x86_64"): ReleaseAsset(
        "ride-linux-x64",
        104284758,
        "ad7dc20b902f1dc60b49f6548296ba85d7642921bd4109a3c5424bc9f05c61b2",
    ),
}
_hash_cache: set[tuple] = set()
_hash_cache_lock = threading.Lock()


def _platform_asset() -> ReleaseAsset:
    machine = platform.machine().lower()
    machine = {"aarch64": "arm64", "amd64": "x86_64"}.get(machine, machine)
    asset = RELEASE_ASSETS.get((platform.system(), machine))
    if asset is None:
        raise RideCLIError("The pinned ride CLI is not available for this platform.")
    return asset


def _uid() -> int:
    if not hasattr(os, "getuid"):
        raise RideCLIError("The pinned ride CLI is not available for this platform.")
    return os.getuid()


def _trusted_parents(path: Path) -> None:
    for parent in (path, *path.parents):
        info = parent.lstat()
        sticky_root = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid not in {0, _uid()}
            or (info.st_mode & 0o022 and not sticky_root)
        ):
            raise RideCLIError("The ride CLI location is not safely owned.")


def _identity(info: os.stat_result) -> tuple:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def find_ride_cli(executable: Path | None = None) -> Path:
    """Find and verify the exact pinned vendor executable without running it."""
    try:
        asset = _platform_asset()
        found = str(executable) if executable is not None else shutil.which("ride")
        if not found:
            raise RideCLIError("Install the official ride CLI to connect Ride with GPS.")
        original = Path(os.path.abspath(found))
        if original.is_symlink():
            raise RideCLIError("The ride CLI executable must not be a symlink.")
        path = original.resolve(strict=True)
        _trusted_parents(path.parent)
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid not in {0, _uid()}
            or before.st_mode & 0o022
            or not before.st_mode & 0o100
            or before.st_nlink != 1
            or before.st_size != asset.size
        ):
            raise RideCLIError("The ride CLI executable is not safely installed.")
        key = (str(path), *_identity(before), asset.sha256)
        with _hash_cache_lock:
            cached = key in _hash_cache
        if not cached:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as handle:
                if _identity(os.fstat(handle.fileno())) != _identity(before):
                    raise RideCLIError("The ride CLI executable changed during validation.")
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
                if _identity(os.fstat(handle.fileno())) != _identity(before):
                    raise RideCLIError("The ride CLI executable changed during validation.")
            if digest != asset.sha256:
                raise RideCLIError("The ride CLI does not match the official pinned release.")
            with _hash_cache_lock:
                _hash_cache.add(key)
        if _identity(path.lstat()) != _identity(before):
            raise RideCLIError("The ride CLI executable changed during validation.")
        return path
    except RideCLIError:
        raise
    except (OSError, ValueError):
        raise RideCLIError("The official ride CLI could not be validated.") from None


def _private_config_dir(config_dir: Path | None) -> Path | None:
    if config_dir is None:
        return None
    try:
        original = Path(os.path.abspath(config_dir))
        if original.is_symlink():
            raise RideCLIError("The ride configuration directory must be private.")
        path = original.resolve(strict=True)
        info = path.lstat()
        _trusted_parents(path)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != _uid() or info.st_mode & 0o077:
            raise RideCLIError("The ride configuration directory must be private.")
        return path
    except RideCLIError:
        raise
    except (OSError, ValueError):
        raise RideCLIError("The ride configuration directory is unavailable.") from None


class _ReleaseRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlsplit(newurl)
        if (
            parsed.scheme != "https"
            or parsed.hostname
            not in {
                "github.com",
                "release-assets.githubusercontent.com",
                "objects.githubusercontent.com",
            }
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 443}
        ):
            raise RideCLIError("The official ride CLI download redirected unexpectedly.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download_asset(asset: ReleaseAsset):
    url = f"https://github.com/ridewithgps/ride-cli/releases/download/v{RIDE_VERSION}/{asset.name}"
    # The default opener retains normal system proxy and certificate verification.
    return build_opener(_ReleaseRedirects()).open(url, timeout=30)


def _make_install_parent(path: Path) -> Path:
    missing = []
    current = path
    while not current.exists():
        if current.is_symlink():
            raise RideCLIError("The ride install directory must not be a symlink.")
        missing.append(current)
        current = current.parent
    _trusted_parents(current.resolve(strict=True))
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
    resolved = path.resolve(strict=True)
    _trusted_parents(resolved)
    if resolved.stat().st_uid != _uid():
        raise RideCLIError("The ride install directory must be owned by you.")
    return resolved


def install_ride_cli(destination: Path, *, confirmed: bool = False) -> Path:
    """Download a checksum-pinned vendor binary only after explicit consent."""
    if confirmed is not True:
        raise RideCLIError("Confirm installation of the official ride CLI first.")
    temporary = None
    directory_fd = None
    try:
        asset = _platform_asset()
        destination = Path(os.path.abspath(destination))
        if destination.exists() or destination.is_symlink():
            return find_ride_cli(destination)
        parent = _make_install_parent(destination.parent)
        destination = parent / destination.name
        directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        temporary = f".ride-{uuid.uuid4().hex}.part"
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        digest = hashlib.sha256()
        size = 0
        deadline = time.monotonic() + INSTALL_TIMEOUT_SECONDS
        with os.fdopen(fd, "wb") as handle, _download_asset(asset) as response:
            while chunk := response.read(1024 * 1024):
                if time.monotonic() >= deadline:
                    raise RideCLIError("The ride CLI download timed out.")
                size += len(chunk)
                if size > asset.size:
                    raise RideCLIError("The ride CLI download exceeded its expected size.")
                digest.update(chunk)
                handle.write(chunk)
            if size != asset.size or digest.hexdigest() != asset.sha256:
                raise RideCLIError("The ride CLI download did not match the official release.")
            os.fchmod(handle.fileno(), 0o700)
            handle.flush()
            os.fsync(handle.fileno())
        # A hard link publishes atomically without overwriting a concurrent installation.
        os.link(
            temporary,
            destination.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        os.unlink(temporary, dir_fd=directory_fd)
        temporary = None
        os.fsync(directory_fd)
        return find_ride_cli(destination)
    except RideCLIError:
        raise
    except (OSError, ValueError):
        raise RideCLIError("The official ride CLI could not be installed.") from None
    finally:
        if directory_fd is not None:
            if temporary is not None:
                try:
                    os.unlink(temporary, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
            os.close(directory_fd)


_SAFE_ENV = {
    "HOME",
    "USER",
    "LOGNAME",
    "PATH",
    "LANG",
    "LC_ALL",
    "TZ",
    "TMPDIR",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
}


def _environment(config_dir: Path | None) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key in _SAFE_ENV}
    env["RIDE_API_URL"] = RIDE_API_ORIGIN
    if config_dir is not None:
        env["RIDE_CONFIG_DIR"] = str(_private_config_dir(config_dir))
    return env


def _authorization_url(value: str) -> str | None:
    """Accept only a vendor PKCE authorization URL, never a callback or token."""
    try:
        parsed = urlsplit(value.strip())
        if (
            parsed.scheme != "https"
            or parsed.netloc != "ridewithgps.com"
            or parsed.path != "/oauth/authorize"
            or parsed.fragment
        ):
            return None
        query = parse_qs(parsed.query, strict_parsing=True)
        if set(query) != {
            "response_type",
            "client_id",
            "redirect_uri",
            "code_challenge",
            "code_challenge_method",
            "state",
        }:
            return None
        if any(len(values) != 1 for values in query.values()):
            return None
        fields = {key: values[0] for key, values in query.items()}
        redirect = urlsplit(fields["redirect_uri"])
        if (
            fields["response_type"] != "code"
            or fields["client_id"] != "ride-cli"
            or fields["code_challenge_method"] != "S256"
            or not re.fullmatch(r"[A-Za-z0-9_-]{43}", fields["code_challenge"])
            or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", fields["state"])
            or redirect.scheme != "http"
            or redirect.hostname != "127.0.0.1"
            or redirect.username is not None
            or redirect.password is not None
            or redirect.port is None
            or not 1 <= redirect.port <= 65535
            or redirect.path != "/oauth/callback"
            or redirect.query
            or redirect.fragment
        ):
            return None
        return value.strip()
    except (ValueError, TypeError):
        return None


@contextmanager
def _login_lock(config_dir: Path | None) -> Iterator[None]:
    """Serialize a vendor profile without opening its configuration or token file."""
    import fcntl

    fd = None
    try:
        profile = config_dir or Path.home() / ".config" / "ride"
        key = hashlib.sha256(str(profile.resolve()).encode()).hexdigest()
        root = Path(tempfile.gettempdir()).resolve() / f"gradient-ascent-ride-login-{_uid()}"
        root.mkdir(mode=0o700, exist_ok=True)
        info = root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != _uid() or info.st_mode & 0o077:
            raise RideCLIError("The ride sign-in lock is not private.")
        fd = os.open(root / key, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != _uid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
        ):
            raise RideCLIError("The ride sign-in lock is not private.")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RideCLIError("Ride with GPS sign-in is already in progress.") from None
        yield
    except RideCLIError:
        raise
    except OSError:
        raise RideCLIError("The ride sign-in lock is unavailable.") from None
    finally:
        if fd is not None:
            os.close(fd)


def _run_bounded(
    args: list[str],
    env: dict[str, str],
    *,
    maximum: int,
    timeout: float,
    cancel: threading.Event | None = None,
    on_line: Callable[[bytes], None] | None = None,
) -> bytes:
    process = None
    output = bytearray()
    pending = bytearray()
    deadline = time.monotonic() + timeout
    try:
        if cancel is not None and cancel.is_set():
            raise RideCLIError("Ride with GPS sign-in was cancelled.")
        process = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        assert process.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                if cancel is not None and cancel.is_set():
                    raise RideCLIError("Ride with GPS sign-in was cancelled.")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RideCLIError("The ride CLI request timed out.")
                for key, _ in selector.select(min(remaining, 0.1)):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    if len(output) + len(chunk) > maximum:
                        raise RideCLIError("The ride CLI response exceeded its safe limit.")
                    output.extend(chunk)
                    if on_line is not None:
                        pending.extend(chunk)
                        while b"\n" in pending:
                            line, _, rest = pending.partition(b"\n")
                            pending = bytearray(rest)
                            on_line(bytes(line))
            if pending and on_line is not None:
                on_line(bytes(pending))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RideCLIError("The ride CLI request timed out.")
        if process.wait(timeout=remaining) != 0:
            raise RideCLIError("The ride CLI request failed. Check your connection and sign-in.")
        return bytes(output)
    except RideCLIError:
        raise
    except Exception:
        raise RideCLIError("The ride CLI request could not be completed.") from None
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait()
            if process.stdout is not None:
                process.stdout.close()


class RideCLI:
    def __init__(self, executable: Path | None = None, *, config_dir: Path | None = None):
        self.executable = find_ride_cli(executable)
        self.config_dir = _private_config_dir(config_dir)

    def get_json(self, path: str, params: Mapping[str, str | int] | None = None) -> dict:
        params = dict(params or {})
        query = []
        if path == "/api/v1/trips.json":
            if (
                set(params) != {"page", "page_size"}
                or type(params["page"]) is not int
                or type(params["page_size"]) is not int
                or not 1 <= params["page"] <= 100000
                or not 20 <= params["page_size"] <= 200
            ):
                raise RideCLIError("The ride trip-list request is outside the supported bounds.")
            query = [
                "--query",
                f"page={params['page']}",
                "--query",
                f"page_size={params['page_size']}",
            ]
        elif params or not (
            path == CURRENT_USER_PATH or re.fullmatch(r"/api/v1/trips/[1-9][0-9]{0,31}\.json", path)
        ):
            raise RideCLIError("That ride API request is not supported.")
        executable = find_ride_cli(self.executable)
        raw = _run_bounded(
            [str(executable), "api", "get", path, *query],
            _environment(self.config_dir),
            maximum=MAX_RESPONSE_BYTES,
            timeout=API_TIMEOUT_SECONDS,
        )
        try:
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError
            return result
        except (ValueError, UnicodeError):
            raise RideCLIError("The ride CLI returned an invalid response.") from None

    def login(
        self,
        on_authorization_url: Callable[[str], None],
        *,
        cancel: threading.Event | None = None,
        timeout_seconds: float = LOGIN_TIMEOUT_SECONDS,
        reauth: bool = False,
    ) -> None:
        if (
            type(timeout_seconds) not in {int, float}
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= LOGIN_TIMEOUT_SECONDS
        ):
            raise RideCLIError("The ride sign-in timeout is outside the supported bounds.")
        if type(reauth) is not bool:
            raise RideCLIError("The ride sign-in option is not supported.")
        executable = find_ride_cli(self.executable)
        delivered = False

        def on_line(line: bytes) -> None:
            nonlocal delivered
            if delivered:
                return
            value = _authorization_url(line.decode("utf-8", errors="replace"))
            if value is not None:
                on_authorization_url(value)
                delivered = True

        with (
            _login_lock(self.config_dir),
            tempfile.TemporaryDirectory(prefix="ride-browser-") as empty,
        ):
            # v0.1.0's Commander negated option is misread by login.ts. Its opener
            # resolves `open`/`xdg-open` through PATH, so this child-only empty PATH
            # makes --no-browser reliable without changing the vendor executable.
            os.chmod(empty, 0o700)
            env = _environment(self.config_dir)
            env["PATH"] = empty
            _run_bounded(
                [str(executable), "login", "--no-browser", *(["--reauth"] if reauth else [])],
                env,
                maximum=MAX_LOGIN_BYTES,
                timeout=timeout_seconds,
                cancel=cancel,
                on_line=on_line,
            )
