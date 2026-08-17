"""Private, credential-free calendar and device-workout downloads."""

from __future__ import annotations

import hashlib
import html
import io
import json
import os
import re
import secrets
import stat
import zipfile
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterator

from .config import ensure_private_data_dir, ensure_private_output_path
from .fit_workout import encode_workout_fit
from .planned_workouts import load_planned_workouts, serialize_plan_csv, serialize_plan_ics
from .storage import ensure_text_line
from .workspace_lock import workspace_identity, workspace_lock


MAX_EXPORT_BYTES = 64 * 1024 * 1024
MAX_WORKOUT_FILES = 1_000
_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,79}\Z")
_FORMATS = {
    "zip": "application/zip",
    "ics": "text/calendar; charset=utf-8",
    "csv": "text/csv; charset=utf-8",
    "fit": "application/octet-stream",
}
_README = """Gradient Ascent planned-workout export

Open index.html for a readable, offline schedule. Import schedule.ics into a
calendar, or open schedule.csv in a spreadsheet. The bundle contains planned
sessions and events only; tentative/cancelled events retain their status. It does
not include completed rides, GPS tracks, health data,
provider credentials, or the athlete profile.

Files in workouts/ are genuine FIT Workout instruction files. Only explicitly
defined structured workouts receive one. Calendar-only descriptions are never
converted into guessed durations, zones, or power targets. The planned date is
in the calendar and filenames: copying a FIT Workout does not automatically
schedule a device calendar.

Compatible Garmin devices: follow the device's workout-transfer instructions.
https://support.garmin.com/en-MY/?faq=rzvP53Si4O3barYoXzw5L7

Wahoo ACE, BOLT 3, and ROAM 3: enable USB access and copy workout FIT files into
the plans folder. Other models may require a supported planned-workout provider.
https://support.wahoofitness.com/hc/en-us/articles/28544587410450

Review this bundle before sharing or uploading it. Gradient Ascent does not
automatically publish it or connect to Garmin/Wahoo cloud accounts.
"""


@dataclass(frozen=True)
class PlanExport:
    filename: str
    content_type: str
    body: bytes
    summary: dict[str, Any]


def parse_export_date(value: str | date | None) -> date | None:
    if value is None:
        return None
    if type(value) is date:
        return value
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("Export dates must use YYYY-MM-DD.")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError("Export dates must use YYYY-MM-DD.") from None


def _fit_name(entry: Mapping[str, Any]) -> str:
    identifier = entry.get("id")
    if not isinstance(identifier, str) or _ID.fullmatch(identifier) is None:
        raise ValueError("Workout export requires a safe workout identifier.")
    day = parse_export_date(entry.get("date"))
    if day is None:
        raise ValueError("Workout export requires a calendar date.")
    return f"{day.isoformat()}-{identifier}.fit"


def _index_html(entries: list[dict[str, Any]], fit_names: dict[str, str]) -> bytes:
    rows = []
    for entry in entries:
        name = html.escape(entry["name"])
        description = html.escape(entry.get("description", ""))
        day = html.escape(entry["date"])
        status = entry.get("status", "confirmed")
        if status not in {"confirmed", "tentative", "cancelled"}:
            raise ValueError("Invalid planned calendar status.")
        context = [status.capitalize()]
        if entry.get("location"):
            context.append(entry["location"])
        if entry.get("priority"):
            context.append(f"Priority {entry['priority']}")
        fit_name = fit_names.get(entry["id"])
        if fit_name:
            action = f'<a download href="workouts/{html.escape(fit_name, quote=True)}">Download FIT workout</a>'
            detail = f"{sum(step['duration_s'] for step in entry['steps']) // 60} min · {len(entry['steps'])} steps"
        else:
            action = '<span class="muted">calendar-only</span>'
            detail = "Original plan description"
        rows.append(
            f'<article class="{status}"><div class="date">{day}</div><h2>{name}</h2>'
            f'<p class="muted">{html.escape(" · ".join(context))}</p>'
            f'<p class="muted">{html.escape(detail)}</p>'
            f'<p class="description">{description}</p><p>{action}</p></article>'
        )
    document = (
        """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>Gradient Ascent · Planned schedule</title><style>
:root{color-scheme:light dark;font:16px/1.5 system-ui,sans-serif;background:#f5f4ef;color:#24362d}body{max-width:850px;margin:auto;padding:32px 20px}h1{font-size:2rem;line-height:1.15}h2{font-size:1.1rem;margin:.35rem 0}a{color:#256749;font-weight:650}header{margin-bottom:28px}.downloads{display:flex;gap:18px;flex-wrap:wrap}article{background:#fff;border:1px solid #dddcd4;border-radius:12px;padding:20px;margin:14px 0}.cancelled h2{text-decoration:line-through}.date{font-size:.8rem;font-weight:750;letter-spacing:.04em}.muted{color:#68746b}.description{white-space:pre-wrap;overflow-wrap:anywhere}@media(prefers-color-scheme:dark){:root{background:#17231c;color:#e8eee9}article{background:#203128;border-color:#3b5143}a{color:#98d9ad}.muted{color:#b0beb3}}
</style></head><body><header><p>GRADIENT ASCENT</p><h1>Planned schedule</h1>
<p>Calendar entries preserve the plan. Device files contain only explicitly prescribed workout steps.</p>
<nav class="downloads"><a download href="schedule.ics">Download calendar</a><a download href="schedule.csv">Download CSV</a><a href="README.txt">Device instructions</a></nav></header>
"""
        + "\n".join(rows)
        + "\n</body></html>\n"
    )
    return document.encode("utf-8")


