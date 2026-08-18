from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import coaching_history as history
from . import recording_repair as files
from .storage import write_json
from .workspace_lock import workspace_lock


COACH_NOTES_VERSION = 1
MAX_LEGACY_NOTE_BYTES = 8 * 1024 * 1024
MAX_LEGACY_NOTES = 10000
THREAD_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_POSIX_PERMISSIONS = os.name == "posix"


def _notes_path(data_dir: Path) -> Path:
    return data_dir / "plan" / "coach_notes.json"


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse_date(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("Coach note date must be an ISO date.")
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
        raise ValueError(
            "Codex thread id may contain only letters, numbers, underscores, and hyphens."
        )
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
        if resolved_thread_id != parsed_thread_id:
            raise ValueError("Codex note link and thread id must agree.")
        return f"codex://threads/{parsed_thread_id}", resolved_thread_id
    resolved_thread_id = thread_id or os.environ.get("CODEX_THREAD_ID") or ""
    if resolved_thread_id:
        resolved_thread_id = _validated_thread_id(resolved_thread_id)
        return f"codex://threads/{resolved_thread_id}", resolved_thread_id
    return "", ""


def _safe_link(value: dict[str, Any]) -> tuple[str, str]:
    identifier = value.get("codex_thread_id") or value.get("thread_id") or ""
    url = value.get("codex_url") or ""
    try:
        return _codex_link(identifier, url) if identifier or url else ("", "")
    except (AttributeError, TypeError, ValueError):
        return "", ""


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Legacy coaching notes contain duplicate fields.")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError("Legacy coaching notes contain a nonfinite number.")


def _portable_entry(info: os.stat_result) -> None:
    if getattr(info, "st_file_attributes", 0) & getattr(
        stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
    ):
        raise ValueError("Legacy coaching note reparse point is unsafe.")
    # Windows synthesizes 0777/0666 stat modes; they are not POSIX ACL evidence.
    # Keep the existing ownership/write-bit check only where those bits mean it.
    if _POSIX_PERMISSIONS:
        files._owner(info)


def _portable_read(path: Path) -> bytes | None:
    for directory in (path.parent.parent, path.parent):
        try:
            info = directory.lstat()
        except FileNotFoundError:
            return None
        _portable_entry(info)
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("Legacy coaching note directory is unsafe.")
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    _portable_entry(info)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_LEGACY_NOTE_BYTES:
        raise ValueError("Legacy coaching notes are not a bounded regular file.")
    with path.open("rb") as handle:
        before = os.fstat(handle.fileno())
        _portable_entry(before)
        if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ):
            raise ValueError("Legacy coaching notes changed while opening.")
        body = handle.read(MAX_LEGACY_NOTE_BYTES + 1)
        after = os.fstat(handle.fileno())
        _portable_entry(after)
    if len(body) > MAX_LEGACY_NOTE_BYTES or (before.st_size, before.st_mtime_ns) != (
        after.st_size,
        after.st_mtime_ns,
    ):
        raise ValueError("Legacy coaching notes changed while reading.")
    return body


def _read_legacy_payload(data_dir: Path, name: str, *, daily: bool = False) -> dict[str, Any]:
    if name not in {"coach_notes.json", "daily_notes.json"}:
        raise ValueError("Unsupported legacy coaching note file.")
    default = {"version": COACH_NOTES_VERSION, "notes": {} if daily else []}
    if files._secure_files_supported():
        try:
            with files._directory(Path(data_dir), "plan") as directory:
                body = files._read(directory, name, MAX_LEGACY_NOTE_BYTES)
        except FileNotFoundError:
            body = None
    else:
        body = _portable_read(Path(data_dir) / "plan" / name)
    if body is None:
        return default
    try:
        payload = json.loads(body, object_pairs_hook=_unique, parse_constant=_reject_constant)
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("Legacy coaching notes contain invalid JSON.") from None
    if (
        not isinstance(payload, dict)
        or type(payload.get("version")) is not int
        or payload["version"] != COACH_NOTES_VERSION
        or not isinstance(payload.get("notes"), dict if daily else list)
        or len(payload["notes"]) > MAX_LEGACY_NOTES
    ):
        raise ValueError("Legacy coaching notes have an unsupported shape.")
    values = payload["notes"].items() if daily else ((None, row) for row in payload["notes"])
    for key, row in values:
        if not isinstance(row, dict) or not isinstance(row.get("note"), str):
            raise ValueError("Legacy coaching note is invalid.")
        day = _parse_date(row.get("date") or key)
        if daily and day != _parse_date(key):
            raise ValueError("Legacy coaching note date does not match its key.")
    return payload


def _legacy_view(value: dict[str, Any]) -> dict[str, Any]:
    result = dict(value)
    result["codex_url"], result["codex_thread_id"] = _safe_link(value)
    return result


def load_legacy_coach_notes(data_dir: Path) -> list[dict[str, Any]]:
    return [
        _legacy_view(row) for row in _read_legacy_payload(data_dir, "coach_notes.json")["notes"]
    ]


def load_legacy_daily_notes(data_dir: Path) -> list[dict[str, Any]]:
    return [
        _legacy_view({**row, "date": day})
        for day, row in _read_legacy_payload(data_dir, "daily_notes.json", daily=True)[
            "notes"
        ].items()
    ]


