from __future__ import annotations

import argparse
import json
import shlex
import shutil
import sys
from pathlib import Path

from .apple_health import import_apple_health_export
from .config import ensure_private_data_dir, ensure_private_output_path, load_config
from .calendar import ingest_calendar
from .coach_notes import add_coach_note
from .connections import (
    connections_payload,
    connections_summary_payload,
    ensure_connection_layout,
    check_provider,
    provider_keys,
    update_provider,
)
from .dashboard_labels import add_dashboard_label, react_to_ride
from .garmin import import_garmin_export
from .insights import build_insights
from .onboarding import (
    add_onboarding_event,
    onboarding_status,
    set_onboarding_choice,
    set_onboarding_goals,
    set_onboarding_profile,
)
from .plan import build_plan_from_csv
from .recordings import import_activity_recording
from .strava import import_strava_export
from .storage import ensure_text_line, write_json, write_text
from .util import parse_date
from .workspace import preview_workspace_purge, purge_workspace_data


WORKSPACE_GITIGNORE = "\n".join(
    [
        ".env",
        ".runtime/",
        "connections/ridewithgps.json",
        "exports/",
        "logs/",
        "*.log",
        ".codex/cache/",
        "derived/.cache/",
        "",
        "# Raw GPS and health exports are intentionally opt-in for git.",
        "# Remove these lines only for a private raw-data archive.",
        "strava/details/",
        "strava/laps/",
        "strava/streams/",
        "recordings/",
        "garmin/",
        "apple_health/",
        "integrations/",
        "imports/strava-export/",
        "imports/activity-recordings/",
        "imports/garmin-connect/",
        "imports/apple-health/",
        "",
    ]
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _workspace_refresh_lock(data_dir: Path, *, require_existing: bool = True):
    # Keep sanctioned source writes from racing a Training Center snapshot.
    # Imported lazily so lightweight setup/status commands stay fast.
    from .workspace_lock import workspace_lock

    return workspace_lock(data_dir, require_existing=require_existing)


def _read_goal_update_source(path: Path, *, label: str) -> str:
    source = path.expanduser()
    if source.is_symlink() or not source.is_file():
        raise ValueError(f"{label} source must be a regular file: {source}")
    if source.stat().st_size > 1024 * 1024:
        raise ValueError(f"{label} source is larger than 1 MiB: {source}")
    try:
        content = source.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label} source must be UTF-8 text: {source}") from exc
    if not content.strip():
        raise ValueError(f"{label} source cannot be empty: {source}")
    return content


