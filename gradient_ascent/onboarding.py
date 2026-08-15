from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .storage import read_json, write_json


ONBOARDING_PATH = Path("plan") / "onboarding.json"
ALLOWED_CHOICES = {
    "events": {"none", "plan_file"},
    "plan": {"none", "file"},
    "activities": {"none", "strava_archive", "local_recordings", "external_sync"},
}


def _list_count(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0


def _activity_count(data_dir: Path) -> int:
    from .external_sync import load_external_sync_manifests

    total = 0
    for relative in ("strava/activities.json", "recordings/activities.json"):
        payload = read_json(data_dir / relative, default={}) or {}
        if isinstance(payload, (dict, list)):
            total += len(payload)
    total += sum(
        len(manifest["activities"])
        for manifest in load_external_sync_manifests(data_dir)
    )
    return total


def _profile_configured(data_dir: Path) -> bool:
    athlete = read_json(data_dir / "plan" / "athlete.json", default={}) or {}
    if not isinstance(athlete, dict):
        return False
    has_experience = bool(athlete.get("experience_level")) or athlete.get("experience_years") is not None
    return bool(
        athlete.get("timezone")
        and athlete.get("unit_system") in {"metric", "imperial"}
        and _list_count(athlete.get("disciplines"))
        and has_experience
        and athlete.get("weekly_availability")
    )


def _goals_configured(data_dir: Path) -> bool:
    path = data_dir / "plan" / "goals.md"
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8").strip().lower()
    return "## main goals" in text and "replace with" not in text


def _choices(data_dir: Path) -> dict[str, str]:
    payload = read_json(data_dir / ONBOARDING_PATH, default={}) or {}
    choices = payload.get("choices") if isinstance(payload, dict) else {}
    if not isinstance(choices, dict):
        return {}
    return {
        str(section): str(value)
        for section, value in choices.items()
        if section in ALLOWED_CHOICES and value in ALLOWED_CHOICES[section]
    }


def set_onboarding_choice(data_dir: Path, section: str, choice: str) -> dict[str, Any]:
    allowed = ALLOWED_CHOICES.get(section)
    if allowed is None:
        raise ValueError(f"Unsupported onboarding section: {section}")
    if choice not in allowed:
        raise ValueError(f"Unsupported {section} choice: {choice}")
    choices = _choices(data_dir)
    choices[section] = choice
    write_json(data_dir / ONBOARDING_PATH, {"version": 1, "choices": choices})
    return onboarding_status(data_dir)


def _required_single_line(value: str, *, field: str) -> str:
    normalized = " ".join(str(value).split())
    if not normalized:
        raise ValueError(f"{field} cannot be empty.")
    return normalized


def set_onboarding_goals(
    data_dir: Path,
    *,
    north_star: str,
    goal: str,
    why: str,
    success: str,
    coaching_implication: str,
    evidence: str,
) -> dict[str, Any]:
    if _goals_configured(data_dir):
        raise ValueError(
            "Goals are already configured; use the goals skill to revise them instead of replacing them."
        )
    values = {
        "north_star": _required_single_line(north_star, field="north_star"),
        "goal": _required_single_line(goal, field="goal"),
        "why": _required_single_line(why, field="why"),
        "success": _required_single_line(success, field="success"),
        "coaching_implication": _required_single_line(
            coaching_implication,
            field="coaching_implication",
        ),
        "evidence": _required_single_line(evidence, field="evidence"),
    }
    text = (
        f"# {values['north_star']}\n\n"
        "## Main Goals\n\n"
        f"### {values['goal']}\n\n"
        f"- **Why it matters:** {values['why']}\n"
        f"- **Success means:** {values['success']}\n"
        f"- **Coaching implication:** {values['coaching_implication']}\n\n"
        "## Measurement Plan\n\n"
        f"- **Direct and supporting evidence:** {values['evidence']}\n"
        "- **Incomplete evidence:** Say what is missing; do not infer success or failure.\n"
    )
    (data_dir / "plan" / "goals.md").write_text(text, encoding="utf-8")
    return onboarding_status(data_dir)


def _event_slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "event"


def add_onboarding_event(
    data_dir: Path,
    *,
    name: str,
    event_date: str,
    discipline: str,
    priority: str,
    location: str | None = None,
) -> dict[str, Any]:
    name = _required_single_line(name, field="name")
    discipline = _required_single_line(discipline, field="discipline")
    try:
        parsed_date = date.fromisoformat(event_date)
    except ValueError as exc:
        raise ValueError("event_date must be an ISO date in YYYY-MM-DD form.") from exc
    priority = str(priority).strip().upper()
    if priority not in {"A", "B", "C"}:
        raise ValueError("priority must be A, B, or C.")
    normalized_location = " ".join(str(location or "").split()) or None
    week_id = (parsed_date - timedelta(days=parsed_date.weekday())).isoformat()
    marker = "[commit]" if priority in {"A", "B"} else "[maybe]"
    raw = f"{marker} {parsed_date.isoformat()} {name}"
    if normalized_location:
        raw = f"{raw} - {normalized_location}"
    event = {
        "id": f"{parsed_date.isoformat()}-{_event_slug(name)}-{_event_slug(discipline)}",
        "date": parsed_date.isoformat(),
        "name": name,
        "location": normalized_location,
        "discipline": discipline,
        "priority": priority,
        "week_id": week_id,
        "markers": {
            "team_priority": False,
            "commitment": priority in {"A", "B"},
            "maybe": priority == "C",
            "skip": False,
        },
        "raw": raw,
    }
    events_path = data_dir / "plan" / "events.json"
    existing = read_json(events_path, default=[]) or []
    if not isinstance(existing, list):
        raise ValueError("Event calendar must be a JSON list.")
    events = [item for item in existing if isinstance(item, dict) and item.get("id") != event["id"]]
    events.append(event)
    events.sort(key=lambda item: (str(item.get("date") or ""), str(item.get("name") or "")))
    write_json(events_path, events)
    return onboarding_status(data_dir)


def _normalized_values(values: list[str], *, field: str, allow_empty: bool) -> list[str]:
    normalized: list[str] = []
    for value in values:
        text = str(value).strip()
        if text and text.lower() != "none" and text not in normalized:
            normalized.append(text)
    if not normalized and not allow_empty:
        raise ValueError(f"At least one {field} value is required.")
    return normalized


def set_onboarding_profile(
    data_dir: Path,
    *,
    display_name: str | None = None,
    timezone: str | None = None,
    unit_system: str | None = None,
    disciplines: list[str] | None = None,
    experience_level: str | None = None,
    weekly_availability: str | None = None,
    constraints: list[str] | None = None,
    sensors: list[str] | None = None,
) -> dict[str, Any]:
    path = data_dir / "plan" / "athlete.json"
    existing = read_json(path, default={}) or {}
    if not isinstance(existing, dict):
        raise ValueError("Athlete profile must be a JSON object.")
    updated = dict(existing)

    if display_name is not None:
        updated["display_name"] = display_name.strip()
    if timezone is not None:
        timezone = timezone.strip()
        try:
            ZoneInfo(timezone)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError(f"Unknown IANA timezone: {timezone}") from exc
        updated["timezone"] = timezone
    if unit_system is not None:
        if unit_system not in {"metric", "imperial"}:
            raise ValueError("unit_system must be metric or imperial.")
        updated["unit_system"] = unit_system
    if disciplines is not None:
        updated["disciplines"] = _normalized_values(
            disciplines,
            field="discipline",
            allow_empty=False,
        )
    if experience_level is not None:
        experience_level = experience_level.strip()
        if not experience_level:
            raise ValueError("experience_level cannot be empty.")
        updated["experience_level"] = experience_level
    if weekly_availability is not None:
        weekly_availability = weekly_availability.strip()
        if not weekly_availability:
            raise ValueError("weekly_availability cannot be empty.")
        updated["weekly_availability"] = weekly_availability
    if constraints is not None:
        updated["constraints"] = _normalized_values(
            constraints,
            field="constraint",
            allow_empty=True,
        )
    if sensors is not None:
        updated["sensors"] = _normalized_values(
            sensors,
            field="sensor",
            allow_empty=True,
        )

    write_json(path, updated)
    return onboarding_status(data_dir)


def onboarding_status(data_dir: Path) -> dict[str, Any]:
    choices = _choices(data_dir)
    weeks = read_json(data_dir / "plan" / "weeks.json", default=[]) or []
    events = read_json(data_dir / "plan" / "events.json", default=[]) or []
    activity_count = _activity_count(data_dir)

    workspace_ready = all(
        (data_dir / relative).exists()
        for relative in ("AGENTS.md", "plan/athlete.json", "plan/goals.md", "connections/config.json")
    )
    plan_ready = isinstance(weeks, list) and bool(weeks)
    events_ready = isinstance(events, list) and bool(events)
    activities_ready = activity_count > 0

    plan_status = "complete" if plan_ready else "skipped" if choices.get("plan") == "none" else "pending"
    activity_status = (
        "complete"
        if activities_ready
        else "skipped"
        if choices.get("activities") == "none"
        else "pending"
    )
    events_status = (
        "complete"
        if events_ready
        else "skipped"
        if choices.get("events") in {"none", "plan_file"}
        else "pending"
    )
    steps = [
        {"key": "workspace", "status": "complete" if workspace_ready else "pending"},
        {"key": "profile", "status": "complete" if _profile_configured(data_dir) else "pending"},
        {"key": "goals", "status": "complete" if _goals_configured(data_dir) else "pending"},
        {"key": "events", "status": events_status, "choice": choices.get("events")},
        {"key": "plan", "status": plan_status, "choice": choices.get("plan")},
        {"key": "activities", "status": activity_status, "choice": choices.get("activities")},
        {
            "key": "dashboard",
            "status": "complete" if (data_dir / "derived" / "training_center.html").exists() else "pending",
        },
    ]
    current = next(
        (step["key"] for step in steps if step["status"] not in {"complete", "skipped"}),
        None,
    )
    next_actions = {
        "workspace": "Initialize the private coaching workspace.",
        "profile": "Ask for timezone, units, cycling disciplines, experience, availability, constraints, and sensors.",
        "goals": "Define the rider's decision-driving goals and target events.",
        "events": "Add priority events or explicitly continue without a current target event.",
        "plan": "Ask whether to import a plan file or continue without a current plan.",
        "activities": (
            "Ask for a Strava archive, standalone FIT/TCX/GPX files, or an optional "
            "local companion sync manifest; otherwise continue without activity history."
        ),
        "dashboard": "Build the training center and report its local URL.",
    }
    return {
        "version": 1,
        "complete": current is None,
        "current_step": current,
        "next_action": next_actions.get(current),
        "steps": steps,
        "summary": {
            "planned_weeks": _list_count(weeks),
            "events": _list_count(events),
            "activities": activity_count,
        },
    }