def _zip_bundle(entries: list[dict[str, Any]]) -> tuple[bytes, int]:
    structured = [entry for entry in entries if entry["structured"]]
    if len(structured) > MAX_WORKOUT_FILES:
        raise ValueError("Choose a smaller date range; too many device workouts were selected.")
    fit_names = {entry["id"]: _fit_name(entry) for entry in structured}
    manifest = {
        "version": 1,
        "kind": "gradient-ascent-planned-workouts",
        "device_calendar_scheduled": False,
        "entries": [
            {
                "id": entry["id"],
                "date": entry["date"],
                "name": entry["name"],
                "source": entry["source"],
                "structured": entry["structured"],
                "status": entry.get("status", "confirmed"),
                "location": entry.get("location", ""),
                "priority": entry.get("priority", ""),
                "duration_s": sum(step["duration_s"] for step in entry["steps"])
                if entry["structured"]
                else None,
                "fit_file": f"workouts/{fit_names[entry['id']]}" if entry["structured"] else None,
            }
            for entry in entries
        ],
    }
    files = [
        ("index.html", _index_html(entries, fit_names)),
        ("schedule.ics", serialize_plan_ics(entries)),
        ("schedule.csv", serialize_plan_csv(entries)),
        (
            "manifest.json",
            (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
        ),
        ("README.txt", _README.encode()),
    ]
    files.extend(
        (f"workouts/{fit_names[entry['id']]}", encode_workout_fit(entry)) for entry in structured
    )
    if sum(len(body) for _, body in files) > MAX_EXPORT_BYTES:
        raise ValueError("Choose a smaller date range; the export exceeds its size limit.")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, body in files:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            archive.writestr(info, body)
    return output.getvalue(), len(structured)


def build_plan_export(
    data_dir: Path,
    *,
    format: str = "zip",
    start: str | date | None = None,
    end: str | date | None = None,
    workout_id: str | None = None,
    expected_identity: tuple[int, int] | None = None,
) -> PlanExport:
    """Build an in-memory, plan-only download under the workspace snapshot lock."""
    data_dir = ensure_private_data_dir(data_dir, action="export a planned schedule")
    if not isinstance(format, str) or format not in _FORMATS:
        raise ValueError("Choose zip, ics, csv, or fit for the plan export.")
    if workout_id is not None and (
        not isinstance(workout_id, str) or _ID.fullmatch(workout_id) is None
    ):
        raise ValueError("Choose a valid planned workout identifier.")
    if format == "fit" and workout_id is None:
        raise ValueError("Choose one structured workout with --workout for FIT export.")
    first, last = parse_export_date(start), parse_export_date(end)
    if first and last and first > last:
        raise ValueError("The export start date must not be after its end date.")
    identity = expected_identity if expected_identity is not None else workspace_identity(data_dir)
    with workspace_lock(data_dir, expected_identity=identity):
        entries = load_planned_workouts(data_dir, start=first, end=last)
        if workout_id is not None:
            entries = [entry for entry in entries if entry["id"] == workout_id]
        if not entries:
            raise ValueError("No planned workouts match this selection.")
        range_start = (first or date.fromisoformat(entries[0]["date"])).isoformat()
        range_end = (last or date.fromisoformat(entries[-1]["date"])).isoformat()
        filename = f"gradient-ascent-plan-{range_start}-to-{range_end}.{format}"
        fit_files = 0
        if format == "fit":
            if len(entries) != 1 or not entries[0]["structured"]:
                raise ValueError("This plan entry has no explicit device-workout steps.")
            body = encode_workout_fit(entries[0])
            filename = _fit_name(entries[0])
            fit_files = 1
        elif format == "ics":
            body = serialize_plan_ics(entries)
        elif format == "csv":
            body = serialize_plan_csv(entries)
        else:
            body, fit_files = _zip_bundle(entries)
        if len(body) > MAX_EXPORT_BYTES:
            raise ValueError("Choose a smaller date range; the export exceeds its size limit.")
        with workspace_lock(data_dir, expected_identity=identity):
            pass
    structured_count = sum(entry["structured"] for entry in entries)
    return PlanExport(
        filename,
        _FORMATS[format],
        body,
        {
            "format": format,
            "entries": len(entries),
            "structured_workouts": structured_count,
            "calendar_only": len(entries) - structured_count,
            "fit_files": fit_files,
            "bytes": len(body),
            "start_date": range_start,
            "end_date": range_end,
            "external_access": False,
        },
    )


def _directory_owner(metadata: os.stat_result, *, final: bool = False) -> None:
    owner = getattr(os, "getuid", lambda: metadata.st_uid)()
    sticky_root = metadata.st_uid == 0 and bool(metadata.st_mode & stat.S_ISVTX)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid not in {0, owner}
        or (metadata.st_mode & 0o022 and not sticky_root)
        or (final and (metadata.st_uid != owner or metadata.st_mode & 0o022))
    ):
        raise ValueError("Export directories must be controlled by the current user.")


