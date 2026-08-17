from __future__ import annotations

import errno
import gzip
import hashlib
import json
from collections.abc import MutableMapping
import ipaddress
import os
import re
import secrets
import subprocess
import sys
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, Thread
from typing import Any
from urllib.parse import unquote, urlparse
from zipfile import BadZipFile

from .canonical import build_canonical_files
from .connections import (
    check_provider,
    connections_payload,
    provider_keys,
    provider_summary,
    update_provider,
)
from .insights import build_insights
from .recordings import import_activity_recording
from .storage import read_json, write_json
from .strava import import_strava_export
from .training_center import build_training_center
from .workspace_lock import workspace_identity, workspace_lock


DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CONNECTION_PROVIDER_RE = re.compile(r"^/api/connections/([a-z_]+)/?$")
CONNECTION_TEST_RE = re.compile(r"^/api/connections/([a-z_]+)/test/?$")
STRAVA_ARCHIVE_UPLOAD_PATH = "/api/connections/strava/archive"
ACTIVITY_RECORDING_UPLOAD_PATH = "/api/activity-recordings"
RIDE_SETUP_PATH = "/api/connections/ridewithgps/setup"
PLAN_EXPORT_PATH = "/api/plan/export"
MAX_BODY_BYTES = 1_000_000
MAX_STRAVA_ARCHIVE_BYTES = 20 * 1024 * 1024 * 1024
MAX_ACTIVITY_RECORDING_BYTES = 512 * 1024 * 1024
SYNC_LOG_LIMIT = 400
KEEP_LOOPBACK_PROXY_ENV = "COACH_DASHBOARD_KEEP_PROXY"
PROXY_ENV_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
TRUE_VALUES = {"1", "true", "yes", "on"}
WRITE_TOKEN_HEADER = "x-coach-write-token"
UPLOAD_NAME_HEADER = "x-coach-upload-name"


def _truthy_env(name: str) -> bool:
    return str(os.getenv(name, "")).strip().lower() in TRUE_VALUES


def _host_without_port(host: str) -> str:
    normalized = str(host or "").strip().lower()
    if normalized.startswith("["):
        match = re.fullmatch(r"\[([^\]]+)\](?::([0-9]+))?", normalized)
        if not match:
            return ""
        if match.group(2) and int(match.group(2)) > 65535:
            return ""
        return match.group(1)
    if normalized.count(":") == 1:
        hostname, port = normalized.rsplit(":", 1)
        if not port.isdigit() or int(port) > 65535:
            return ""
        return hostname
    return normalized


def _is_loopback_host(host: str) -> bool:
    normalized = _host_without_port(host)
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _workspace_instance_id(data_dir: Path) -> str:
    resolved = str(data_dir.expanduser().resolve())
    generation = workspace_identity(data_dir)
    identity = f"{resolved}\0{generation[0]}:{generation[1]}".encode("utf-8")
    return hashlib.sha256(identity).hexdigest()[:16]


def _origin_matches_host(value: str, host: str) -> bool:
    parsed = urlparse(value)
    if not parsed.netloc:
        return False
    return parsed.netloc.lower() == str(host or "").strip().lower()


def _remove_transient_loopback_proxies(env: MutableMapping[str, str]) -> bool:
    if str(env.get(KEEP_LOOPBACK_PROXY_ENV, "")).strip().lower() in TRUE_VALUES:
        return False
    if not any(
        _is_loopback_host(urlparse(str(env.get(name, ""))).hostname or "")
        for name in PROXY_ENV_NAMES
        if env.get(name)
    ):
        return False
    for name in PROXY_ENV_NAMES:
        env.pop(name, None)
    return True


