from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .apple_health import resolve_apple_health_export_xml
from .garmin import resolve_garmin_export_dir
from .storage import read_json, write_json


STRAVA_ACCOUNT_EXPORT_URL = "https://www.strava.com/athlete/download_my_account"
CONFIG_PATH = Path("connections") / "config.json"


@dataclass(frozen=True)
class ConnectionProvider:
    key: str
    label: str
    input_mode: str
    support_tier: str
    category: str
    summary: str
    fields: tuple[dict[str, str], ...] = ()
    export_url: str = ""
    archive_upload_available: bool = False
    notes: tuple[str, ...] = ()


PROVIDERS: dict[str, ConnectionProvider] = {
    "strava": ConnectionProvider(
        key="strava",
        label="Strava",
        input_mode="archive",
        support_tier="official",
        category="activity",
        summary=(
            "Import an official account archive for local ride history, recordings, "
            "laps, and streams."
        ),
        export_url=STRAVA_ACCOUNT_EXPORT_URL,
        archive_upload_available=True,
        notes=("Upload a newer archive when you want to refresh local ride history.",),
    ),
    "apple_health": ConnectionProvider(
        key="apple_health",
        label="Apple Health",
        input_mode="file",
        support_tier="import_only",
        category="recovery",
        summary="Import workouts and recovery signals from a local Apple Health export.",
        fields=(
            {
                "key": "export_path",
                "label": "Local export path",
            },
        ),
        notes=("The export stays local and is parsed on this device.",),
    ),
    "garmin": ConnectionProvider(
        key="garmin",
        label="Garmin Connect",
        input_mode="file",
        support_tier="import_only",
        category="recovery",
        summary="Import recovery signals from an official Garmin Connect account export.",
        fields=(
            {
                "key": "export_path",
                "label": "Local export path",
            },
        ),
        notes=("The export stays local and is parsed on this device.",),
    ),
}


def provider_keys() -> tuple[str, ...]:
    return tuple(PROVIDERS)


def provider_spec(provider: str) -> ConnectionProvider:
    try:
        return PROVIDERS[provider]
    except KeyError as exc:
        raise KeyError(f"Unknown provider: {provider}") from exc


def connection_config_path(data_dir: Path) -> Path:
    return data_dir / CONFIG_PATH


def _load_connection_config(data_dir: Path) -> dict[str, Any]:
    payload = read_json(
        connection_config_path(data_dir),
        default={"version": 1, "providers": {}},
    ) or {}
    providers = payload.get("providers")
    return {
        "version": 1,
        "providers": providers if isinstance(providers, dict) else {},
    }


def ensure_connection_layout(data_dir: Path) -> None:
    path = connection_config_path(data_dir)
    if not path.exists():
        write_json(path, {"version": 1, "providers": {}})


def provider_values(data_dir: Path, provider: str) -> dict[str, Any]:
    provider_spec(provider)
    config = _load_connection_config(data_dir)
    value = config["providers"].get(provider)
    if not isinstance(value, dict):
        return {}
    fields = value.get("fields")
    return fields if isinstance(fields, dict) else {}


