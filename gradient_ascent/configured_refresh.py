"""Explicitly enabled provider sync followed by one local workspace rebuild."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .config import ensure_private_data_dir, load_config
from .refresh import refresh_workspace
from .ride_connection import RideConnectionError, _sync_summary, sync_configured_ride
from .workspace_lock import workspace_identity, workspace_lock


def refresh_configured_workspace(
    data_dir: Path,
    *,
    expected_identity: tuple[int, int] | None = None,
    local_only: bool = False,
    ride_days: int | None = None,
    ride_history: bool = False,
    restart_history: bool = False,
) -> dict[str, Any]:
    data_dir = ensure_private_data_dir(data_dir, action="refresh coaching workspace")
    identity = expected_identity if expected_identity is not None else workspace_identity(data_dir)
    with workspace_lock(data_dir, expected_identity=identity):
        provider_sync = {
            "ridewithgps": (
                {"provider": "ridewithgps", "status": "local_only", "external_access": False}
                if local_only
                else sync_configured_ride(
                    data_dir,
                    expected_identity=identity,
                    days=ride_days,
                    full_history=ride_history,
                    restart=restart_history,
                )
            )
        }
        result = refresh_workspace(data_dir, expected_identity=identity)
        return {**result, "provider_sync": provider_sync}


def aggregate_refresh_result(result: dict[str, Any]) -> dict[str, Any]:
    canonical = result.get("canonical") or {}
    return validate_aggregate_refresh_result(
        {
            "activities": canonical.get("resolved_activities", canonical.get("activities", 0)),
            "activity_candidates": canonical.get("activities", 0),
            "recovery": canonical.get("recovery", 0),
            "provider_sync": result.get("provider_sync", {}),
        }
    )


def validate_aggregate_refresh_result(value: Any) -> dict[str, Any]:
    """Accept only the small schema allowed into the browser's sync log."""
    keys = {"activities", "activity_candidates", "recovery", "provider_sync"}
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("Invalid refresh summary.")
    for key in keys - {"provider_sync"}:
        number = value[key]
        if isinstance(number, bool) or not isinstance(number, int) or not 0 <= number <= 10_000_000:
            raise ValueError("Invalid refresh summary.")
    providers = value["provider_sync"]
    if not isinstance(providers, dict) or set(providers) != {"ridewithgps"}:
        raise ValueError("Invalid refresh summary.")
    ride = providers["ridewithgps"]
    if not isinstance(ride, dict) or ride.get("provider") != "ridewithgps":
        raise ValueError("Invalid refresh summary.")
    if ride.get("status") in {"not_configured", "local_only"}:
        if (
            set(ride) != {"provider", "status", "external_access"}
            or ride["external_access"] is not False
        ):
            raise ValueError("Invalid refresh summary.")
    elif ride.get("status") == "synced" and ride.get("external_access") is True:
        _sync_summary(
            {key: item for key, item in ride.items() if key not in {"status", "external_access"}}
        )
    else:
        raise ValueError("Invalid refresh summary.")
    return {key: value[key] for key in keys}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Refresh explicitly enabled sources and rebuild the local Training Center."
    )
    parser.add_argument("--data-dir", default=str(load_config().data_dir))
    parser.add_argument("--local-only", action="store_true")
    parser.add_argument("--ride-history", action="store_true")
    parser.add_argument("--restart-history", action="store_true")
    parser.add_argument("--expected-workspace-device", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--expected-workspace-inode", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    values = (args.expected_workspace_device, args.expected_workspace_inode)
    if (values[0] is None) != (values[1] is None):
        parser.error("expected workspace device and inode must be provided together")
    identity = (
        (int(values[0]), int(values[1]))
        if values[0] is not None and values[1] is not None
        else None
    )
    try:
        result = refresh_configured_workspace(
            Path(args.data_dir),
            expected_identity=identity,
            local_only=args.local_only,
            ride_history=args.ride_history,
            restart_history=args.restart_history,
        )
    except (RideConnectionError, OSError, RuntimeError, ValueError):
        raise SystemExit("Refresh failed. Check the local connection status and retry.") from None
    print(json.dumps(aggregate_refresh_result(result), sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
