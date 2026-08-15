from __future__ import annotations

import os
import re
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .storage import read_json, write_json


COACH_NOTES_VERSION = 1
THREAD_ID_RE = re.compile(r"[A-Za-z0-9_-]+")


def _notes_path(data_dir: Path) -> Path:
    return data_dir / "plan" / "coach_notes.json"


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _slug(value: str) -> str:
    lowered = value.lower()
    lowered = re.sub(r"[^a-z0-9]+", "-", lowered)
    return re.sub(r"-+", "-", lowered).strip("-") or "coach-note"


def _parse_date(value: str) -> str:
    return date.fromisoformat(value).isoformat()


def _tags(value: str | None) -> list[str]:
    if not value:
        return []
    tags = []
    for item in value.split(","):
        tag = item.strip()
        if tag:
            tags.append(tag)
    return tags


def _validated_thread_id(value: str) -> str:
    thread_id = value.strip()
    if not THREAD_ID_RE.fullmatch(thread_id):
        raise ValueError("Codex thread id may contain only letters, numbers, underscores, and hyphens.")
    return thread_id


def _codex_link(thread_id: str | None = None, codex_url: str | None = None) -> tuple[str, str]:
    if codex_url:
        clean_url = codex_url.strip()
        parsed = urlparse(clean_url)
        parsed_thread_id = parsed.path.strip("/")
        if (
            parsed.scheme.lower() != "codex"
            or parsed.netloc.lower() != "threads"
            or not THREAD_ID_RE.fullmatch(parsed_thread_id)
            or parsed.params
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Codex note links must use codex://threads/<thread-id>.")
        resolved_thread_id = _validated_thread_id(thread_id) if thread_id else parsed_thread_id
        return f"codex://threads/{parsed_thread_id}", resolved_thread_id
    resolved_thread_id = thread_id or os.environ.get("CODEX_THREAD_ID") or ""
    if resolved_thread_id:
        resolved_thread_id = _validated_thread_id(resolved_thread_id)
        return f"codex://threads/{resolved_thread_id}", resolved_thread_id
    return "", ""


def _load_payload(path: Path) -> dict[str, Any]:
    payload = read_json(path, default={"version": COACH_NOTES_VERSION, "notes": []})
    if not isinstance(payload, dict):
        return {"version": COACH_NOTES_VERSION, "notes": []}
    notes = payload.get("notes")
    if not isinstance(notes, list):
        notes = []
    return {"version": COACH_NOTES_VERSION, "notes": notes}


def load_coach_notes(data_dir: Path) -> list[dict[str, Any]]:
    payload = _load_payload(_notes_path(data_dir))
    return [note for note in payload["notes"] if isinstance(note, dict)]


def coach_notes_by_date(data_dir: Path) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for note in load_coach_notes(data_dir):
        day = str(note.get("date") or "")
        if not day:
            continue
        result.setdefault(day, []).append(note)
    for notes in result.values():
        notes.sort(key=lambda item: str(item.get("created_at") or ""))
    return result


def add_coach_note(
    data_dir: Path,
    *,
    note_date: str,
    note: str,
    title: str | None = None,
    ride_id: str | None = None,
    activity_name: str | None = None,
    tags: str | None = None,
    codex_thread_id: str | None = None,
    codex_url: str | None = None,
) -> dict[str, Any]:
    parsed_date = _parse_date(note_date)
    clean_note = note.strip()
    if not clean_note:
        raise ValueError("Coach note text is required.")

    created_at = _now_utc()
    resolved_url, resolved_thread_id = _codex_link(codex_thread_id, codex_url)
    note_title = (title or "").strip() or "Coach note"
    entry = {
        "id": f"{parsed_date}-{_slug(note_title)}-{uuid.uuid4().hex[:8]}",
        "date": parsed_date,
        "title": note_title,
        "note": clean_note,
        "ride_id": str(ride_id or "").strip(),
        "activity_name": str(activity_name or "").strip(),
        "tags": _tags(tags),
        "codex_thread_id": resolved_thread_id,
        "codex_url": resolved_url,
        "source": "coach-note",
        "created_at": created_at,
        "updated_at": created_at,
    }

    path = _notes_path(data_dir)
    payload = _load_payload(path)
    payload["notes"].append(entry)
    payload["notes"].sort(key=lambda item: (str(item.get("date") or ""), str(item.get("created_at") or "")))
    write_json(path, payload)
    return {"path": str(path), "entry": entry, "count": len(payload["notes"])}
