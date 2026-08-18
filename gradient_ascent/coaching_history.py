"""Private coaching recall and an allowlisted, local-only plan write-ahead log.

Callers validate domain semantics before handing this engine exact bytes. A
multi-file change is recoverable, not atomic; divergent files are never rolled
back automatically. Reading or recording a decision does not apply a plan.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import os
import re
import stat
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from . import recording_repair as _files
from .workspace_lock import workspace_identity, workspace_lock


ALLOWED_PLAN_FILES = frozenset(
    {
        "calendar.json",
        *(
            f"plan/{name}"
            for name in (
                "athlete.json",
                "events.json",
                "weeks.json",
                "phases.json",
                "legend.json",
                "workouts.json",
                "tss_budgets.json",
                "goals.md",
                "goal_measurement.py",
            )
        ),
    }
)
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_CHANGE_BYTES = 32 * 1024 * 1024
MAX_HISTORY_DETAIL_BYTES = MAX_CHANGE_BYTES * 2
MAX_JOURNAL_BYTES = 32 * 1024 * 1024
MAX_RECORDS = 10000
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,127}\Z")
_THREAD = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_KINDS = {"observation", "proposal", "decision"}
_STATES = {"prepared", "applied", "failed", "recovery_required", "restored"}
_COMMON = {
    "idempotency_key",
    "title",
    "rationale",
    "scopes",
    "thread_id",
    "conditions",
    "evidence",
    "tags",
}
_ENTRY_KEYS = _COMMON | {"kind", "body", "id", "expected_revision", "related_ids", "activity_name"}
_REQUEST_KEYS = _COMMON | {"decision_id"}


def _secure_files_supported() -> bool:
    return _files._secure_files_supported()


def _empty() -> dict[str, Any]:
    return {"version": 1, "entries": [], "transactions": []}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _text(value: Any, limit: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()) or "\0" in value:
        raise ValueError("Coaching history text is invalid.")
    try:
        valid = len(value.encode("utf-8")) <= limit
    except UnicodeError:
        valid = False
    if not valid or any((ord(c) < 32 and c not in "\t\r\n") or ord(c) == 127 for c in value):
        raise ValueError("Coaching history text exceeds its limits.")
    return value.strip()


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("Coaching history identifier is invalid.")
    return value


def _day(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("Coaching history dates must be ISO dates.")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise ValueError("Coaching history dates must be ISO dates.") from None


def _scopes(value: Any, *, required: bool) -> list[dict[str, str]]:
    if not isinstance(value, list) or not int(required) <= len(value) <= 16:
        raise ValueError("Coaching history scopes must be a bounded list.")
    result = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"kind", "start_date", "end_date"}:
            raise ValueError("Coaching history scope is invalid.")
        first, last = _day(item["start_date"]), _day(item["end_date"])
        start, end = date.fromisoformat(first), date.fromisoformat(last)
        span, kind = (end - start).days, item["kind"]
        valid = kind == "day" and span == 0 or kind == "week" and 0 <= span <= 6
        valid = valid or (
            kind == "month"
            and start.day == 1
            and start.year == end.year
            and start.month == end.month
            and end.day == calendar.monthrange(start.year, start.month)[1]
        )
        valid = valid or kind == "season" and 0 <= span <= 365
        if not valid:
            raise ValueError("Coaching history scope range is invalid.")
        normalized = {"kind": kind, "start_date": first, "end_date": last}
        if normalized in result:
            raise ValueError("Coaching history scopes must be unique.")
        result.append(normalized)
    return sorted(result, key=lambda item: (item["start_date"], item["end_date"], item["kind"]))


def _strings(value: Any, count: int, size: int) -> list[str]:
    if not isinstance(value, list) or len(value) > count:
        raise ValueError("Coaching history list exceeds its limits.")
    return [_text(item, size) for item in value]


def _evidence(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) > 32:
        raise ValueError("Coaching evidence exceeds its limits.")
    result = []
    for item in value:
        if (
            not isinstance(item, dict)
            or not {"kind", "ref"} <= set(item)
            or set(item) - {"kind", "ref", "summary"}
        ):
            raise ValueError("Coaching evidence is invalid.")
        kind, ref = item["kind"], _text(item["ref"], 256)
        if kind in {"daily_summary", "weekly_summary"}:
            ref = _day(ref)
        elif kind == "plan_file":
            if ref not in ALLOWED_PLAN_FILES:
                raise ValueError("Coaching evidence plan file is not supported.")
        elif kind == "activity":
            if not re.fullmatch(r"[A-Za-z0-9_-]+(?::[A-Za-z0-9_-]+)?", ref):
                raise ValueError("Coaching evidence reference is invalid.")
        elif kind in {"coaching_entry", "transaction"}:
            ref = _identifier(ref)
        else:
            raise ValueError("Coaching evidence kind is not supported.")
        normalized = {"kind": kind, "ref": ref}
        if "summary" in item:
            normalized["summary"] = _text(item["summary"], 1024)
        result.append(normalized)
    return result


def _common(value: dict[str, Any], *, require_scopes: bool) -> dict[str, Any]:
    result = {
        "idempotency_key": _identifier(value.get("idempotency_key")),
        "title": _text(value.get("title"), 256),
        "rationale": _text(value.get("rationale"), 4096),
        "scopes": _scopes(value.get("scopes", []), required=require_scopes),
        "conditions": _strings(value.get("conditions", []), 16, 1024),
        "evidence": _evidence(value.get("evidence", [])),
    }
    if "thread_id" in value:
        if not isinstance(value["thread_id"], str) or not _THREAD.fullmatch(value["thread_id"]):
            raise ValueError("Coaching history thread identifier is invalid.")
        result["thread_id"] = value["thread_id"]
    if "tags" in value:
        result["tags"] = _strings(value["tags"], 16, 64)
    return result


def _entry_draft(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - _ENTRY_KEYS or value.get("kind") not in _KINDS:
        raise ValueError("Coaching history entry fields are invalid.")
    result = {
        **_common(value, require_scopes=True),
        "kind": value["kind"],
        "body": _text(value.get("body"), 16384),
        "related_ids": [
            _identifier(item) for item in _strings(value.get("related_ids", []), 32, 128)
        ],
    }
    if "activity_name" in value:
        result["activity_name"] = _text(value["activity_name"], 1024)
    if "id" in value or "expected_revision" in value:
        result["id"] = _identifier(value.get("id"))
        revision = value.get("expected_revision")
        if type(revision) is not int or not 1 <= revision <= MAX_RECORDS:
            raise ValueError("Coaching history expected revision is invalid.")
        result["expected_revision"] = revision
    return result


def _request(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - _REQUEST_KEYS:
        raise ValueError("Plan change request fields are invalid.")
    result = _common(value, require_scopes=False)
    if "decision_id" in value:
        result["decision_id"] = _identifier(value["decision_id"])
    return result


def _encode(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()


def _digest(body: bytes | None) -> str | None:
    return hashlib.sha256(body).hexdigest() if body is not None else None


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Coaching history contains duplicate JSON keys.")
        result[key] = value
    return result


def _reference(value: Any) -> bool:
    return value is None or isinstance(value, str) and _SHA.fullmatch(value) is not None


def _timestamp(value: Any) -> bool:
    try:
        return (
            isinstance(value, str)
            and datetime.fromisoformat(value.replace("Z", "+00:00")).utcoffset() is not None
        )
    except ValueError:
        return False


def _decode(body: bytes | None) -> dict[str, Any]:
    if body is None:
        return _empty()
    try:
        document = json.loads(body, object_pairs_hook=_unique)
        if (
            not isinstance(document, dict)
            or set(document) != {"version", "entries", "transactions"}
            or type(document["version"]) is not int
            or document["version"] != 1
        ):
            raise ValueError
        if any(
            not isinstance(document[key], list) or len(document[key]) > MAX_RECORDS
            for key in ("entries", "transactions")
        ):
            raise ValueError
        entry_ids, entry_keys = {}, set()
        for item in document["entries"]:
            if not isinstance(item, dict) or set(item) != {
                "id",
                "revision",
                "created_at",
                "capture_hash",
                "draft",
            }:
                raise ValueError
            identifier = _identifier(item["id"])
            draft = _entry_draft(item["draft"])
            previous_revision = entry_ids.get(identifier, 0)
            if (
                type(item["revision"]) is not int
                or item["revision"] != previous_revision + 1
                or not _timestamp(item["created_at"])
                or item["capture_hash"] != _digest(_encode(draft))
                or draft["idempotency_key"] in entry_keys
            ):
                raise ValueError
            if (
                previous_revision
                and (
                    draft.get("id") != identifier
                    or draft.get("expected_revision") != previous_revision
                )
                or not previous_revision
                and "id" in draft
            ):
                raise ValueError
            entry_ids[identifier] = item["revision"]
            entry_keys.add(draft["idempotency_key"])
        tx_ids, tx_keys = set(), set()
        for item in document["transactions"]:
            if (
                not isinstance(item, dict)
                or set(item)
                - {
                    "id",
                    "kind",
                    "created_at",
                    "request",
                    "request_hash",
                    "decision",
                    "files",
                    "events",
                }
                or not {"id", "kind", "created_at", "files", "events"} <= set(item)
                or item.get("kind") not in {"baseline", "change"}
                or not _timestamp(item["created_at"])
            ):
                raise ValueError
            identifier = _identifier(item["id"])
            if (
                identifier in tx_ids
                or not isinstance(item.get("files"), dict)
                or set(item["files"]) - ALLOWED_PLAN_FILES
            ):
                raise ValueError
            tx_ids.add(identifier)
            for refs in item["files"].values():
                if (
                    not isinstance(refs, dict)
                    or set(refs) != {"before", "after"}
                    or not all(_reference(ref) for ref in refs.values())
                ):
                    raise ValueError
            events = item.get("events")
            if (
                not isinstance(events, list)
                or not 1 <= len(events) <= MAX_RECORDS
                or any(
                    not isinstance(event, dict)
                    or not {"status", "at"} <= set(event)
                    or set(event) - {"status", "at", "action"}
                    or event["status"] not in _STATES
                    or not _timestamp(event["at"])
                    or (
                        "action" in event
                        and (
                            event["status"] != "recovery_required"
                            or event["action"] not in {"finish", "restore"}
                        )
                    )
                    for event in events
                )
            ):
                raise ValueError
            if any(
                before["status"] not in {"prepared", "recovery_required"}
                or after["status"] == "prepared"
                for before, after in zip(events, events[1:])
            ):
                raise ValueError
            if item["kind"] == "change":
                request = _request(item.get("request"))
                key = request["idempotency_key"]
                expected = _digest(
                    _encode(
                        {
                            "request": request,
                            "updates": {
                                path: refs["after"] for path, refs in item["files"].items()
                            },
                        }
                    )
                )
                if (
                    key in tx_keys
                    or item.get("request_hash") != expected
                    or events[0]["status"] != "prepared"
                ):
                    raise ValueError
                linked = item.get("decision")
                if linked is not None:
                    original = (
                        next(
                            (
                                entry
                                for entry in document["entries"]
                                if entry["id"] == linked.get("id")
                                and entry["revision"] == linked.get("revision")
                            ),
                            None,
                        )
                        if isinstance(linked, dict)
                        else None
                    )
                    if (
                        original is None
                        or original["draft"]["kind"] != "decision"
                        or linked
                        != {
                            "id": original["id"],
                            "revision": original["revision"],
                            "capture_hash": original["capture_hash"],
                            "rationale": original["draft"]["rationale"],
                        }
                        or request.get("decision_id") != original["id"]
                    ):
                        raise ValueError
                elif "decision_id" in request:
                    raise ValueError
                tx_keys.add(key)
            elif events != [{"status": "applied", "at": item["created_at"]}] or any(
                refs["before"] is not None for refs in item["files"].values()
            ):
                raise ValueError
        return document
    except (UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        raise ValueError("Coaching history journal is invalid.") from None


def _private(descriptor: int) -> None:
    if stat.S_IMODE(os.fstat(descriptor).st_mode) & 0o077:
        raise ValueError("Coaching history directories must be owner-private.")


@contextmanager
def _history_dir(root: int, *, create: bool = False):
    if create:
        from .workspace_guidance import _ensure_history_ignore

        _ensure_history_ignore(root)
    with _files._directory(root, "plan", create=create) as plan:
        if create:
            os.fsync(root)
        with _files._directory(plan, ".history", create=create) as directory:
            _private(directory)
            if create:
                os.fsync(plan)
            yield directory


def _load(root: int) -> dict[str, Any]:
    try:
        with _history_dir(root) as directory:
            info = _files._stat(directory, "journal.json", MAX_JOURNAL_BYTES)
            if info is not None and stat.S_IMODE(info.st_mode) & 0o077:
                raise ValueError("Coaching history journal must be owner-private.")
            return _decode(_files._read(directory, "journal.json", MAX_JOURNAL_BYTES))
    except FileNotFoundError:
        return _empty()


def _save(root: int, document: dict[str, Any]) -> None:
    body = _encode(document)
    _decode(body)
    with _history_dir(root, create=True) as directory:
        _files._write(directory, "journal.json", body, MAX_JOURNAL_BYTES)
        os.fsync(directory)


def _empty_portable(data_dir: Path) -> bool:
    path = data_dir / "plan" / ".history" / "journal.json"
    for parent in (data_dir, data_dir / "plan", data_dir / "plan" / ".history"):
        if parent.is_symlink():
            raise ValueError("Could not safely inspect coaching history.")
    try:
        info = path.lstat()
    except FileNotFoundError:
        directory = path.parent
        return not directory.exists() or not any(directory.iterdir())
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_size > MAX_JOURNAL_BYTES
        or (
            hasattr(os, "getuid")
            and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077)
        )
    ):
        raise ValueError("Could not safely inspect coaching history.")
    with path.open("rb") as handle:
        opened = os.fstat(handle.fileno())
        if (info.st_dev, info.st_ino, info.st_size) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
        ):
            raise ValueError("Coaching history changed while opening.")
        result = _decode(handle.read(MAX_JOURNAL_BYTES + 1))
    return (
        not result["entries"]
        and not result["transactions"]
        and {child.name for child in path.parent.iterdir()} == {"journal.json"}
    )


def history_write_available(data_dir: Path) -> bool:
    if _secure_files_supported():
        return True
    if _empty_portable(Path(data_dir)):
        return False
    raise RuntimeError("This platform cannot safely access existing coaching history.")


def _require_supported(data_dir: Path) -> None:
    if not history_write_available(data_dir):
        raise RuntimeError("This platform cannot safely write coaching history.")


def _read_document(data_dir: Path) -> dict[str, Any]:
    data_dir = Path(data_dir)
    if not history_write_available(data_dir):
        return _empty()
    with _files._directory(data_dir) as root:
        document = _load(root)
        _files._assert_generation(data_dir, root, None)
        return document


def _latest(document):
    return {item["id"]: item for item in document["entries"]}


def capture_coaching_entry(
    data_dir: Path, draft: dict, *, expected_identity=None
) -> dict[str, Any]:
    data_dir, normalized = Path(data_dir), _entry_draft(draft)
    _require_supported(data_dir)
    digest = _digest(_encode(normalized))
    identity = expected_identity if expected_identity is not None else workspace_identity(data_dir)
    with workspace_lock(data_dir, expected_identity=identity), _files._directory(data_dir) as root:
        document = _load(root)
        for prior in document["entries"]:
            if prior["draft"]["idempotency_key"] == normalized["idempotency_key"]:
                if prior["capture_hash"] != digest:
                    raise ValueError("Coaching capture idempotency key has different content.")
                return {"id": prior["id"], "revision": prior["revision"], "created": False}
        latest = _latest(document)
        identifier = normalized.get("id") or f"entry-{uuid.uuid4().hex}"
        previous = latest.get(identifier)
        if "id" in normalized and (
            previous is None or previous["revision"] != normalized["expected_revision"]
        ):
            raise ValueError("Coaching entry revision changed; reread and retry.")
        if previous is not None and previous["draft"]["kind"] != normalized["kind"]:
            raise ValueError("Coaching entry kind cannot change across revisions.")
        revision = previous["revision"] + 1 if previous else 1
        document["entries"].append(
            {
                "id": identifier,
                "revision": revision,
                "created_at": _now(),
                "capture_hash": digest,
                "draft": normalized,
            }
        )
        _files._assert_generation(data_dir, root, identity)
        _save(root, document)
        _files._assert_generation(data_dir, root, identity)
        return {"id": identifier, "revision": revision, "created": True}


def recall_coaching_history(
    data_dir: Path, *, start=None, end=None, kind=None, limit=50, include_revisions=False
) -> list[dict[str, Any]]:
    if (
        type(limit) is not int
        or not 1 <= limit <= MAX_RECORDS
        or type(include_revisions) is not bool
        or (kind is not None and kind not in _KINDS)
    ):
        raise ValueError("Coaching recall options are invalid.")
    first, last = _day(start) if start is not None else None, _day(end) if end is not None else None
    if first and last and first > last:
        raise ValueError("Coaching recall date range is invalid.")
    document = _read_document(data_dir)
    entries = document["entries"] if include_revisions else list(_latest(document).values())
    result = []
    for item in entries:
        draft = item["draft"]
        if kind is not None and draft["kind"] != kind:
            continue
        if not any(
            (first is None or scope["end_date"] >= first)
            and (last is None or scope["start_date"] <= last)
            for scope in draft["scopes"]
        ):
            continue
        result.append(
            {
                **{
                    key: value
                    for key, value in draft.items()
                    if key not in {"id", "expected_revision", "idempotency_key"}
                },
                "id": item["id"],
                "revision": item["revision"],
                "created_at": item["created_at"],
            }
        )
    return result[-limit:]


def coaching_entry_by_id(
    data_dir: Path, entry_id: str, *, revision: int | None = None
) -> dict[str, Any] | None:
    identifier = _identifier(entry_id)
    if revision is not None and (type(revision) is not int or not 1 <= revision <= MAX_RECORDS):
        raise ValueError("Coaching history revision is invalid.")
    entries = recall_coaching_history(
        data_dir, limit=MAX_RECORDS, include_revisions=revision is not None
    )
    return next(
        (
            item
            for item in reversed(entries)
            if item["id"] == identifier and (revision is None or item["revision"] == revision)
        ),
        None,
    )


def coaching_history_summary(data_dir: Path) -> dict[str, int]:
    document = _read_document(data_dir)
    latest = _latest(document)
    return {
        "entries": len(latest),
        "revisions": len(document["entries"]),
        **{
            kind: sum(item["draft"]["kind"] == kind for item in latest.values())
            for kind in sorted(_KINDS)
        },
        "transactions": len(document["transactions"]),
        "recovery_required": sum(
            item["events"][-1]["status"] in {"prepared", "recovery_required"}
            for item in document["transactions"]
        ),
    }


def _read_target(root: int, path: str) -> bytes | None:
    if path not in ALLOWED_PLAN_FILES:
        raise ValueError("Plan change path is not allowed.")
    parts = path.split("/")
    try:
        with _files._directory(root, *parts[:-1]) as directory:
            return _files._read(directory, parts[-1], MAX_FILE_BYTES)
    except FileNotFoundError:
        return None


def _write_target(root: int, path: str, body: bytes | None) -> None:
    if path not in ALLOWED_PLAN_FILES:
        raise ValueError("Plan change path is not allowed.")
    parts = path.split("/")
    with _files._directory(root, *parts[:-1], create=body is not None) as directory:
        if body is None:
            if _files._stat(directory, parts[-1], MAX_FILE_BYTES) is not None:
                os.unlink(parts[-1], dir_fd=directory)
        else:
            _files._write(directory, parts[-1], body, MAX_FILE_BYTES)
        os.fsync(directory)


def _snapshot(root: int, body: bytes | None) -> str | None:
    digest = _digest(body)
    if digest is None:
        return None
    with (
        _history_dir(root, create=True) as history,
        _files._directory(history, "objects", create=True) as objects,
    ):
        _private(objects)
        os.fsync(history)
        info = _files._stat(objects, digest, MAX_FILE_BYTES)
        if info is not None and stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("Coaching history snapshot must be owner-private.")
        existing = _files._read(objects, digest, MAX_FILE_BYTES)
        if existing is None:
            _files._write(objects, digest, body, MAX_FILE_BYTES)
            os.fsync(objects)
        elif existing != body:
            raise ValueError("Coaching history snapshot integrity check failed.")
    return digest


def _snapshot_body(root: int, digest: str | None) -> bytes | None:
    if digest is None:
        return None
    if not _reference(digest):
        raise ValueError("Coaching history snapshot reference is invalid.")
    with _history_dir(root) as history, _files._directory(history, "objects") as objects:
        _private(objects)
        info = _files._stat(objects, digest, MAX_FILE_BYTES)
        if info is None or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("Coaching history snapshot is unavailable or not private.")
        body = _files._read(objects, digest, MAX_FILE_BYTES)
    if _digest(body) != digest:
        raise ValueError("Coaching history snapshot integrity check failed.")
    return body


def _status(transaction) -> str:
    return transaction["events"][-1]["status"]


def _classify(root: int, transaction: dict) -> str:
    matches_before = matches_after = True
    for path, refs in transaction["files"].items():
        for digest in refs.values():
            _snapshot_body(root, digest)
        current = _digest(_read_target(root, path))
        matches_before = matches_before and current == refs["before"]
        matches_after = matches_after and current == refs["after"]
    action = next(
        (event["action"] for event in reversed(transaction["events"]) if "action" in event), None
    )
    if action == "restore":
        return "restored" if matches_before else "recovery_required"
    if action == "finish":
        return "applied" if matches_after else "recovery_required"
    return "applied" if matches_after else "failed" if matches_before else "recovery_required"


def _event(transaction: dict, status: str) -> None:
    if _status(transaction) != status:
        transaction["events"].append({"status": status, "at": _now()})


def _reconcile(root: int, document: dict) -> bool:
    changed = False
    for transaction in document["transactions"]:
        if _status(transaction) not in {"prepared", "recovery_required"}:
            continue
        status = _classify(root, transaction)
        if status != _status(transaction):
            _event(transaction, status)
            changed = True
    return changed


def reconcile_plan_history(data_dir: Path, *, expected_identity=None) -> dict[str, int]:
    data_dir = Path(data_dir)
    _require_supported(data_dir)
    identity = expected_identity if expected_identity is not None else workspace_identity(data_dir)
    with workspace_lock(data_dir, expected_identity=identity), _files._directory(data_dir) as root:
        document = _load(root)
        changed = _reconcile(root, document)
        _files._assert_generation(data_dir, root, identity)
        if changed:
            _save(root, document)
        _files._assert_generation(data_dir, root, identity)
        return {
            status: sum(_status(item) == status for item in document["transactions"])
            for status in sorted(_STATES)
        }


def _result(transaction, *, created):
    return {
        "id": transaction["id"],
        "status": _status(transaction),
        "created": created,
        "changed_files": sum(
            refs["before"] != refs["after"] for refs in transaction["files"].values()
        ),
    }


def apply_plan_change(
    data_dir: Path,
    *,
    updates: Mapping[str, bytes | None],
    request: dict,
    expected_identity=None,
    expected_hashes=None,
) -> dict[str, Any]:
    data_dir, normalized = Path(data_dir), _request(request)
    if (
        not isinstance(updates, Mapping)
        or not updates
        or set(updates) - ALLOWED_PLAN_FILES
        or any(
            body is not None and (not isinstance(body, bytes) or len(body) > MAX_FILE_BYTES)
            for body in updates.values()
        )
        or sum(len(body) for body in updates.values() if body is not None) > MAX_CHANGE_BYTES
    ):
        raise ValueError("Plan change files or size are invalid.")
    if expected_hashes is not None and (
        not isinstance(expected_hashes, Mapping)
        or set(expected_hashes) != set(updates)
        or not all(_reference(value) for value in expected_hashes.values())
    ):
        raise ValueError("Expected plan hashes must cover exactly the changed files.")
    _require_supported(data_dir)
    updates = dict(sorted(updates.items()))
    request_hash = _digest(
        _encode(
            {
                "request": normalized,
                "updates": {path: _digest(body) for path, body in updates.items()},
            }
        )
    )
    identity = expected_identity if expected_identity is not None else workspace_identity(data_dir)
    with workspace_lock(data_dir, expected_identity=identity), _files._directory(data_dir) as root:
        document = _load(root)
        if _reconcile(root, document):
            _files._assert_generation(data_dir, root, identity)
            _save(root, document)
        if any(
            _status(item) in {"prepared", "recovery_required"} for item in document["transactions"]
        ):
            raise RuntimeError("Plan history requires recovery before another plan change.")
        for prior in document["transactions"]:
            if prior.get("request", {}).get("idempotency_key") == normalized["idempotency_key"]:
                if prior.get("request_hash") != request_hash:
                    raise ValueError("Plan change idempotency key has different content.")
                if expected_hashes is not None and any(
                    expected_hashes[path] != prior["files"][path]["before"] for path in updates
                ):
                    raise ValueError("Plan change retry has different expected file hashes.")
                return _result(prior, created=False)
        decision = None
        if "decision_id" in normalized:
            decision = _latest(document).get(normalized["decision_id"])
            if decision is None or decision["draft"]["kind"] != "decision":
                raise ValueError("Plan change must reference an existing coaching decision.")
            decision = {
                "id": decision["id"],
                "revision": decision["revision"],
                "capture_hash": decision["capture_hash"],
                "rationale": decision["draft"]["rationale"],
            }
        before = {path: _read_target(root, path) for path in updates}
        if sum(len(body) for body in before.values() if body is not None) > MAX_CHANGE_BYTES:
            raise ValueError("Plan change snapshots exceed their size limit.")
        if expected_hashes is not None and any(
            _digest(before[path]) != expected_hashes[path] for path in updates
        ):
            raise ValueError("Expected plan file changed; reread and retry.")
        _files._assert_generation(data_dir, root, identity)
        if all(before[path] == body for path, body in updates.items()):
            return {"id": None, "status": "unchanged", "created": False, "changed_files": 0}
        refs = {
            path: {"before": _snapshot(root, before[path]), "after": _snapshot(root, body)}
            for path, body in updates.items()
        }
        transaction = {
            "id": f"change-{uuid.uuid4().hex}",
            "kind": "change",
            "created_at": _now(),
            "request": normalized,
            "request_hash": request_hash,
            "decision": decision,
            "files": refs,
            "events": [{"status": "prepared", "at": _now()}],
        }
        document["transactions"].append(transaction)
        _files._assert_generation(data_dir, root, identity)
        _save(root, document)
        try:
            for path, body in updates.items():
                _files._assert_generation(data_dir, root, identity)
                if _digest(_read_target(root, path)) != refs[path]["before"]:
                    raise ValueError("Plan file changed during its transaction.")
                if refs[path]["before"] != refs[path]["after"]:
                    _write_target(root, path, body)
            _files._assert_generation(data_dir, root, identity)
            status = _classify(root, transaction)
            if status != "applied":
                raise ValueError("Plan change verification failed.")
            _event(transaction, status)
            _save(root, document)
            _files._assert_generation(data_dir, root, identity)
        except (OSError, ValueError, RuntimeError):
            _files._assert_generation(data_dir, root, identity)
            try:
                _event(transaction, _classify(root, transaction))
                _save(root, document)
            except (OSError, ValueError, RuntimeError):
                pass
            raise RuntimeError(
                "Plan change did not complete cleanly; inspect plan history recovery."
            ) from None
        return _result(transaction, created=True)


def initialize_plan_history(data_dir: Path, *, expected_identity=None) -> dict[str, Any]:
    data_dir = Path(data_dir)
    _require_supported(data_dir)
    identity = expected_identity if expected_identity is not None else workspace_identity(data_dir)
    with workspace_lock(data_dir, expected_identity=identity), _files._directory(data_dir) as root:
        document = _load(root)
        for item in document["transactions"]:
            if item["kind"] == "baseline":
                return {"id": item["id"], "created": False, "file_count": len(item["files"])}
        if any(
            _status(item) in {"prepared", "recovery_required"} for item in document["transactions"]
        ):
            raise RuntimeError("Resolve unresolved plan changes before initializing a baseline.")
        bodies = {path: _read_target(root, path) for path in sorted(ALLOWED_PLAN_FILES)}
        bodies = {path: body for path, body in bodies.items() if body is not None}
        if sum(map(len, bodies.values())) > MAX_CHANGE_BYTES:
            raise ValueError("Plan baseline exceeds its size limit.")
        refs = {
            path: {"before": None, "after": _snapshot(root, body)} for path, body in bodies.items()
        }
        now = _now()
        item = {
            "id": f"baseline-{uuid.uuid4().hex}",
            "kind": "baseline",
            "created_at": now,
            "files": refs,
            "events": [{"status": "applied", "at": now}],
        }
        document["transactions"].append(item)
        _files._assert_generation(data_dir, root, identity)
        _save(root, document)
        _files._assert_generation(data_dir, root, identity)
        return {"id": item["id"], "created": True, "file_count": len(refs)}


def recover_plan_change(
    data_dir: Path, transaction_id: str, *, action: str, expected_identity=None
) -> dict[str, Any]:
    """Explicitly finish or restore known bytes; refuse unrelated file changes."""
    data_dir, identifier = Path(data_dir), _identifier(transaction_id)
    if action not in {"finish", "restore"}:
        raise ValueError("Plan recovery action must be finish or restore.")
    _require_supported(data_dir)
    identity = expected_identity if expected_identity is not None else workspace_identity(data_dir)
    with workspace_lock(data_dir, expected_identity=identity), _files._directory(data_dir) as root:
        document = _load(root)
        transaction = next(
            (item for item in document["transactions"] if item["id"] == identifier), None
        )
        if (
            transaction is None
            or transaction["kind"] != "change"
            or _status(transaction) not in {"prepared", "recovery_required"}
        ):
            raise ValueError("Plan recovery requires an unresolved plan change.")
        if any(
            item["id"] != identifier and _status(item) in {"prepared", "recovery_required"}
            for item in document["transactions"]
        ):
            raise RuntimeError("Another plan change requires recovery first.")
        side = "after" if action == "finish" else "before"
        desired = {
            path: _snapshot_body(root, refs[side]) for path, refs in transaction["files"].items()
        }
        observed = {path: _digest(_read_target(root, path)) for path in desired}
        if any(
            observed[path] not in (refs["before"], refs["after"])
            for path, refs in transaction["files"].items()
        ):
            raise RuntimeError("Plan recovery refused because an affected file diverged.")
        transaction["events"].append(
            {"status": "recovery_required", "action": action, "at": _now()}
        )
        _files._assert_generation(data_dir, root, identity)
        _save(root, document)
        try:
            for path, body in desired.items():
                _files._assert_generation(data_dir, root, identity)
                if _digest(_read_target(root, path)) != observed[path]:
                    raise RuntimeError("Plan recovery refused because an affected file diverged.")
                if observed[path] != _digest(body):
                    _write_target(root, path, body)
            _files._assert_generation(data_dir, root, identity)
            if any(
                _digest(_read_target(root, path)) != _digest(body) for path, body in desired.items()
            ):
                raise RuntimeError("Plan recovery verification failed.")
            _event(transaction, "applied" if action == "finish" else "restored")
            _save(root, document)
            _files._assert_generation(data_dir, root, identity)
        except (OSError, ValueError):
            raise RuntimeError("Plan recovery did not complete; inspect plan history.") from None
        return _result(transaction, created=False)


def _transaction_view(
    root: int, item: dict, *, details: bool, detail_budget: list[int] | None = None
) -> dict[str, Any]:
    value = {
        **item,
        "status": _status(item),
        "files": {path: dict(refs) for path, refs in item["files"].items()},
    }
    if details:
        if detail_budget is None:
            detail_budget = [MAX_HISTORY_DETAIL_BYTES]
        for refs in value["files"].values():
            for side in ("before", "after"):
                body = _snapshot_body(root, refs[side])
                detail_budget[0] -= len(body) if body is not None else 0
                if detail_budget[0] < 0:
                    raise ValueError("Plan history detail exceeds its size limit.")
                try:
                    refs[f"{side}_content"] = None if body is None else body.decode("utf-8")
                except UnicodeError:
                    raise ValueError("Plan snapshot is not supported UTF-8 text.") from None
    return value


def plan_change_details(data_dir: Path, transaction_id: str) -> dict[str, Any]:
    data_dir, identifier = Path(data_dir), _identifier(transaction_id)
    if not history_write_available(data_dir):
        raise ValueError("Plan change was not found.")
    with _files._directory(data_dir) as root:
        item = next(
            (item for item in _load(root)["transactions"] if item["id"] == identifier), None
        )
        if item is None:
            raise ValueError("Plan change was not found.")
        result = _transaction_view(root, item, details=True)
        _files._assert_generation(data_dir, root, None)
        return result


def plan_change_by_key(data_dir: Path, idempotency_key: str) -> dict[str, Any] | None:
    """Find compact immutable transaction metadata without opening snapshots."""
    key = _identifier(idempotency_key)
    document = _read_document(Path(data_dir))
    item = next(
        (
            item
            for item in document["transactions"]
            if item.get("request", {}).get("idempotency_key") == key
        ),
        None,
    )
    return None if item is None else _transaction_view(-1, item, details=False)


def _drift_heads(document: dict[str, Any]) -> tuple[bool, dict[str, str | None], set[str]]:
    baseline = False
    heads: dict[str, str | None] = {}
    excluded: set[str] = set()
    for item in document["transactions"]:
        status = _status(item)
        if status in {"prepared", "recovery_required"}:
            excluded.update(item["files"])
            continue
        if item["kind"] == "baseline":
            # A baseline inspected the entire allowlist: omitted files were
            # genuinely absent at that point, unlike untracked older history.
            baseline = True
            heads = dict.fromkeys(ALLOWED_PLAN_FILES)
        if status == "applied":
            heads.update({path: refs["after"] for path, refs in item["files"].items()})
        elif status == "restored":
            heads.update({path: refs["before"] for path, refs in item["files"].items()})
    return baseline, heads, excluded


def plan_history_drift(data_dir: Path) -> dict[str, Any]:
    """Compare known official file heads without inventing or capturing an edit."""
    data_dir = Path(data_dir)
    if not history_write_available(data_dir):
        document, current = _empty(), {}
        baseline, heads, excluded = _drift_heads(document)
    else:
        with _files._directory(data_dir) as root:
            for _ in range(3):
                document = _load(root)
                baseline, heads, excluded = _drift_heads(document)
                current = {
                    path: _digest(_read_target(root, path))
                    for path in sorted(heads.keys() - excluded)
                }
                if document == _load(root):
                    break
            else:
                raise RuntimeError("Plan history changed during drift inspection; retry.")
            _files._assert_generation(data_dir, root, None)
    drifted = sorted(path for path, digest in current.items() if digest != heads[path])
    return {
        "baseline_present": baseline,
        "checked_files": len(current),
        "drifted_files": drifted,
        "drifted_count": len(drifted),
        "unknown_files": sorted(ALLOWED_PLAN_FILES - heads.keys() - excluded),
        "excluded_unresolved_files": sorted(excluded),
    }


def plan_history(data_dir: Path, *, limit=50, details=False) -> list[dict[str, Any]]:
    if type(limit) is not int or not 1 <= limit <= MAX_RECORDS or type(details) is not bool:
        raise ValueError("Plan history options are invalid.")
    data_dir = Path(data_dir)
    if not history_write_available(data_dir):
        return []
    with _files._directory(data_dir) as root:
        document = _load(root)
        budget = [MAX_HISTORY_DETAIL_BYTES]
        result = [
            _transaction_view(root, item, details=details, detail_budget=budget)
            for item in document["transactions"][-limit:]
        ]
        _files._assert_generation(data_dir, root, None)
        return result