def _install_goal_files(
    data_dir: Path,
    *,
    goals: str,
    measurement: str | None,
) -> list[str]:
    updates = [(data_dir / "plan" / "goals.md", goals)]
    if measurement is not None:
        updates.append((data_dir / "plan" / "goal_measurement.py", measurement))
    originals = {
        path: path.read_text(encoding="utf-8") if path.exists() else None
        for path, _content in updates
    }
    written: list[Path] = []
    try:
        for path, content in updates:
            write_text(path, content)
            written.append(path)
    except Exception as exc:
        rollback_errors: list[str] = []
        for path in reversed(written):
            try:
                original = originals[path]
                if original is None:
                    path.unlink(missing_ok=True)
                else:
                    write_text(path, original)
            except Exception as rollback_exc:  # pragma: no cover - catastrophic filesystem failure.
                rollback_errors.append(f"{path}: {rollback_exc}")
        if rollback_errors:
            raise RuntimeError(
                "Goal update failed and could not fully restore the previous files: "
                + "; ".join(rollback_errors)
            ) from exc
        raise
    return [path.relative_to(data_dir).as_posix() for path, _content in updates]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Gradient Ascent local cycling coach")
    subparsers = parser.add_subparsers(dest="command", required=True)
    provider_choices = provider_keys()

    init_parser = subparsers.add_parser(
        "init-data",
        help="Create a private coach workspace/data directory outside the plugin repo",
    )
    init_parser.add_argument(
        "--data-dir",
        default=None,
        help="Private workspace/data directory to initialize (defaults to COACH_WORKSPACE_DIR/COACH_DATA_DIR)",
    )
    workspace_parser = subparsers.add_parser(
        "init-workspace",
        help="Initialize a private coaching workspace in the target directory",
    )
    workspace_parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help="Workspace directory to initialize (defaults to current directory)",
    )
    workspace_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite starter template files that already exist",
    )

    purge_parser = subparsers.add_parser(
        "purge-workspace",
        help="Preview or irreversibly delete a complete private Gradient Ascent workspace",
    )
    purge_parser.add_argument(
        "path",
        help="Explicit Gradient Ascent workspace path; there is no implicit default for deletion",
    )
    purge_parser.add_argument(
        "--confirm",
        help="Exact resolved path printed by the preview; deletes the directory and any Git history",
    )

    onboarding_status_parser = subparsers.add_parser(
        "onboarding-status",
        help="Report compact prompt-driven onboarding progress",
    )
    onboarding_status_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable onboarding status",
    )

    onboarding_choice_parser = subparsers.add_parser(
        "onboarding-choice",
        help="Record a prompt-driven onboarding source choice",
    )
    onboarding_choice_parser.add_argument("section", choices=("events", "plan", "activities"))
    onboarding_choice_parser.add_argument("choice")

    onboarding_profile_parser = subparsers.add_parser(
        "onboarding-profile",
        help="Validate and merge rider profile answers without replacing existing metrics",
    )
    onboarding_profile_parser.add_argument("--display-name")
    onboarding_profile_parser.add_argument("--timezone")
    onboarding_profile_parser.add_argument("--unit-system", choices=("metric", "imperial"))
    onboarding_profile_parser.add_argument("--discipline", action="append", dest="disciplines")
    onboarding_profile_parser.add_argument("--experience-level")
    onboarding_profile_parser.add_argument("--weekly-availability")
    onboarding_profile_parser.add_argument("--constraint", action="append", dest="constraints")
    onboarding_profile_parser.add_argument("--sensor", action="append", dest="sensors")

    onboarding_goals_parser = subparsers.add_parser(
        "onboarding-goals",
        help="Write one complete decision-driving goal contract during initial setup",
    )
    onboarding_goals_parser.add_argument("--north-star", required=True)
    onboarding_goals_parser.add_argument("--goal", required=True)
    onboarding_goals_parser.add_argument("--why", required=True)
    onboarding_goals_parser.add_argument("--success", required=True)
    onboarding_goals_parser.add_argument("--coaching-implication", required=True)
    onboarding_goals_parser.add_argument("--evidence", required=True)

    onboarding_event_parser = subparsers.add_parser(
        "onboarding-event",
        help="Add or update a structured priority event during initial setup",
    )
    onboarding_event_parser.add_argument("--name", required=True)
    onboarding_event_parser.add_argument("--date", required=True, dest="event_date")
    onboarding_event_parser.add_argument("--discipline", default="cycling")
    onboarding_event_parser.add_argument(
        "--priority",
        required=True,
        type=str.upper,
        choices=("A", "B", "C"),
    )
    onboarding_event_parser.add_argument("--location")

    strava_import_parser = subparsers.add_parser(
        "import-strava-export",
        help="Import an official Strava account archive",
    )
    strava_import_parser.add_argument(
        "export_path",
        help="Path to a Strava account archive ZIP, extracted export directory, or activities.csv",
    )

    recording_import_parser = subparsers.add_parser(
        "import-activity-recording",
        help="Import one local FIT, TCX, or GPX activity recording",
    )
    recording_import_parser.add_argument("recording_path")

    sync_manifest_parser = subparsers.add_parser(
        "import-sync-manifest",
        help="Import a credential-free local companion activity or recovery manifest",
    )
    sync_manifest_parser.add_argument("manifest_path")

    apple_health_import_parser = subparsers.add_parser(
        "import-apple-health-export",
        help="Import a local Apple Health export directory or export.xml file",
    )
    apple_health_import_parser.add_argument("export_path")

    garmin_import_parser = subparsers.add_parser(
        "import-garmin-export",
        help="Import an official Garmin Connect account export from a local directory",
    )
    garmin_import_parser.add_argument("export_path")
    garmin_import_parser.add_argument("--start", help="Optional first date, YYYY-MM-DD")
    garmin_import_parser.add_argument("--end", help="Optional last date, YYYY-MM-DD")

    calendar_parser = subparsers.add_parser(
        "import-calendar", help="Import training calendar CSV/XLSX"
    )
    calendar_parser.add_argument("csv_path", help="Path to calendar CSV/XLSX")
    calendar_parser.add_argument(
        "--out",
        default=None,
        help="Optional output JSON path (defaults to config data_dir/calendar.json)",
    )

    plan_parser = subparsers.add_parser(
        "build-plan", help="Convert calendar CSV/XLSX into coach plan schema"
    )
    plan_parser.add_argument("csv_path", help="Path to calendar CSV/XLSX")
    plan_parser.add_argument(
        "--out-dir",
        default=None,
        help="Optional output dir (defaults to config data_dir/plan)",
    )

    budget_update_parser = subparsers.add_parser(
        "update-tss-budgets", help="Save explicit coach-authored weekly TSS budgets"
    )
    budget_update_parser.add_argument("--file", required=True, help="Version 1 budget draft JSON")
    budget_update_parser.add_argument(
        "--replace", action="store_true", help="Replace the complete coach-budget set"
    )
    budget_update_parser.add_argument(
        "--no-rebuild", action="store_true", help="Save without rebuilding local insights and dashboard"
    )
    budget_status_parser = subparsers.add_parser(
        "tss-budget-status", help="Show local coach-budget counts and review status"
    )
    budget_status_parser.add_argument(
        "--fingerprints",
        action="store_true",
        help="Also show source-week dates and plan fingerprints",
    )

    export_plan_parser = subparsers.add_parser(
        "export-plan", help="Export a private planned calendar or explicit device workouts"
    )
    export_plan_parser.add_argument("--format", choices=("zip", "ics", "csv", "fit"), default="zip")
    export_plan_parser.add_argument("--start", help="First included date, YYYY-MM-DD")
    export_plan_parser.add_argument("--end", help="Last included date, YYYY-MM-DD")
    export_plan_parser.add_argument("--workout", dest="workout_id", help="One explicit workout ID (required for FIT)")
    export_plan_parser.add_argument("--out", help="Destination file (defaults to private exports/planned)")
    export_plan_parser.add_argument("--overwrite", action="store_true", help="Allow replacing an existing different export")

    insights_parser = subparsers.add_parser(
        "build-insights", help="Merge supported local sources into summaries"
    )
    insights_parser.add_argument(
        "--calendar",
        default=None,
        help="Optional calendar JSON path (defaults to config data_dir/calendar.json)",
    )
    insights_parser.add_argument(
        "--out-dir",
        default=None,
        help="Optional output dir (defaults to config data_dir/derived)",
    )

    subparsers.add_parser(
        "build-training-center",
        help="Build the combined training center HTML and data bundle",
    )
    refresh_parser = subparsers.add_parser(
        "refresh",
        help="Refresh explicitly enabled sources and rebuild derived workspace artifacts",
    )
    refresh_parser.add_argument("--local-only", action="store_true", help="Rebuild local data without contacting Ride with GPS")

    ride_parser = subparsers.add_parser("ride", help="Set up and use Ride with GPS's official ride CLI")
    ride_actions = ride_parser.add_subparsers(dest="ride_action", required=True)
    ride_setup = ride_actions.add_parser("setup", help="Connect this workspace using vendor-owned sign-in")
    ride_setup.add_argument("--install", action="store_true", help="Allow downloading the checksum-verified official ride CLI if missing")
    ride_setup.add_argument("--executable", help="Optional independently installed official ride executable")
    ride_setup.add_argument("--config-dir", help="Optional existing vendor-owned ride configuration directory")
    ride_setup.add_argument("--days", type=int, help="Recent-sync lookback, 1–365 days (default 14)")
    ride_setup.add_argument("--reauth", action="store_true", help="Explicitly choose the account again in your browser")
    ride_actions.add_parser("status", help="Show offline connection status without contacting the provider")
    ride_actions.add_parser("check", help="Explicitly verify the vendor-owned sign-in and account")
    ride_sync = ride_actions.add_parser("sync", help="Import a bounded batch of rides and rebuild once")
    ride_sync.add_argument("--days", type=int, help="Override recent lookback for this sync")
    ride_sync.add_argument("--history", action="store_true", help="Import the next resumable full-history batch")
    ride_sync.add_argument("--restart-history", action="store_true", help="Restart a history scan from page one; existing rides are deduplicated")
    ride_actions.add_parser("disable", help="Stop future sync without deleting rides or the vendor's sign-in")

    serve_training_center_parser = subparsers.add_parser(
        "serve-training-center",
        help="Serve the training center with API writes to config data_dir/plan/daily_notes.json",
    )
    serve_training_center_parser.add_argument(
        "--port",
        type=int,
        default=8787,
        help="Port to bind (defaults to 8787)",
    )
    serve_training_center_parser.add_argument(
        "--strict-port",
        action="store_true",
        help="Fail instead of trying the next ten ports when the requested port is occupied",
    )
    serve_training_center_parser.add_argument(
        "--no-rebuild",
        action="store_true",
        help="Serve existing derived files without rebuilding first",
    )
    coach_note_parser = subparsers.add_parser(
        "add-coach-note",
        help="Add a synthesized coach note for a ride/day and rebuild the training center",
    )
    coach_note_parser.add_argument("--date", required=True, help="Ride/day date, YYYY-MM-DD")
    coach_note_parser.add_argument("--title", help="Short note title")
    coach_note_parser.add_argument("--note", required=True, help="Coach note text")
    coach_note_parser.add_argument("--ride-id", help="Optional Strava activity id")
    coach_note_parser.add_argument("--activity-name", help="Optional Strava activity title")
    coach_note_parser.add_argument("--tags", help="Optional comma-separated tags")
    coach_note_parser.add_argument("--codex-thread-id", help="Optional Codex thread id")
    coach_note_parser.add_argument(
        "--codex-url",
        help="Optional Codex thread URL using codex://threads/<thread-id>",
    )
    coach_note_parser.add_argument(
        "--no-rebuild",
        action="store_true",
        help="Write the note without rebuilding config data_dir/derived/training_center.html",
    )

    goal_update_parser = subparsers.add_parser(
        "update-goal-files",
        help="Safely update authored goal files and rebuild the training center",
    )
    goal_update_parser.add_argument(
        "--goals-file",
        required=True,
        help="UTF-8 Markdown file to install as plan/goals.md",
    )
    goal_update_parser.add_argument(
        "--measurement-file",
        help="Optional UTF-8 Python file to install as plan/goal_measurement.py",
    )
    goal_update_parser.add_argument(
        "--no-rebuild",
        action="store_true",
        help="Update goal files without rebuilding the Training Center",
    )

    label_parser = subparsers.add_parser(
        "add-dashboard-label",
        help="Add an authored dashboard label to a day or ride and rebuild the training center",
    )
    label_target = label_parser.add_mutually_exclusive_group(required=True)
    label_target.add_argument("--date", help="Day date, YYYY-MM-DD")
    label_target.add_argument("--ride-id", help="Strava activity id")
    label_parser.add_argument("--label", required=True, help="Dashboard label text")
    label_parser.add_argument("--short", help="Optional compact label for week cards")
    label_parser.add_argument("--title", help="Optional hover text")
    label_parser.add_argument(
        "--no-rebuild",
        action="store_true",
        help="Write the label without rebuilding config data_dir/derived/training_center.html",
    )

    ride_reaction_parser = subparsers.add_parser(
        "react-to-ride",
        help="Attach an emoji reaction to a ride and rebuild the training center",
    )
    ride_reaction_parser.add_argument("--ride-id", required=True, help="Strava activity id")
    ride_reaction_parser.add_argument("--emoji", required=True, help="Emoji reaction")
    ride_reaction_parser.add_argument(
        "--no-rebuild",
        action="store_true",
        help="Write the reaction without rebuilding config data_dir/derived/training_center.html",
    )

    connections_status_parser = subparsers.add_parser(
        "connections-status",
        help="Print machine-readable connection status for every supported provider",
    )
    connections_status_parser.add_argument(
        "--json",
        action="store_true",
        help="Print raw JSON only",
    )
    connections_status_parser.add_argument(
        "--summary",
        action="store_true",
        help="Return compact local-import status",
    )

    connections_set_parser = subparsers.add_parser(
        "connections-set",
        help="Store local import settings",
    )
    connections_set_parser.add_argument("provider", choices=provider_choices)
    connections_set_parser.add_argument(
        "--field",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Provider field to set; may be repeated",
    )

    connections_test_parser = subparsers.add_parser(
        "connections-test",
        help="Check whether a provider is configured enough to use",
    )
    connections_test_parser.add_argument("provider", choices=provider_choices)


    args = parser.parse_args()
    if args.command == "ride" and args.ride_action == "sync" and args.restart_history and not args.history:
        parser.error("--restart-history requires --history")
    return args