@contextmanager
def _output_parent(path: Path) -> Iterator[int]:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(path.anchor, flags)
    try:
        _directory_owner(os.fstat(descriptor))
        for part in path.parent.parts[1:]:
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                os.mkdir(part, 0o700, dir_fd=descriptor)
                child = os.open(part, flags, dir_fd=descriptor)
            try:
                _directory_owner(os.fstat(child))
            except BaseException:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
        _directory_owner(os.fstat(descriptor), final=True)
        yield descriptor
    finally:
        os.close(descriptor)


def _existing_export(directory: int, filename: str, expected_body: bytes) -> bool | None:
    try:
        before = os.stat(filename, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_uid != os.getuid()
        or before.st_mode & 0o077
    ):
        raise ValueError("An existing export must be an owner-private regular file.")
    if before.st_size != len(expected_body):
        return False
    descriptor = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
    with os.fdopen(descriptor, "rb") as handle:
        current = os.fstat(handle.fileno())
        if (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino):
            raise ValueError("The export changed while opening.")
        return (
            hashlib.file_digest(handle, "sha256").digest() == hashlib.sha256(expected_body).digest()
        )


def write_plan_export(
    data_dir: Path,
    *,
    output_path: Path | None = None,
    overwrite: bool = False,
    expected_identity: tuple[int, int] | None = None,
    **options: Any,
) -> dict[str, Any]:
    """Atomically save an owner-private export; existing different files need consent."""
    if type(overwrite) is not bool:
        raise ValueError("Overwriting an export requires explicit consent.")
    data_dir = ensure_private_data_dir(data_dir, action="export a planned schedule").resolve()
    identity = expected_identity if expected_identity is not None else workspace_identity(data_dir)
    with workspace_lock(data_dir, expected_identity=identity):
        artifact = build_plan_export(data_dir, expected_identity=identity, **options)
        # The root can be manually moved/replaced even while a sanctioned lock is
        # held. Revalidate after the potentially long build, before setup writes.
        with workspace_lock(data_dir, expected_identity=identity):
            pass
        if output_path is None:
            path = data_dir / "exports" / "planned" / artifact.filename
        else:
            original = Path(output_path).expanduser().absolute()
            if ".." in original.parts or original.is_symlink():
                raise ValueError("Choose a regular export destination.")
            # Resolve a deliberately selected parent (including system /tmp aliases),
            # then pin the canonical directory chain without following later links.
            path = original.parent.resolve() / original.name
        if not path.name or path.name in {".", ".."} or any(ord(char) < 32 for char in path.name):
            raise ValueError("Choose a valid export filename.")
        ensure_private_output_path(path, action="write a planned schedule export")
        with workspace_lock(data_dir, expected_identity=identity):
            ensure_text_line(data_dir / ".gitignore", "exports/")
        with _output_parent(path) as directory:
            existing = _existing_export(directory, path.name, artifact.body)
            if existing is True:
                return {**artifact.summary, "path": str(path), "written": False}
            if existing is False and not overwrite:
                raise FileExistsError(
                    "The export already exists; choose another path or --overwrite."
                )
            temporary = f".gradient-ascent-export-{secrets.token_hex(12)}.tmp"
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(artifact.body)
                    handle.flush()
                    os.fsync(handle.fileno())
                with workspace_lock(data_dir, expected_identity=identity):
                    if overwrite:
                        _existing_export(directory, path.name, artifact.body)
                        os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
                    else:
                        os.link(
                            temporary,
                            path.name,
                            src_dir_fd=directory,
                            dst_dir_fd=directory,
                            follow_symlinks=False,
                        )
                        os.unlink(temporary, dir_fd=directory)
                os.fsync(directory)
            finally:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass
        return {**artifact.summary, "path": str(path), "written": True}
