from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .apple_health import import_apple_health_export
from .canonical import build_canonical_files
from .config import ensure_private_data_dir, load_config
from .connections import provider_values
from .garmin import import_garmin_export
from .insights import build_insights
from .storage import read_json, write_json
from .training_center import build_training_center
from .workspace_lock import workspace_lock


POST_SYNC_SUMMARY_FILENAME = "post_sync_summary.json"


def _date_coverage(items: list[dict[str, Any]], *keys: str) -> dict[str, Any]:
    dates: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        for key in keys:
            value = item.get(key)
            if value:
                date_value = str(value)[:10]
                if len(date_value) == 10:
                    dates.append(date_value)
                break
    return {
        "count": len(items),
        "first": min(dates) if dates else None,
        "last": max(dates) if dates else None,
    }


def _strava_coverage(data_dir: Path) -> dict[str, Any]:
    payload = read_json(data_dir / "strava" / "activities.json", default={}) or {}
    if isinstance(payload, dict):
        items = list(payload.values())
    elif isinstance(payload, list):
        items = payload
    else:
        items = []
    return _date_coverage(items, "start_date_local", "start_date", "date")


def _recording_coverage(data_dir: Path, *, source_provider: str | None = None) -> dict[str, Any]:
    payload = read_json(data_dir / "recordings" / "activities.json", default={}) or {}
    items = list(payload.values()) if isinstance(payload, dict) else []
    if source_provider is not None:
        items = [item for item in items if isinstance(item, dict) and item.get("source_provider") == source_provider]
    return _date_coverage(items, "start_date_local", "start_date", "date")


def _apple_health_coverage(data_dir: Path) -> dict[str, Any]:
    workouts = read_json(data_dir / "apple_health" / "workouts.json", default=[]) or []
    recovery = read_json(data_dir / "apple_health" / "recovery.json", default=[]) or []
    workout_items = workouts if isinstance(workouts, list) else []
    recovery_items = recovery if isinstance(recovery, list) else []
    return {
        "workouts": _date_coverage(workout_items, "start_date", "date"),
        "recovery": _date_coverage(recovery_items, "date"),
    }


def _garmin_coverage(data_dir: Path) -> dict[str, Any]:
    dates = sorted(path.stem for path in (data_dir / "garmin").glob("*.json"))
    return {
        "count": len(dates),
        "first": dates[0] if dates else None,
        "last": dates[-1] if dates else None,
    }


def _external_sync_coverage(data_dir: Path) -> dict[str, Any]:
    from .external_sync import load_external_sync_manifests

    return {
        manifest["provider"]["id"]: {
            "label": manifest["provider"]["label"],
            "activities": _date_coverage(
                manifest["activities"],
                "start_date_local",
                "start_date",
                "date",
            ),
            "recovery": _date_coverage(manifest["recovery"], "date"),
            "last_import_at": manifest.get("synced_at"),
        }
        for manifest in load_external_sync_manifests(data_dir)
    }


def _refresh_configured_imports(data_dir: Path) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for provider, importer in (
        ("apple_health", import_apple_health_export),
        ("garmin", import_garmin_export),
    ):
        export_path = str(provider_values(data_dir, provider).get("export_path") or "").strip()
        if not export_path:
            results[provider] = {"status": "not_configured"}
            continue
        try:
            result = importer(data_dir, Path(export_path).expanduser())
        except (FileNotFoundError, OSError, ValueError) as exc:
            results[provider] = {"status": "needs_attention", "error": str(exc)}
            continue
        results[provider] = {
            "status": "imported",
            "result": asdict(result) if is_dataclass(result) else result,
        }
    return results


def refresh_workspace(
    data_dir: Path,
    *,
    expected_identity: tuple[int, int] | None = None,
) -> dict[str, Any]:
    data_dir = ensure_private_data_dir(data_dir, action="refresh coaching workspace")
    with workspace_lock(data_dir, expected_identity=expected_identity):
        derived_dir = data_dir / "derived"
        calendar_path = data_dir / "calendar.json"

        imports = _refresh_configured_imports(data_dir)
        canonical = build_canonical_files(data_dir)
        insights = build_insights(
            data_dir,
            calendar_path if calendar_path.exists() else None,
            derived_dir,
        )
        summary = {
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "sources": {
                "strava": _strava_coverage(data_dir),
                "recordings": _recording_coverage(data_dir),
                "ridewithgps": _recording_coverage(data_dir, source_provider="ridewithgps"),
                "apple_health": _apple_health_coverage(data_dir),
                "garmin": _garmin_coverage(data_dir),
                "external": _external_sync_coverage(data_dir),
            },
            "imports": imports,
            "canonical": canonical,
            "insights": insights,
        }
        write_json(derived_dir / POST_SYNC_SUMMARY_FILENAME, summary)
        training_center = build_training_center(data_dir)
        summary["training_center"] = training_center
        write_json(derived_dir / POST_SYNC_SUMMARY_FILENAME, summary)
        return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rebuild canonical data, coaching summaries, and the local Training Center."
    )
    parser.add_argument(
        "--data-dir",
        default=str(load_config().data_dir),
        help="Private coaching workspace directory",
    )
    parser.add_argument(
        "--expected-workspace-device",
        type=int,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--expected-workspace-inode",
        type=int,
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()
    expected_values = (args.expected_workspace_device, args.expected_workspace_inode)
    if (expected_values[0] is None) != (expected_values[1] is None):
        parser.error("expected workspace device and inode must be provided together")
    expected_identity = (
        (int(expected_values[0]), int(expected_values[1]))
        if expected_values[0] is not None and expected_values[1] is not None
        else None
    )
    refresh_workspace(Path(args.data_dir), expected_identity=expected_identity)
    print("Workspace refresh complete")


if __name__ == "__main__":
    main()
