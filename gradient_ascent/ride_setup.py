"""Small in-memory setup jobs for the localhost Connections screen."""

from __future__ import annotations

from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any

from .ride_cli import RideCLIError
from .ride_connection import RideConnectionError, check_ride, connect_ride, disable_ride


class RideSetupJobs:
    def __init__(self, data_dir: Path, expected_identity: tuple[int, int]) -> None:
        self._data_dir = data_dir
        self._expected_identity = expected_identity
        self._lock = Lock()
        self._cancel = Event()
        self._state: dict[str, Any] = {
            "status": "idle",
            "running": False,
            "action": None,
            "message": "Connect using Ride with GPS's official ride CLI.",
            "authorization_url": None,
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def _update(self, **changes: Any) -> None:
        with self._lock:
            self._state.update(changes)

    def start(self, action: str, *, install: bool = False, reauth: bool = False) -> dict[str, Any]:
        if (
            not isinstance(action, str)
            or action not in {"connect", "check", "disable"}
            or type(install) is not bool
            or type(reauth) is not bool
        ):
            raise RideConnectionError("Unknown Ride with GPS setup action.")
        if action != "connect" and (install or reauth):
            raise RideConnectionError("Only Connect may install or start sign-in.")
        with self._lock:
            if self._state["running"]:
                return dict(self._state)
            self._cancel = Event()
            cancel = self._cancel
            self._state = {
                "status": "running",
                "running": True,
                "action": action,
                "message": "Preparing the official ride CLI..."
                if action == "connect"
                else "Checking Ride with GPS..."
                if action == "check"
                else "Stopping Ride with GPS sync...",
                "authorization_url": None,
            }
        Thread(
            target=self._run,
            args=(action, install, reauth, cancel),
            name="ridewithgps-setup",
            daemon=True,
        ).start()
        return self.snapshot()

    def cancel(self) -> dict[str, Any]:
        with self._lock:
            if self._state["running"]:
                self._cancel.set()
                self._state.update(
                    status="cancelling",
                    message="Cancelling Ride with GPS sign-in...",
                    authorization_url=None,
                )
            return dict(self._state)

    def _run(self, action: str, install: bool, reauth: bool, cancel: Event) -> None:
        def authorization_url(url: str) -> None:
            if not cancel.is_set():
                self._update(
                    status="waiting_for_browser",
                    message="Open the sign-in link in your preferred browser profile.",
                    authorization_url=url,
                )

        try:
            if action == "connect":
                connect_ride(
                    self._data_dir,
                    install=install,
                    force_login=reauth,
                    on_authorization_url=authorization_url,
                    cancel=cancel,
                    expected_identity=self._expected_identity,
                )
                message = "Ride with GPS is connected. Choose Refresh to import recent rides."
            elif action == "check":
                check_ride(self._data_dir, expected_identity=self._expected_identity)
                message = "Ride with GPS sign-in is working."
            else:
                disable_ride(self._data_dir, expected_identity=self._expected_identity)
                message = "Ride with GPS sync is off. Existing rides and the vendor's sign-in are unchanged."
        except (RideConnectionError, RideCLIError) as exc:
            self._update(
                status="cancelled" if cancel.is_set() else "failed",
                running=False,
                authorization_url=None,
                message="Ride with GPS sign-in was cancelled." if cancel.is_set() else str(exc),
            )
        except (Exception, SystemExit):
            self._update(
                status="failed",
                running=False,
                authorization_url=None,
                message="Ride with GPS setup failed. Check the official ride CLI and try again.",
            )
        else:
            self._update(status="completed", running=False, authorization_url=None, message=message)