def _notes_payload(notes_path: Path) -> dict[str, Any]:
    payload = read_json(notes_path, default={"version": 1, "notes": {}})
    if not isinstance(payload, dict):
        return {"version": 1, "notes": {}}
    notes = payload.get("notes")
    return {
        "version": 1,
        "notes": notes if isinstance(notes, dict) else {},
    }


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def _timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _strava_archive_upload_name(value: str) -> str:
    original_name = Path(unquote(str(value or "").replace("\\", "/"))).name
    suffix = Path(original_name).suffix.lower()
    if suffix not in {".zip", ".csv"}:
        raise ValueError("Expected a downloaded Strava archive ZIP or activities.csv file.")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(original_name).stem).strip(".-_")
    return f"{stem or 'strava-export'}{suffix}"


def _activity_recording_upload_name(value: str) -> str:
    original_name = Path(unquote(str(value or "").replace("\\", "/"))).name
    suffix = Path(original_name).suffix.lower()
    if suffix not in {".fit", ".tcx", ".gpx"}:
        raise ValueError("Expected a FIT, TCX, or GPX activity recording.")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(original_name).stem).strip(".-_")
    return f"{stem or 'activity-recording'}{suffix}"


def _ride_is_configured(data_dir: Path) -> bool:
    from .ride_connection import load_ride_settings

    return bool(load_ride_settings(data_dir)["enabled"])


def _workspace_refresh_steps(
    data_dir: Path,
    expected_identity: tuple[int, int] | None = None,
    *,
    configured: bool = False,
    history: bool = False,
    restart_history: bool = False,
) -> list[tuple[str, list[str], bool]]:
    module = "gradient_ascent.configured_refresh" if configured else "gradient_ascent.refresh"
    command = [sys.executable, "-m", module, "--data-dir", str(data_dir)]
    if history:
        command.append("--ride-history")
    if restart_history:
        command.append("--restart-history")
    if expected_identity is not None:
        command.extend(
            [
                "--expected-workspace-device",
                str(expected_identity[0]),
                "--expected-workspace-inode",
                str(expected_identity[1]),
            ]
        )
    name = "Ride with GPS history + workspace rebuild" if history else "Ride with GPS + workspace rebuild" if configured else "Workspace rebuild"
    return [(name, command, False)]


def _sync_steps(
    data_dir: Path,
    expected_identity: tuple[int, int] | None = None,
) -> list[tuple[str, list[str], bool]]:
    return _workspace_refresh_steps(data_dir, expected_identity, configured=_ride_is_configured(data_dir))


def _visible_sync_command(command: list[str], data_dir: Path) -> list[str]:
    if not command:
        return []
    visible = [Path(command[0]).name]
    for argument in command[1:]:
        if argument == str(data_dir):
            visible.append("<workspace>")
        elif Path(argument).is_absolute():
            visible.append("<local-path>")
        else:
            visible.append(argument)
    return visible


def _visible_sync_output(command: list[str], returncode: int, output: str) -> tuple[int, str]:
    if command[1:3] == ["-m", "gradient_ascent.configured_refresh"]:
        if returncode == 0:
            from .configured_refresh import validate_aggregate_refresh_result

            try:
                value = validate_aggregate_refresh_result(json.loads(output))
                return 0, json.dumps(value, sort_keys=True, separators=(",", ":"))
            except (RuntimeError, TypeError, ValueError):
                pass
        return returncode or 1, "Ride with GPS refresh failed. Check Connections and retry."
    if command[1:3] == ["-m", "gradient_ascent.refresh"]:
        return returncode, "Workspace refresh complete" if returncode == 0 else "Local workspace rebuild failed."
    # Separately installed companion launchers own their aggregate-only output.
    return returncode, output