def update_provider(
    data_dir: Path,
    provider: str,
    *,
    fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    spec = provider_spec(provider)
    ensure_connection_layout(data_dir)
    config = _load_connection_config(data_dir)
    existing_provider_config = config["providers"].get(provider)
    if not isinstance(existing_provider_config, dict):
        existing_provider_config = {}
    provider_config: dict[str, Any] = {}

    allowed_fields = {field["key"] for field in spec.fields}
    stored_fields = existing_provider_config.get("fields")
    if not isinstance(stored_fields, dict):
        stored_fields = {}
    for key, value in (fields or {}).items():
        if key not in allowed_fields:
            continue
        text = str(value or "").strip()
        if text:
            stored_fields[key] = text
        else:
            stored_fields.pop(key, None)
    if stored_fields:
        provider_config["fields"] = stored_fields
    else:
        provider_config.pop("fields", None)
    config["providers"][provider] = provider_config
    write_json(connection_config_path(data_dir), config)
    return provider_summary(data_dir, provider)


def _strava_archive_state(data_dir: Path) -> dict[str, Any]:
    payload = read_json(data_dir / "strava" / "state.json", default={}) or {}
    if not isinstance(payload, dict):
        return {}
    archive_import = payload.get("archive_import")
    return archive_import if isinstance(archive_import, dict) else {}


def _apple_health_imported(data_dir: Path) -> bool:
    return any(
        path.exists()
        for path in (
            data_dir / "apple_health" / "workouts.json",
            data_dir / "apple_health" / "recovery.json",
        )
    )


def _garmin_imported(data_dir: Path) -> bool:
    return any((data_dir / "garmin").glob("*.json"))


def _status_for(data_dir: Path, provider: str) -> tuple[str, list[str], list[str]]:
    if provider == "strava":
        if _strava_archive_state(data_dir).get("imported_at"):
            return (
                "imported",
                [],
                ["Upload a newer official archive when you want to refresh ride history."],
            )
        activities = read_json(data_dir / "strava" / "activities.json", default={})
        if isinstance(activities, dict) and activities:
            return (
                "imported",
                [],
                ["Local ride history is available. Upload an official archive to update it."],
            )
        return (
            "needs_setup",
            ["No Strava archive has been imported."],
            ["Request your official Strava archive, then upload its ZIP here."],
        )

    values = provider_values(data_dir, provider)
    imported = _apple_health_imported(data_dir) if provider == "apple_health" else _garmin_imported(data_dir)
    label = "Apple Health" if provider == "apple_health" else "Garmin Connect"
    if imported:
        return (
            "imported",
            [],
            [f"Choose Refresh at the top of the Training Center after the local {label} export changes."],
        )
    if values.get("export_path"):
        return (
            "configured",
            [],
            [f"Check the path, then choose Refresh at the top of the Training Center to import {label}."],
        )
    return (
        "optional",
        [],
        [f"Add a local {label} export path, or continue without this recovery source."],
    )


def provider_summary(data_dir: Path, provider: str) -> dict[str, Any]:
    spec = provider_spec(provider)
    config = _load_connection_config(data_dir)
    provider_config = config["providers"].get(provider)
    if not isinstance(provider_config, dict):
        provider_config = {}
    values = provider_values(data_dir, provider)
    status, issues, next_steps = _status_for(data_dir, provider)
    archive_state = _strava_archive_state(data_dir) if provider == "strava" else {}
    return {
        "key": spec.key,
        "label": spec.label,
        "input_mode": spec.input_mode,
        "support_tier": spec.support_tier,
        "category": spec.category,
        "summary": spec.summary,
        "export_url": spec.export_url,
        "archive_upload_available": spec.archive_upload_available,
        "fields": list(spec.fields),
        "configured_fields": {
            field["key"]: bool(values.get(field["key"]))
            for field in spec.fields
        },
        "archive_imported": bool(archive_state.get("imported_at")),
        "status": status,
        "issues": issues,
        "next_steps": next_steps,
        "notes": list(spec.notes),
        "test_available": provider in {"apple_health", "garmin"} and bool(values.get("export_path")),
        "last_import_at": archive_state.get("imported_at"),
    }


def _external_provider_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    provider = manifest["provider"]
    activity_count = len(manifest["activities"])
    recovery_count = len(manifest["recovery"])
    if activity_count and recovery_count:
        category = "activity_and_recovery"
    elif activity_count:
        category = "activity"
    else:
        category = "recovery"
    return {
        "key": f"external:{provider['id']}",
        "label": provider["label"],
        "input_mode": "manifest",
        "support_tier": "optional_companion",
        "category": category,
        "summary": (
            f"Local companion manifest with {activity_count} activities and "
            f"{recovery_count} recovery records."
        ),
        "export_url": "",
        "archive_upload_available": False,
        "fields": [],
        "configured_fields": {},
        "archive_imported": False,
        "status": "imported",
        "issues": [],
        "next_steps": ["Import a newer local sync manifest, then refresh the Training Center."],
        "notes": ["Companion sources are read-only and never accept provider credentials."],
        "test_available": False,
        "last_import_at": manifest.get("synced_at"),
    }


def connections_payload(data_dir: Path) -> dict[str, Any]:
    from .external_sync import load_external_sync_manifests

    providers = [provider_summary(data_dir, provider) for provider in provider_keys()]
    providers.extend(
        _external_provider_summary(manifest)
        for manifest in load_external_sync_manifests(data_dir)
    )
    counts: dict[str, int] = {}
    for provider in providers:
        status = str(provider["status"])
        counts[status] = counts.get(status, 0) + 1
    return {
        "version": 1,
        "providers": providers,
        "counts": counts,
    }


def connections_summary_payload(data_dir: Path) -> dict[str, Any]:
    payload = connections_payload(data_dir)
    return {
        "version": payload["version"],
        "available": [
            {
                "key": provider["key"],
                "label": provider["label"],
                "status": provider["status"],
                "archive_imported": provider["archive_imported"],
                "last_import_at": provider["last_import_at"],
                "next_action": (provider["next_steps"] or [None])[0],
            }
            for provider in payload["providers"]
        ],
    }


def check_provider(data_dir: Path, provider: str) -> dict[str, Any]:
    summary = provider_summary(data_dir, provider)
    if provider == "strava":
        return {
            "provider": provider,
            "ok": summary["status"] == "imported",
            "status": summary["status"],
            "issues": summary["issues"],
            "next_steps": summary["next_steps"],
        }

    export_path = str(provider_values(data_dir, provider).get("export_path") or "").strip()
    if not export_path:
        label = summary["label"]
        return {
            "provider": provider,
            "ok": False,
            "status": summary["status"],
            "issues": [f"No local {label} export path is configured."],
            "next_steps": summary["next_steps"],
        }
    try:
        if provider == "apple_health":
            resolved = resolve_apple_health_export_xml(Path(export_path).expanduser())
        else:
            resolved = resolve_garmin_export_dir(Path(export_path).expanduser())
    except (FileNotFoundError, OSError) as exc:
        return {
            "provider": provider,
            "ok": False,
            "status": "needs_attention",
            "issues": [str(exc)],
            "next_steps": [f"Choose a valid local {summary['label']} export path."],
        }
    return {
        "provider": provider,
        "ok": True,
        "status": "configured",
        "issues": [],
        "next_steps": [f"Ready to import {resolved}."],
    }