def _journal_note(entry: dict[str, Any], day: str) -> dict[str, Any]:
    activity = next((item for item in entry.get("evidence", []) if item["kind"] == "activity"), {})
    url, thread = _safe_link(entry)
    kind = entry["kind"]
    title = entry["title"] if kind == "observation" else f"{kind.title()}: {entry['title']}"
    return {
        "id": entry["id"],
        "date": day,
        "title": title,
        "note": entry["body"],
        "ride_id": activity.get("ref", ""),
        "activity_name": entry.get("activity_name") or activity.get("summary", ""),
        "tags": entry.get("tags", []),
        "codex_thread_id": thread,
        "codex_url": url,
        "source": "coaching-context",
        "kind": kind,
        "revision": entry["revision"],
        "created_at": entry["created_at"],
        "updated_at": entry["created_at"],
    }


def load_coach_notes(data_dir: Path) -> list[dict[str, Any]]:
    result = load_legacy_coach_notes(data_dir)
    for entry in history.recall_coaching_history(data_dir, limit=history.MAX_RECORDS):
        for day in sorted(
            {scope["start_date"] for scope in entry["scopes"] if scope["kind"] == "day"}
        ):
            result.append(_journal_note(entry, day))
    return sorted(result, key=lambda item: (item["date"], str(item.get("created_at") or "")))


def coach_notes_by_date(data_dir: Path) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for note in load_coach_notes(data_dir):
        result.setdefault(note["date"], []).append(note)
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
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    data_dir = Path(data_dir)
    parsed_date = _parse_date(note_date)
    if not isinstance(note, str) or not note.strip():
        raise ValueError("Coach note text is required.")
    resolved_url, resolved_thread_id = _codex_link(codex_thread_id, codex_url)
    activity_id, label = str(ride_id or "").strip(), str(activity_name or "").strip()
    draft: dict[str, Any] = {
        "kind": "observation",
        "title": (title or "").strip() or "Coach note",
        "body": note.strip(),
        "rationale": "Coach-authored ride or day observation.",
        "scopes": [{"kind": "day", "start_date": parsed_date, "end_date": parsed_date}],
        "tags": _tags(tags),
    }
    if resolved_thread_id:
        draft["thread_id"] = resolved_thread_id
    if activity_id:
        draft["evidence"] = [
            {"kind": "activity", "ref": activity_id, **({"summary": label} if label else {})}
        ]
    elif label:
        draft["activity_name"] = label
    digest = hashlib.sha256(
        json.dumps(draft, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    draft["idempotency_key"] = idempotency_key or f"coach-note-{digest}"
    normalized = history._entry_draft(draft)
    # Malformed old data must not be hidden by a successful new capture.
    _read_legacy_payload(data_dir, "coach_notes.json")
    if history.history_write_available(data_dir):
        captured = history.capture_coaching_entry(data_dir, normalized)
        saved = history.coaching_entry_by_id(
            data_dir, captured["id"], revision=captured["revision"]
        )
        if saved is None:
            raise RuntimeError("Saved coaching entry could not be read.")
        return {
            "path": str(data_dir / "plan/.history/journal.json"),
            "entry": _journal_note(saved, parsed_date),
            "count": len(load_coach_notes(data_dir)),
            "created": captured["created"],
            "history_status": "available",
        }
    with workspace_lock(data_dir):
        if history.history_write_available(data_dir):
            raise RuntimeError("Coaching history availability changed; retry.")
        legacy = _read_legacy_payload(data_dir, "coach_notes.json")
        for prior in legacy["notes"]:
            if prior.get("idempotency_key") == normalized["idempotency_key"]:
                if prior.get("capture_hash") != digest:
                    raise ValueError("Coach note idempotency key has different content.")
                return {
                    "path": str(_notes_path(data_dir)),
                    "entry": _legacy_view(prior),
                    "count": len(legacy["notes"]),
                    "created": False,
                    "history_status": "unavailable",
                }
        now = _now_utc()
        identifier = hashlib.sha256(
            json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        entry = {
            "id": f"{parsed_date}-coach-note-{identifier[:24]}",
            "date": parsed_date,
            "title": normalized["title"],
            "note": normalized["body"],
            "ride_id": activity_id,
            "activity_name": label,
            "tags": normalized.get("tags", []),
            "codex_thread_id": resolved_thread_id,
            "codex_url": resolved_url,
            "source": "coach-note",
            "created_at": now,
            "updated_at": now,
            "idempotency_key": normalized["idempotency_key"],
            "capture_hash": digest,
        }
        legacy["notes"].append(entry)
        legacy["notes"].sort(key=lambda item: (item["date"], str(item.get("created_at") or "")))
        if (
            len(legacy["notes"]) > MAX_LEGACY_NOTES
            or len((json.dumps(legacy, indent=2, sort_keys=True, allow_nan=False) + "\n").encode())
            > MAX_LEGACY_NOTE_BYTES
        ):
            raise ValueError("Legacy coaching notes exceed their supported limits.")
        write_json(_notes_path(data_dir), legacy)
        return {
            "path": str(_notes_path(data_dir)),
            "entry": entry,
            "count": len(legacy["notes"]),
            "created": True,
            "history_status": "unavailable",
        }