def _write_json_if_missing(path: Path, payload: object) -> None:
    if path.exists():
        return
    write_json(path, payload)


def _write_text_if_missing(path: Path, text: str) -> None:
    if path.exists():
        return
    write_text(path, text)


def _copy_starter_templates(workspace_dir: Path, *, force: bool) -> dict[str, object]:
    template_dir = Path(__file__).resolve().parent / "workspace_templates"
    if not template_dir.exists():
        raise SystemExit(
            f"Workspace template directory is missing: {template_dir}. "
            "Install Gradient Ascent from its checkout with `python -m pip install -e .`."
        )

    created: list[str] = []
    skipped: list[str] = []
    for source in sorted(template_dir.rglob("*")):
        relative = source.relative_to(template_dir)
        if "__pycache__" in relative.parts or source.suffix == ".pyc":
            continue
        target = workspace_dir / relative
        if source.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if target.exists() and not force:
            skipped.append(str(relative))
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        created.append(str(relative))
    return {"created": created, "skipped": skipped}


def _starter_template_text(relative: str) -> str:
    return (Path(__file__).resolve().parent / "workspace_templates" / relative).read_text(encoding="utf-8")


def _ensure_workspace_env(workspace_dir: Path) -> dict[str, object]:
    env_path = workspace_dir / ".env"
    if env_path.exists():
        return {"path": str(env_path), "created": False}

    example_path = workspace_dir / ".env.example"
    if example_path.exists():
        shutil.copy2(example_path, env_path)
    else:
        env_path.write_text("COACH_WORKSPACE_DIR=.\n", encoding="utf-8")
    try:
        env_path.chmod(0o600)
    except OSError:
        pass
    return {"path": str(env_path), "created": True}