def make_training_center_handler(data_dir: Path) -> type[SimpleHTTPRequestHandler]:
    from .ride_setup import RideSetupJobs

    derived_dir = (data_dir / "derived").resolve()
    notes_path = (data_dir / "plan" / "daily_notes.json").resolve()
    write_token = secrets.token_urlsafe(32)
    workspace_id = _workspace_instance_id(data_dir)
    workspace_generation = workspace_identity(data_dir)
    ride_setup_jobs = RideSetupJobs(data_dir, workspace_generation)
    lock = Lock()
    sync_lock = Lock()
    static_cache_lock = Lock()
    compressed_static_cache: dict[Path, tuple[int, int, bytes]] = {}
    sync_state: dict[str, Any] = {
        "status": "idle",
        "running": False,
        "ok": None,
        "message": "Sync has not run from this page yet.",
        "started_at": None,
        "completed_at": None,
        "steps": [],
        "log_tail": [],
    }
    def _sync_snapshot() -> dict[str, Any]:
        with sync_lock:
            return json.loads(json.dumps(sync_state))

    def _write_payload(payload: dict[str, Any]) -> dict[str, Any]:
        return {**payload, "write_token": write_token}

    def _update_sync_state(**updates: Any) -> None:
        with sync_lock:
            sync_state.update(updates)

    def _append_sync_log(message: str) -> None:
        lines = [line for line in str(message or "").splitlines() if line.strip()]
        if not lines:
            return
        with sync_lock:
            log_tail = sync_state.setdefault("log_tail", [])
            log_tail.extend(lines)
            del log_tail[:-SYNC_LOG_LIMIT]

    def _sync_env() -> dict[str, str]:
        env = os.environ.copy()
        env["COACH_DATA_DIR"] = str(data_dir)
        env["COACH_WORKSPACE_DIR"] = str(data_dir)
        env["PYTHON_BIN"] = sys.executable
        env.setdefault("PYTHONUNBUFFERED", "1")
        return env

    def _run_sync_step(name: str, command: list[str], *, continue_on_error: bool = False) -> dict[str, Any]:
        visible_command = _visible_sync_command(command, data_dir)
        _append_sync_log(f"[{_timestamp()}] {name}: {' '.join(visible_command)}")
        result = subprocess.run(
            command,
            cwd=data_dir,
            env=_sync_env(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        returncode, output = _visible_sync_output(command, result.returncode, result.stdout or "")
        _append_sync_log(output)
        step = {
            "name": name,
            "command": visible_command,
            "returncode": returncode,
            "ok": returncode == 0,
            "continued": bool(continue_on_error and returncode != 0),
            "completed_at": _timestamp(),
            "output": output,
        }
        with sync_lock:
            sync_state.setdefault("steps", []).append(step)
        return step

    def _run_sync_job(*, ride_history: bool = False, restart_history: bool = False, local_only: bool = False) -> None:
        _update_sync_state(
            status="running",
            running=True,
            ok=None,
            message="Refreshing enabled sources and rebuilding the Training Center.",
            started_at=_timestamp(),
            completed_at=None,
            steps=[],
            log_tail=[],
        )
        failure: str | None = None
        try:
            if ride_history:
                steps = _workspace_refresh_steps(data_dir, workspace_generation, configured=True, history=True, restart_history=restart_history)
            elif local_only:
                steps = _workspace_refresh_steps(data_dir, workspace_generation)
            else:
                steps = _sync_steps(data_dir, workspace_generation)
            for name, command, continue_on_error in steps:
                step = _run_sync_step(name, command, continue_on_error=continue_on_error)
                if step["ok"]:
                    continue
                failure = f"{name} failed with exit code {step['returncode']}."
                break
        except Exception as exc:  # pragma: no cover - defensive server guard
            failure = f"Sync worker failed ({type(exc).__name__}). Check Connections and retry."
            _append_sync_log(f"[{_timestamp()}] ERROR: {failure}")

        if failure:
            _update_sync_state(
                status="failed",
                running=False,
                ok=False,
                message=failure,
                completed_at=_timestamp(),
            )
        else:
            _update_sync_state(
                status="completed",
                running=False,
                ok=True,
                message="Refresh complete; Training Center rebuilt.",
                completed_at=_timestamp(),
            )

    class TrainingCenterHandler(SimpleHTTPRequestHandler):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=str(derived_dir), **kwargs)

        def log_message(self, format: str, *args: Any) -> None:
            return

        def parse_request(self) -> bool:
            if not super().parse_request():
                return False
            host = self.headers.get("Host")
            if not host or not _is_loopback_host(host):
                self.send_error(HTTPStatus.FORBIDDEN, "Loopback Host header required")
                return False
            return True

        def end_headers(self) -> None:
            # The training center payload is rebuilt in-place; disable browser caching
            # so reloads pick up the latest HTML and JS immediately after sync.
            self.send_header("cache-control", "no-store, max-age=0")
            self.send_header("pragma", "no-cache")
            self.send_header("expires", "0")
            self.send_header("cross-origin-resource-policy", "same-origin")
            self.send_header("x-content-type-options", "nosniff")
            super().end_headers()

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/api/health":
                self._send_json(
                    {
                        "ok": True,
                        "service": "gradient-ascent-training-center",
                        "workspace_id": workspace_id,
                    }
                )
                return
            if parsed.path == "/api/daily-notes":
                with lock:
                    payload = _notes_payload(notes_path)
                self._send_json(
                    _write_payload(
                        {
                            **payload,
                            "writable": True,
                            "path": _display_path(notes_path),
                        }
                    )
                )
                return
            if parsed.path == "/api/sync":
                self._send_json(_write_payload(_sync_snapshot()))
                return
            if parsed.path == "/api/connections":
                self._send_json(_write_payload(connections_payload(data_dir)))
                return
            if parsed.path == RIDE_SETUP_PATH:
                write_error = self._write_error()
                if write_error:
                    self._send_json({"error": write_error}, status=HTTPStatus.FORBIDDEN)
                    return
                self._send_json(_write_payload(ride_setup_jobs.snapshot()))
                return
            static_path = "/training_center.html" if parsed.path == "/" else parsed.path
            if self._send_compressed_static(static_path):
                return
            if static_path != parsed.path:
                self.path = static_path
            super().do_GET()

        def _send_compressed_static(self, request_path: str) -> bool:
            accepted = self.headers.get("accept-encoding", "").lower()
            if "gzip" not in accepted:
                return False
            relative_path = Path(unquote(request_path).lstrip("/"))
            target_path = (derived_dir / relative_path).resolve()
            try:
                target_path.relative_to(derived_dir)
            except ValueError:
                return False
            if target_path.suffix.lower() not in {".html", ".js", ".css", ".svg"}:
                return False
            try:
                stat = target_path.stat()
            except OSError:
                return False
            if not target_path.is_file() or stat.st_size < 1024:
                return False

            cache_key = (stat.st_mtime_ns, stat.st_size)
            with static_cache_lock:
                cached = compressed_static_cache.get(target_path)
            if cached and cached[:2] == cache_key:
                body = cached[2]
            else:
                try:
                    body = gzip.compress(target_path.read_bytes(), compresslevel=6)
                except OSError:
                    return False
                with static_cache_lock:
                    compressed_static_cache[target_path] = (*cache_key, body)

            self.send_response(HTTPStatus.OK)
            self.send_header("content-type", self.guess_type(str(target_path)))
            self.send_header("content-encoding", "gzip")
            self.send_header("vary", "Accept-Encoding")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return True

        def do_POST(self) -> None:
            write_error = self._write_error()
            if write_error:
                self._send_json({"error": write_error}, status=HTTPStatus.FORBIDDEN)
                return
            parsed = urlparse(self.path)
            if parsed.path == PLAN_EXPORT_PATH:
                from .plan_export import build_plan_export

                try:
                    body = self._read_json_body()
                    if set(body) - {"format", "start", "end", "workout_id"}:
                        raise ValueError("Only the export format, date range, and workout ID are accepted.")
                    if any(not isinstance(value, str) for value in body.values()):
                        raise ValueError("Plan export options must be text values.")
                    artifact = build_plan_export(
                        data_dir, expected_identity=workspace_generation, **body
                    )
                except ValueError as exc:
                    self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                    return
                except (OSError, RuntimeError):
                    self._send_json({"error": "The plan could not be exported; check the workspace and retry."}, status=HTTPStatus.CONFLICT)
                    return
                self.send_response(HTTPStatus.OK)
                self.send_header("content-type", artifact.content_type)
                self.send_header("content-disposition", f'attachment; filename="{artifact.filename}"')
                self.send_header("content-length", str(len(artifact.body)))
                self.send_header("x-gradient-ascent-plan-entries", str(artifact.summary["entries"]))
                self.send_header("x-gradient-ascent-fit-files", str(artifact.summary["fit_files"]))
                self.end_headers()
                self.wfile.write(artifact.body)
                return
            if parsed.path == RIDE_SETUP_PATH:
                from .ride_connection import RideConnectionError

                try:
                    body = self._read_json_body()
                    if set(body) - {"action", "install", "reauth"}:
                        raise ValueError("Only the setup action and consent flags are accepted.")
                    action = body.get("action")
                    if action == "cancel":
                        if set(body) != {"action"}:
                            raise ValueError("Cancel does not accept setup options.")
                        payload = ride_setup_jobs.cancel()
                    else:
                        payload = ride_setup_jobs.start(action, install=body.get("install", False), reauth=body.get("reauth", False))
                except (RideConnectionError, ValueError) as exc:
                    self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                    return
                self._send_json(_write_payload(payload), status=HTTPStatus.ACCEPTED)
                return
            if parsed.path == STRAVA_ARCHIVE_UPLOAD_PATH:
                try:
                    payload = self._import_strava_archive_upload()
                except (BadZipFile, OSError, ValueError) as exc:
                    self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                    return
                except Exception as exc:  # pragma: no cover - defensive server guard
                    self._send_json({"error": f"Strava archive import failed: {exc}"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
                    return
                self._send_json(_write_payload(payload))
                return
            if parsed.path == ACTIVITY_RECORDING_UPLOAD_PATH:
                try:
                    payload = self._import_activity_recording_upload()
                except (OSError, ValueError) as exc:
                    self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                    return
                except Exception as exc:  # pragma: no cover - defensive server guard
                    self._send_json(
                        {"error": f"Activity recording import failed: {exc}"},
                        status=HTTPStatus.INTERNAL_SERVER_ERROR,
                    )
                    return
                self._send_json(_write_payload(payload))
                return
            if parsed.path == "/api/sync":
                try:
                    body = self._read_json_body()
                    if set(body) - {"ride_history", "restart_history", "local_only"} or any(type(value) is not bool for value in body.values()):
                        raise ValueError("Unknown refresh option.")
                    if body.get("restart_history") and not body.get("ride_history"):
                        raise ValueError("Restart requires a history import.")
                    if body.get("ride_history") and (body.get("local_only") or not _ride_is_configured(data_dir)):
                        raise ValueError("Connect Ride with GPS before importing older rides.")
                except (OSError, RuntimeError, ValueError):
                    self._send_json({"error": "Choose a valid refresh option and check Connections."}, status=HTTPStatus.BAD_REQUEST)
                    return
                should_start = False
                with sync_lock:
                    if not sync_state.get("running"):
                        sync_state.update(
                            status="running",
                            running=True,
                            ok=None,
                            message="Starting local workspace refresh...",
                            started_at=_timestamp(),
                            completed_at=None,
                            steps=[],
                            log_tail=[],
                        )
                        should_start = True
                if should_start:
                    Thread(
                        target=_run_sync_job,
                        kwargs=body,
                        name="training-center-sync",
                        daemon=True,
                    ).start()
                self._send_json(_write_payload(_sync_snapshot()), status=HTTPStatus.ACCEPTED)
                return

            test_match = CONNECTION_TEST_RE.fullmatch(parsed.path)
            if test_match:
                provider = test_match.group(1)
                if provider not in provider_keys():
                    self.send_error(HTTPStatus.NOT_FOUND, "Unknown provider")
                    return
                if provider == "ridewithgps":
                    self._send_json(_write_payload(ride_setup_jobs.start("check")), status=HTTPStatus.ACCEPTED)
                    return
                self._send_json(_write_payload(check_provider(data_dir, provider)))
                return

            if parsed.path != "/api/sync":
                self.send_error(HTTPStatus.NOT_FOUND, "Unknown API route")
                return

        def do_PUT(self) -> None:
            write_error = self._write_error()
            if write_error:
                self._send_json({"error": write_error}, status=HTTPStatus.FORBIDDEN)
                return
            parsed = urlparse(self.path)
            prefix = "/api/daily-notes/"
            connection_match = CONNECTION_PROVIDER_RE.fullmatch(parsed.path)
            if connection_match:
                provider = connection_match.group(1)
                if provider not in provider_keys():
                    self.send_error(HTTPStatus.NOT_FOUND, "Unknown provider")
                    return
                try:
                    body = self._read_json_body()
                except ValueError as exc:
                    self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                    return
                fields = body.get("fields")
                if fields is None:
                    fields = {}
                if not isinstance(fields, dict):
                    self._send_json({"error": "fields must be an object."}, status=HTTPStatus.BAD_REQUEST)
                    return
                try:
                    with lock, workspace_lock(
                        data_dir,
                        expected_identity=workspace_generation,
                    ):
                        summary = update_provider(data_dir, provider, fields=fields)
                except (OSError, RuntimeError, ValueError) as exc:
                    self._send_json(
                        {"error": str(exc)},
                        status=HTTPStatus.CONFLICT,
                    )
                    return
                self._send_json({"ok": True, "provider": summary})
                return
            if not parsed.path.startswith(prefix):
                self.send_error(HTTPStatus.NOT_FOUND, "Unknown API route")
                return
            day = unquote(parsed.path[len(prefix) :])
            if not DATE_RE.fullmatch(day):
                self._send_json({"error": "Expected YYYY-MM-DD date."}, status=HTTPStatus.BAD_REQUEST)
                return

            try:
                body = self._read_json_body()
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return

            note = str(body.get("note") or "")
            source = str(body.get("source") or "training_center")
            updated_at = str(body.get("updated_at") or datetime.now(timezone.utc).replace(microsecond=0).isoformat())
            preserve_empty = bool(body.get("preserve_empty"))
            try:
                with lock, workspace_lock(
                    data_dir,
                    expected_identity=workspace_generation,
                ):
                    payload = _notes_payload(notes_path)
                    notes = payload["notes"]
                    if note.strip() or preserve_empty:
                        notes[day] = {
                            "date": day,
                            "note": note,
                            "updated_at": updated_at,
                            "source": source,
                        }
                    else:
                        notes.pop(day, None)
                    write_json(notes_path, {"version": 1, "notes": notes})
            except (OSError, RuntimeError, ValueError) as exc:
                self._send_json(
                    {"error": str(exc)},
                    status=HTTPStatus.CONFLICT,
                )
                return

            self._send_json(
                {
                    "ok": True,
                    "date": day,
                    "entry": notes.get(day),
                    "count": len(notes),
                    "path": _display_path(notes_path),
                }
            )

        def _write_error(self) -> str | None:
            host = self.headers.get("host", "")
            if host and not _is_loopback_host(host):
                return "Write requests must use localhost, 127.0.0.1, or ::1."
            for header in ("origin", "referer"):
                value = self.headers.get(header)
                if value and not _origin_matches_host(value, host):
                    return "Cross-origin writes are not allowed."
            if self.headers.get(WRITE_TOKEN_HEADER) != write_token:
                return "Missing or invalid write token."
            try:
                current_generation = workspace_identity(data_dir)
            except OSError:
                return "Workspace no longer exists; restart the Training Center."
            if current_generation != workspace_generation:
                return "Workspace changed; restart the Training Center."
            return None

        def _read_json_body(self) -> dict[str, Any]:
            try:
                length = int(self.headers.get("content-length") or "0")
            except ValueError as exc:
                raise ValueError("Invalid content length.") from exc
            if length > MAX_BODY_BYTES:
                raise ValueError("Request body is too large.")
            raw = self.rfile.read(length)
            if not raw:
                return {}
            try:
                body = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError("Body must be JSON.") from exc
            if not isinstance(body, dict):
                raise ValueError("Body must be a JSON object.")
            return body

        def _import_strava_archive_upload(self) -> dict[str, Any]:
            try:
                length = int(self.headers.get("content-length") or "0")
            except ValueError as exc:
                raise ValueError("Invalid content length.") from exc
            if length <= 0:
                raise ValueError("Choose a downloaded Strava archive ZIP or activities.csv file.")
            if length > MAX_STRAVA_ARCHIVE_BYTES:
                raise ValueError("Strava archive upload is too large; use import-strava-export from the command line.")

            with workspace_lock(
                data_dir,
                expected_identity=workspace_generation,
            ):
                upload_name = _strava_archive_upload_name(self.headers.get(UPLOAD_NAME_HEADER, ""))
                upload_dir = data_dir / "imports" / "strava-export"
                upload_dir.mkdir(parents=True, exist_ok=True)
                try:
                    upload_dir.chmod(0o700)
                except OSError:
                    pass
                stored_name = (
                    f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-"
                    f"{secrets.token_hex(4)}-{upload_name}"
                )
                target_path = upload_dir / stored_name
                partial_path = upload_dir / f".{stored_name}.part"
                remaining = length
                try:
                    with partial_path.open("wb") as handle:
                        while remaining:
                            chunk = self.rfile.read(min(1024 * 1024, remaining))
                            if not chunk:
                                raise ValueError(
                                    "Strava archive upload ended before the declared content length."
                                )
                            handle.write(chunk)
                            remaining -= len(chunk)
                    partial_path.replace(target_path)
                    try:
                        target_path.chmod(0o600)
                    except OSError:
                        pass
                finally:
                    partial_path.unlink(missing_ok=True)

            with lock, workspace_lock(
                data_dir,
                expected_identity=workspace_generation,
            ):
                try:
                    result = import_strava_export(data_dir, target_path)
                except Exception:
                    target_path.unlink(missing_ok=True)
                    raise
                canonical = build_canonical_files(data_dir)
                calendar_path = data_dir / "calendar.json"
                insights = build_insights(
                    data_dir,
                    calendar_path if calendar_path.exists() else None,
                    data_dir / "derived",
                )
                training_center = build_training_center(data_dir)
            return {
                "ok": True,
                "upload": {"path": target_path.relative_to(data_dir).as_posix()},
                "import": result,
                "canonical": canonical,
                "insights": insights,
                "training_center": training_center,
                "provider": provider_summary(data_dir, "strava"),
            }

        def _import_activity_recording_upload(self) -> dict[str, Any]:
            try:
                length = int(self.headers.get("content-length") or "0")
            except ValueError as exc:
                raise ValueError("Invalid content length.") from exc
            if length <= 0:
                raise ValueError("Choose a FIT, TCX, or GPX activity recording.")
            if length > MAX_ACTIVITY_RECORDING_BYTES:
                raise ValueError("Activity recording is too large; use a smaller source file.")

            with workspace_lock(
                data_dir,
                expected_identity=workspace_generation,
            ):
                upload_name = _activity_recording_upload_name(
                    self.headers.get(UPLOAD_NAME_HEADER, "")
                )
                upload_dir = data_dir / "imports" / "activity-recordings"
                upload_dir.mkdir(parents=True, exist_ok=True)
                try:
                    upload_dir.chmod(0o700)
                except OSError:
                    pass
                stored_name = (
                    f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-"
                    f"{secrets.token_hex(4)}-{upload_name}"
                )
                target_path = upload_dir / stored_name
                partial_path = upload_dir / f".{stored_name}.part"
                remaining = length
                try:
                    with partial_path.open("wb") as handle:
                        while remaining:
                            chunk = self.rfile.read(min(1024 * 1024, remaining))
                            if not chunk:
                                raise ValueError(
                                    "Activity recording upload ended before the declared content length."
                                )
                            handle.write(chunk)
                            remaining -= len(chunk)
                    partial_path.replace(target_path)
                    try:
                        target_path.chmod(0o600)
                    except OSError:
                        pass
                finally:
                    partial_path.unlink(missing_ok=True)

            with lock, workspace_lock(
                data_dir,
                expected_identity=workspace_generation,
            ):
                try:
                    result = import_activity_recording(
                        data_dir,
                        target_path,
                        filename=upload_name,
                    )
                except Exception:
                    target_path.unlink(missing_ok=True)
                    raise
                if not result["created"]:
                    target_path.unlink(missing_ok=True)
                canonical = build_canonical_files(data_dir)
                calendar_path = data_dir / "calendar.json"
                insights = build_insights(
                    data_dir,
                    calendar_path if calendar_path.exists() else None,
                    data_dir / "derived",
                )
                summary_path = data_dir / "derived" / "post_sync_summary.json"
                summary = read_json(summary_path, default={})
                if not isinstance(summary, dict):
                    summary = {}
                summary.update(
                    generated_at=_timestamp(),
                    canonical=canonical,
                    insights=insights,
                )
                summary.pop("training_center", None)
                write_json(summary_path, summary)
                training_center = build_training_center(data_dir)
                summary["training_center"] = training_center
                write_json(summary_path, summary)
            return {
                "ok": True,
                "upload": {
                    "path": (
                        target_path.relative_to(data_dir).as_posix()
                        if result["created"]
                        else None
                    ),
                    "deduplicated": not result["created"],
                },
                "import": result,
                "canonical": canonical,
                "insights": insights,
                "training_center": training_center,
            }

        def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
            body = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
            self.send_response(status)
            self.send_header("content-type", "application/json; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return TrainingCenterHandler


def serve_training_center(
    data_dir: Path,
    *,
    port: int = 8787,
    rebuild: bool = True,
    fallback_ports: int = 10,
) -> None:
    host = "127.0.0.1"
    # A detached server can outlive a launch-scoped proxy and otherwise retain dead provider routes.
    _remove_transient_loopback_proxies(os.environ)
    if rebuild:
        build_training_center(data_dir)
    handler = make_training_center_handler(data_dir)
    server = None
    attempted_ports: list[int] = []
    for candidate_port in range(port, port + max(0, fallback_ports) + 1):
        attempted_ports.append(candidate_port)
        try:
            server = ThreadingHTTPServer((host, candidate_port), handler)
            break
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise
    if server is None:
        attempted = ", ".join(str(candidate) for candidate in attempted_ports)
        raise SystemExit(
            f"No free training-center port on {host}; tried {attempted}. "
            "Stop an existing server or choose another --port."
        )
    actual_host, actual_port = server.server_address[:2]
    url = f"http://{actual_host}:{actual_port}/training_center.html"
    health_url = f"http://{actual_host}:{actual_port}/api/health"
    notes_path = data_dir / "plan" / "daily_notes.json"
    print(f"Serving training center at {url}", flush=True)
    print(f"Workspace ID: {_workspace_instance_id(data_dir)}", flush=True)
    print(f"Verify instance at {health_url}", flush=True)
    print(f"Daily notes write to {_display_path(notes_path)}", flush=True)
    print("Press Ctrl-C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped training center server.")
    finally:
        server.server_close()
