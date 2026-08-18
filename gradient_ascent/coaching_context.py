"""Bounded private recall across coaching entries, plan changes, and old notes."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from . import coaching_history as history
from .coach_notes import load_legacy_coach_notes, load_legacy_daily_notes


MAX_RECALL_LIMIT = 1000
MAX_LEGACY_BODY = 4096


def _date(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Coaching recall dates must be ISO dates.")
    try:
        parsed = date.fromisoformat(value).isoformat()
    except ValueError:
        raise ValueError("Coaching recall dates must be ISO dates.") from None
    if parsed != value:
        raise ValueError("Coaching recall dates must be ISO dates.")
    return parsed


def _overlaps(scopes: list[dict], start: str | None, end: str | None) -> bool:
    return not scopes or any(
        (start is None or scope["end_date"] >= start)
        and (end is None or scope["start_date"] <= end)
        for scope in scopes
    )


def _text(value: Any, limit: int) -> str:
    return value[:limit] if isinstance(value, str) else ""


def _legacy_note(row: dict, source: str) -> dict[str, Any]:
    body = row["note"]
    return {
        "id": _text(row.get("id"), 256),
        "kind": "observation",
        "source": source,
        "date": row["date"],
        "title": _text(row.get("title"), 256)
        or ("Coach note" if source == "coach-note" else "Daily note"),
        "body": body[:MAX_LEGACY_BODY],
        "body_truncated": len(body) > MAX_LEGACY_BODY,
        "ride_id": _text(row.get("ride_id"), 256),
        "activity_name": _text(row.get("activity_name"), 1024),
        "tags": [_text(tag, 64) for tag in row.get("tags", [])[:16] if isinstance(tag, str)]
        if isinstance(row.get("tags"), list)
        else [],
        "codex_url": row.get("codex_url", ""),
        "thread_id": row.get("codex_thread_id", ""),
        "updated_at": _text(row.get("updated_at") or row.get("created_at"), 64),
    }


def _plan_change(row: dict[str, Any]) -> dict[str, Any]:
    request = row.get("request") or {}
    decision = row.get("decision") or {}
    thread = request.get("thread_id", "")
    return {
        "id": row["id"],
        "kind": row["kind"],
        "status": row["status"],
        "created_at": row["created_at"],
        "date": row["created_at"][:10],
        "title": request.get("title", "Current-state baseline"),
        "rationale": request.get("rationale", "Records current files, not past changes."),
        "scopes": request.get("scopes", []),
        "decision_id": decision.get("id"),
        "decision_revision": decision.get("revision"),
        "files": sorted(row["files"]),
        "thread_id": thread,
        "codex_url": f"codex://threads/{thread}" if thread else "",
    }


def build_coaching_context(
    data_dir: Path,
    *,
    start: str | None = None,
    end: str | None = None,
    kind: str | None = None,
    limit: int = 50,
    include_revisions: bool = False,
) -> dict[str, Any]:
    """Return explicit compact recall; never open snapshots or run goal code."""
    start, end = _date(start), _date(end)
    if (
        type(limit) is not int
        or not 1 <= limit <= MAX_RECALL_LIMIT
        or type(include_revisions) is not bool
        or kind not in {None, "observation", "proposal", "decision"}
        or (start is not None and end is not None and start > end)
    ):
        raise ValueError("Coaching recall options are invalid.")
    data_dir = Path(data_dir)
    available = history.history_write_available(data_dir)
    entries = history.recall_coaching_history(
        data_dir,
        start=start,
        end=end,
        kind=kind,
        limit=history.MAX_RECORDS,
        include_revisions=include_revisions,
    )
    all_changes = history.plan_history(data_dir, limit=history.MAX_RECORDS)
    baseline = next((row for row in all_changes if row["kind"] == "baseline"), None)
    changes = [
        _plan_change(row)
        for row in all_changes
        if row["kind"] != "baseline"
        and _overlaps((row.get("request") or {}).get("scopes", []), start, end)
    ]
    coach_notes = load_legacy_coach_notes(data_dir)
    daily_notes = load_legacy_daily_notes(data_dir)
    legacy = []
    if kind in {None, "observation"}:
        for source, rows in (("coach-note", coach_notes), ("daily-note", daily_notes)):
            for row in rows:
                if (
                    row["note"].strip()
                    and (start is None or row["date"] >= start)
                    and (end is None or row["date"] <= end)
                ):
                    legacy.append(_legacy_note(row, source))
        legacy.sort(key=lambda row: (row["date"], row["updated_at"], row["id"]))
    summary = history.coaching_history_summary(data_dir)
    return {
        "entries": entries[-limit:],
        "plan_changes": changes[-limit:],
        "legacy_notes": legacy[-limit:],
        "summary": {
            **summary,
            "legacy_coach_notes": len(coach_notes),
            "legacy_daily_notes": len(daily_notes),
            "returned_entries": min(len(entries), limit),
            "returned_plan_changes": min(len(changes), limit),
            "returned_legacy_notes": min(len(legacy), limit),
            "matching_entries": len(entries),
            "matching_plan_changes": len(changes),
            "matching_legacy_notes": len(legacy),
            "truncated_entries": max(0, len(entries) - limit),
            "truncated_plan_changes": max(0, len(changes) - limit),
            "truncated_legacy_notes": max(0, len(legacy) - limit),
            "truncated": any(len(rows) > limit for rows in (entries, changes, legacy)),
        },
        "history": {
            "available": available,
            "recovery_required": summary["recovery_required"],
            "baseline_id": baseline["id"] if baseline else None,
            "baseline_created_at": baseline["created_at"] if baseline else None,
            "drift": history.plan_history_drift(data_dir),
        },
        "external_access": False,
    }