def _ensure_workspace_cli(workspace_dir: Path) -> dict[str, object]:
    workspace_dir = workspace_dir.expanduser().resolve()
    cli_path = workspace_dir / ".codex" / "bin" / "gradient-ascent"
    cli_path.parent.mkdir(parents=True, exist_ok=True)
    created = not cli_path.exists()
    quoted_workspace = shlex.quote(str(workspace_dir))
    content = (
        "#!/bin/sh\n"
        f"export COACH_WORKSPACE_DIR={quoted_workspace}\n"
        f"cd {quoted_workspace} || exit 1\n"
        f"exec {shlex.quote(sys.executable)} -m gradient_ascent.cli \"$@\"\n"
    )
    if created or cli_path.read_text(encoding="utf-8") != content:
        cli_path.write_text(content, encoding="utf-8")
    cli_path.chmod(0o755)
    return {"path": str(cli_path), "created": created}


def _ensure_codex_environment(
    workspace_dir: Path,
    *,
    workspace_cli: Path,
) -> dict[str, object]:
    environment_path = workspace_dir / ".codex" / "environments" / "environment.toml"
    if environment_path.exists():
        return {"path": str(environment_path), "created": False}

    environment_path.parent.mkdir(parents=True, exist_ok=True)
    command = f'{shlex.quote(str(workspace_cli))} serve-training-center --port 8787'
    environment_path.write_text(
        "\n".join(
            [
                "version = 1",
                'name = "Gradient Ascent"',
                "",
                "[setup]",
                'script = ""',
                "",
                "[[actions]]",
                'name = "Training Center"',
                'icon = "run"',
                f"command = {json.dumps(command)}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return {"path": str(environment_path), "created": True}


def _init_workspace(workspace_dir: Path, *, force: bool) -> dict[str, object]:
    workspace_dir = ensure_private_data_dir(workspace_dir, action="initialize coach workspace")
    workspace_dir.parent.mkdir(parents=True, exist_ok=True)
    with _workspace_refresh_lock(workspace_dir, require_existing=False):
        workspace_dir.mkdir(parents=True, exist_ok=True)
        templates = _copy_starter_templates(workspace_dir, force=force)
        env = _ensure_workspace_env(workspace_dir)
        workspace_cli = _ensure_workspace_cli(workspace_dir)
        codex_environment = _ensure_codex_environment(
            workspace_dir,
            workspace_cli=Path(str(workspace_cli["path"])),
        )
        data = _init_data_dir(workspace_dir)
        return {
            "workspace_dir": str(workspace_dir),
            "templates": templates,
            "env": env,
            "workspace_cli": workspace_cli,
            "codex_environment": codex_environment,
            "data": data,
        }


def _init_data_dir(data_dir: Path) -> dict[str, object]:
    data_dir = ensure_private_data_dir(data_dir, action="initialize coach data")
    data_dir.parent.mkdir(parents=True, exist_ok=True)
    with _workspace_refresh_lock(data_dir, require_existing=False):
        data_dir.mkdir(parents=True, exist_ok=True)
        try:
            data_dir.chmod(0o700)
        except OSError:
            pass
        return _init_data_dir_unlocked(data_dir)


def _init_data_dir_unlocked(data_dir: Path) -> dict[str, object]:
    from .workspace_lock import ensure_workspace_generation

    ensure_workspace_generation(data_dir)
    for relative in [
        "plan",
        "connections",
        "canonical",
        "strava/details",
        "strava/laps",
        "strava/streams",
        "recordings/laps",
        "recordings/streams",
        "garmin",
        "apple_health",
        "derived",
        "logs",
        "imports/strava-export",
        "imports/activity-recordings",
        "imports/garmin-connect",
        "imports/apple-health",
    ]:
        (data_dir / relative).mkdir(parents=True, exist_ok=True)

    _write_json_if_missing(
        data_dir / "plan" / "athlete.json",
        {
            "age": None,
            "display_name": "",
            "disciplines": [],
            "experience_level": "",
            "experience_years": None,
            "ftp_w": None,
            "height_in": None,
            "notes": "",
            "profile_text": "",
            "race_category": "",
            "sensors": [],
            "timezone": "",
            "unit_system": "",
            "weekly_availability": "",
            "constraints": [],
            "weight_lb": None,
        },
    )
    _write_json_if_missing(data_dir / "plan" / "events.json", [])
    _write_json_if_missing(data_dir / "plan" / "onboarding.json", {"version": 1, "choices": {}})
    _write_text_if_missing(data_dir / "plan" / "goals.md", _starter_template_text("plan/goals.md"))
    _write_text_if_missing(
        data_dir / "plan" / "goals_template.md",
        _starter_template_text("plan/goals_template.md"),
    )
    _write_text_if_missing(
        data_dir / "plan" / "goal_measurement.py",
        _starter_template_text("plan/goal_measurement.py"),
    )
    _write_json_if_missing(data_dir / "plan" / "weeks.json", [])
    _write_json_if_missing(data_dir / "plan" / "workouts.json", {"version": 1, "workouts": []})
    _write_json_if_missing(data_dir / "plan" / "tss_budgets.json", {"version": 1, "budgets": []})
    _write_json_if_missing(data_dir / "plan" / "phases.json", [])
    _write_json_if_missing(data_dir / "plan" / "legend.json", {"markers": {}, "notes": None})
    _write_json_if_missing(data_dir / "plan" / "daily_notes.json", {"version": 1, "notes": {}})
    _write_json_if_missing(data_dir / "plan" / "coach_notes.json", {"version": 1, "notes": []})
    _write_json_if_missing(
        data_dir / "plan" / "device_corrections.json",
        {"version": 1, "temperature": []},
    )
    _write_json_if_missing(
        data_dir / "plan" / "dashboard_labels.json",
        {"version": 1, "days": {}, "rides": {}},
    )
    _write_json_if_missing(data_dir / "plan" / "context_markers.json", {"version": 1, "events": [], "markers": []})
    _write_json_if_missing(data_dir / "plan" / "garage.json", {"meta": {}, "bikes": []})
    _write_json_if_missing(data_dir / "strava" / "activities.json", {})
    _write_json_if_missing(data_dir / "recordings" / "activities.json", {})
    ensure_connection_layout(data_dir)
    _write_text_if_missing(data_dir / ".gitignore", WORKSPACE_GITIGNORE)
    ensure_text_line(data_dir / ".gitignore", ".codex/cache/")
    ensure_text_line(data_dir / ".gitignore", "derived/.cache/")
    ensure_text_line(data_dir / ".gitignore", "integrations/")
    ensure_text_line(data_dir / ".gitignore", ".runtime/")
    ensure_text_line(data_dir / ".gitignore", "connections/ridewithgps.json")
    ensure_text_line(data_dir / ".gitignore", "exports/")
    return {"data_dir": str(data_dir), "mode": "empty"}


def main() -> None:
    args = _parse_args()
    config = load_config()

    if args.command == "init-data":
        data_dir = Path(args.data_dir).expanduser() if args.data_dir else config.data_dir
        result = _init_data_dir(data_dir)
        print("Coach workspace/data directory initialized", json.dumps(result, sort_keys=True))
        return

    if args.command == "init-workspace":
        result = _init_workspace(Path(args.path).expanduser(), force=args.force)
        print("Coach workspace initialized", json.dumps(result, sort_keys=True))
        return

    if args.command == "purge-workspace":
        try:
            if args.confirm:
                payload = purge_workspace_data(
                    Path(args.path),
                    confirmation=args.confirm,
                )
            else:
                payload = {
                    **preview_workspace_purge(Path(args.path)),
                    "deleted": False,
                    "confirmation_required": True,
                }
        except ValueError as exc:
            raise SystemExit(str(exc))
        print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
        return

    ensure_private_data_dir(config.data_dir, action=f"run {args.command}")

    if args.command in {"update-tss-budgets", "tss-budget-status"}:
        from .tss_budgets import (
            plan_tss_budget_fingerprints,
            tss_budget_summary,
            update_tss_budgets,
        )
        from .workspace_lock import workspace_identity, workspace_lock

        try:
            identity = workspace_identity(config.data_dir)
            with workspace_lock(config.data_dir, expected_identity=identity):
                if args.command == "tss-budget-status":
                    result = {**tss_budget_summary(config.data_dir), "external_access": False}
                    if args.fingerprints:
                        result["weeks"] = [
                            {"start_date": start, "end_date": end, "plan_fingerprint": fingerprint}
                            for (start, end), fingerprint in sorted(
                                plan_tss_budget_fingerprints(config.data_dir).items()
                            )
                        ]
                else:
                    try:
                        updated = update_tss_budgets(
                            config.data_dir,
                            Path(args.file),
                            replace=args.replace,
                            expected_identity=identity,
                        )
                    except ValueError as exc:
                        # This schema-owned API uses controlled validation
                        # messages. Unrelated readers/builders below do not.
                        raise SystemExit(str(exc)) from None
                    result = {
                        **updated,
                        "rebuilt": False,
                        "external_access": False,
                    }
                    if not args.no_rebuild:
                        from .training_center import build_training_center

                        # Reproject the current source plan before rendering.
                        # No configured imports or provider refresh is needed.
                        calendar = config.data_dir / "calendar.json"
                        with workspace_lock(config.data_dir, expected_identity=identity):
                            build_insights(
                                config.data_dir,
                                calendar if calendar.exists() else None,
                                config.data_dir / "derived",
                            )
                        with workspace_lock(config.data_dir, expected_identity=identity):
                            build_training_center(config.data_dir)
                        result["rebuilt"] = True
                with workspace_lock(config.data_dir, expected_identity=identity):
                    pass
        except (OSError, RuntimeError, ValueError):
            raise SystemExit(
                "TSS budget action could not finish safely. Check the workspace and retry."
            ) from None
        print(json.dumps(result, separators=(",", ":"), sort_keys=True))
        return

    if args.command == "export-plan":
        from .plan_export import write_plan_export

        try:
            result = write_plan_export(
                config.data_dir,
                format=args.format,
                start=args.start,
                end=args.end,
                workout_id=args.workout_id,
                output_path=Path(args.out).expanduser() if args.out else None,
                overwrite=args.overwrite,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise SystemExit(str(exc)) from None
        print(json.dumps(result, separators=(",", ":"), sort_keys=True))
        return

    if args.command == "ride":
        from .configured_refresh import aggregate_refresh_result, refresh_configured_workspace
        from .ride_cli import RideCLIError
        from .ride_connection import (
            RideConnectionError,
            check_ride,
            connect_ride,
            disable_ride,
            load_ride_settings,
            ride_status,
        )

        try:
            if args.ride_action == "status":
                result = ride_status(config.data_dir)
            elif args.ride_action == "check":
                result = check_ride(config.data_dir)
            elif args.ride_action == "disable":
                result = disable_ride(config.data_dir)
            elif args.ride_action == "setup":
                allow_install = args.install
                if not allow_install and not args.executable and sys.stdin.isatty() and not ride_status(config.data_dir)["installed"]:
                    answer = input("Download the verified official Ride with GPS CLI into this private workspace? [y/N] ")
                    allow_install = answer.strip().lower() in {"y", "yes"}

                def show_authorization_url(url: str) -> None:
                    print("Open this link in your preferred browser profile:", flush=True)
                    print(url, flush=True)

                result = connect_ride(
                    config.data_dir,
                    install=allow_install,
                    executable=Path(args.executable) if args.executable else None,
                    config_dir=Path(args.config_dir) if args.config_dir else None,
                    days=args.days,
                    force_login=args.reauth,
                    on_authorization_url=show_authorization_url,
                )
            else:
                if not load_ride_settings(config.data_dir)["enabled"]:
                    raise RideConnectionError("Run gradient-ascent ride setup before syncing rides.")
                result = aggregate_refresh_result(refresh_configured_workspace(
                    config.data_dir, ride_days=args.days, ride_history=args.history,
                    restart_history=args.restart_history,
                ))
        except (RideCLIError, RideConnectionError) as exc:
            raise SystemExit(str(exc)) from None
        except (OSError, RuntimeError, ValueError):
            raise SystemExit("Ride with GPS could not complete this action. Check the connection and retry.") from None
        print(json.dumps(result, separators=(",", ":"), sort_keys=True))
        return

    if args.command == "onboarding-status":
        payload = onboarding_status(config.data_dir)
        if args.json:
            print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
        else:
            state = "complete" if payload["complete"] else f"next: {payload['current_step']}"
            print(f"Coach onboarding {state}")
        return

    if args.command == "onboarding-choice":
        try:
            with _workspace_refresh_lock(config.data_dir):
                payload = set_onboarding_choice(config.data_dir, args.section, args.choice)
        except ValueError as exc:
            raise SystemExit(str(exc))
        print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
        return

    if args.command == "onboarding-profile":
        try:
            with _workspace_refresh_lock(config.data_dir):
                payload = set_onboarding_profile(
                    config.data_dir,
                    display_name=args.display_name,
                    timezone=args.timezone,
                    unit_system=args.unit_system,
                    disciplines=args.disciplines,
                    experience_level=args.experience_level,
                    weekly_availability=args.weekly_availability,
                    constraints=args.constraints,
                    sensors=args.sensors,
                )
        except ValueError as exc:
            raise SystemExit(str(exc))
        print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
        return

    if args.command == "onboarding-goals":
        try:
            with _workspace_refresh_lock(config.data_dir):
                payload = set_onboarding_goals(
                    config.data_dir,
                    north_star=args.north_star,
                    goal=args.goal,
                    why=args.why,
                    success=args.success,
                    coaching_implication=args.coaching_implication,
                    evidence=args.evidence,
                )
        except ValueError as exc:
            raise SystemExit(str(exc))
        print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
        return

    if args.command == "onboarding-event":
        try:
            with _workspace_refresh_lock(config.data_dir):
                payload = add_onboarding_event(
                    config.data_dir,
                    name=args.name,
                    event_date=args.event_date,
                    discipline=args.discipline,
                    priority=args.priority,
                    location=args.location,
                )
        except ValueError as exc:
            raise SystemExit(str(exc))
        print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
        return

    if args.command == "import-strava-export":
        with _workspace_refresh_lock(config.data_dir):
            result = import_strava_export(config.data_dir, Path(args.export_path).expanduser())
        print("Strava export import complete", result)
        return

    if args.command == "import-activity-recording":
        with _workspace_refresh_lock(config.data_dir):
            result = import_activity_recording(
                config.data_dir,
                Path(args.recording_path).expanduser(),
            )
        print("Activity recording import complete", result)
        return

    if args.command == "import-sync-manifest":
        from .external_sync import import_sync_manifest

        try:
            with _workspace_refresh_lock(config.data_dir):
                result = import_sync_manifest(
                    config.data_dir,
                    Path(args.manifest_path).expanduser(),
                )
        except (OSError, RuntimeError, ValueError) as exc:
            raise SystemExit(str(exc))
        print("Sync manifest import complete", json.dumps(result, sort_keys=True))
        return

    if args.command == "import-apple-health-export":
        export_path = Path(args.export_path).expanduser()
        with _workspace_refresh_lock(config.data_dir):
            result = import_apple_health_export(config.data_dir, export_path)
            update_provider(
                config.data_dir,
                "apple_health",
                fields={"export_path": str(export_path.resolve())},
            )
        print("Apple Health export import complete", result)
        return

    if args.command == "import-garmin-export":
        export_path = Path(args.export_path).expanduser()
        try:
            with _workspace_refresh_lock(config.data_dir):
                result = import_garmin_export(
                    config.data_dir,
                    export_path,
                    start_date=parse_date(args.start) if args.start else None,
                    end_date=parse_date(args.end) if args.end else None,
                )
                update_provider(
                    config.data_dir,
                    "garmin",
                    fields={"export_path": str(export_path.resolve())},
                )
        except ValueError as exc:
            raise SystemExit(str(exc))
        print("Garmin Connect export import complete", result)
        return

    if args.command == "import-calendar":
        output_path = (
            ensure_private_output_path(Path(args.out), action="write imported calendar")
            if args.out
            else (config.data_dir / "calendar.json")
        )
        with _workspace_refresh_lock(config.data_dir):
            result = ingest_calendar(Path(args.csv_path), output_path)
        print("Calendar import complete", result)
        return

    if args.command == "build-plan":
        output_dir = (
            ensure_private_output_path(Path(args.out_dir), action="write plan output")
            if args.out_dir
            else (config.data_dir / "plan")
        )
        with _workspace_refresh_lock(config.data_dir):
            result = build_plan_from_csv(Path(args.csv_path), output_dir)
        print("Plan build complete", result)
        return

    if args.command == "build-insights":
        calendar_path = Path(args.calendar) if args.calendar else (config.data_dir / "calendar.json")
        output_dir = (
            ensure_private_output_path(Path(args.out_dir), action="write insights output")
            if args.out_dir
            else (config.data_dir / "derived")
        )
        with _workspace_refresh_lock(config.data_dir):
            result = build_insights(config.data_dir, calendar_path, output_dir)
        print("Insights build complete", result)
        return

    if args.command == "build-training-center":
        from .training_center import build_training_center

        result = build_training_center(config.data_dir)
        print("Training center build complete", result)
        return

    if args.command == "refresh":
        from .configured_refresh import aggregate_refresh_result, refresh_configured_workspace
        from .ride_connection import RideConnectionError

        try:
            result = refresh_configured_workspace(config.data_dir, local_only=args.local_only)
        except (RideConnectionError, OSError, RuntimeError, ValueError) as exc:
            message = str(exc) if isinstance(exc, RideConnectionError) else "Workspace refresh failed. Check Connections and retry."
            raise SystemExit(message) from None
        print("Workspace refresh complete", json.dumps(aggregate_refresh_result(result), separators=(",", ":"), sort_keys=True))
        return

    if args.command == "serve-training-center":
        from .training_center_server import serve_training_center

        serve_training_center(
            config.data_dir,
            port=args.port,
            rebuild=not args.no_rebuild,
            fallback_ports=0 if args.strict_port else 10,
        )
        return

    if args.command == "connections-status":
        payload = (
            connections_summary_payload(config.data_dir)
            if args.summary
            else connections_payload(config.data_dir)
        )
        if args.json:
            print(json.dumps(payload, sort_keys=True))
        else:
            providers = payload.get("providers") or payload.get("available") or []
            for provider in providers:
                print(f"{provider['label']}: {provider['status']}")
        return

    if args.command == "connections-set":
        fields: dict[str, str] = {}
        for item in args.field:
            key, separator, value = item.partition("=")
            if not separator or not key.strip():
                raise SystemExit(f"Expected --field KEY=VALUE, got {item!r}")
            fields[key.strip()] = value
        with _workspace_refresh_lock(config.data_dir):
            summary = update_provider(config.data_dir, args.provider, fields=fields)
        print("Connection updated", json.dumps(summary, sort_keys=True))
        return

    if args.command == "connections-test":
        print(json.dumps(check_provider(config.data_dir, args.provider), sort_keys=True))
        return

    if args.command == "add-coach-note":
        with _workspace_refresh_lock(config.data_dir):
            result = add_coach_note(
                config.data_dir,
                note_date=args.date,
                note=args.note,
                title=args.title,
                ride_id=args.ride_id,
                activity_name=args.activity_name,
                tags=args.tags,
                codex_thread_id=args.codex_thread_id,
                codex_url=args.codex_url,
            )
            if not args.no_rebuild:
                from .training_center import build_training_center

                result["training_center"] = build_training_center(config.data_dir)
        print("Coach note added", json.dumps(result, sort_keys=True))
        return

    if args.command == "update-goal-files":
        try:
            goals = _read_goal_update_source(Path(args.goals_file), label="Goals")
            measurement = (
                _read_goal_update_source(
                    Path(args.measurement_file),
                    label="Goal measurement",
                )
                if args.measurement_file
                else None
            )
            if measurement is not None:
                compile(measurement, args.measurement_file, "exec")
        except (OSError, SyntaxError, ValueError) as exc:
            raise SystemExit(str(exc))
        with _workspace_refresh_lock(config.data_dir):
            try:
                updated = _install_goal_files(
                    config.data_dir,
                    goals=goals,
                    measurement=measurement,
                )
            except (OSError, RuntimeError) as exc:
                raise SystemExit(str(exc))
            result: dict[str, object] = {"updated": updated}
            if not args.no_rebuild:
                from .training_center import build_training_center

                result["training_center"] = build_training_center(config.data_dir)
        print("Goal files updated", json.dumps(result, sort_keys=True))
        return

    if args.command == "add-dashboard-label":
        with _workspace_refresh_lock(config.data_dir):
            result = add_dashboard_label(
                config.data_dir,
                label=args.label,
                day=args.date,
                ride_id=args.ride_id,
                short=args.short,
                title=args.title,
            )
            if not args.no_rebuild:
                from .training_center import build_training_center

                result["training_center"] = build_training_center(config.data_dir)
        print("Dashboard label added", json.dumps(result, sort_keys=True))
        return

    if args.command == "react-to-ride":
        with _workspace_refresh_lock(config.data_dir):
            result = react_to_ride(
                config.data_dir,
                ride_id=args.ride_id,
                emoji=args.emoji,
            )
            if not args.no_rebuild:
                from .training_center import build_training_center

                result["training_center"] = build_training_center(config.data_dir)
        print("Ride reaction added", json.dumps(result, sort_keys=True))
        return

if __name__ == "__main__":
    main()
