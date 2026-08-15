from __future__ import annotations

import atexit
import csv
import gzip
import io
import os
import re
import tempfile
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterator, TextIO
from zipfile import ZipFile


from .activity_files import parse_activity_recording, recording_format
from .storage import read_json, write_json

STRAVA_BULK_EXPORT_SOURCE = "strava_bulk_export"
MAX_ARCHIVE_RECORDING_BYTES = 512 * 1024 * 1024
RECORDING_SPOOL_MEMORY_BYTES = 16 * 1024 * 1024
MAX_RECORDING_WORKERS = 8
MIN_PARALLEL_RECORDINGS = 4
_worker_export_source: "_StravaExportSource | None" = None


def _state_path(data_dir: Path) -> Path:
    return data_dir / "strava" / "state.json"


def _activities_path(data_dir: Path) -> Path:
    return data_dir / "strava" / "activities.json"


def _streams_dir(data_dir: Path) -> Path:
    return data_dir / "strava" / "streams"


def _laps_dir(data_dir: Path) -> Path:
    return data_dir / "strava" / "laps"


def _activity_index(payload: Any) -> Dict[str, Dict[str, Any]]:
    if isinstance(payload, dict):
        return {str(key): value for key, value in payload.items() if isinstance(value, dict)}
    if isinstance(payload, list):
        activities: Dict[str, Dict[str, Any]] = {}
        for value in payload:
            if not isinstance(value, dict):
                continue
            activity_id = value.get("id")
            if activity_id is None:
                continue
            activities[str(activity_id)] = value
        return activities
    return {}


def _normalize_export_field_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def _normalized_export_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        _normalize_export_field_name(key): value.strip() if isinstance(value, str) else value
        for key, value in row.items()
        if isinstance(key, str)
    }


def _first_export_value(row: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def _safe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _activity_id(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if re.fullmatch(r"\d+\.0+", text):
        return text.split(".", 1)[0]
    return text or None


def _activity_id_value(activity_id: str) -> int | str:
    return int(activity_id) if activity_id.isdigit() else activity_id


def _export_datetime(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is None:
        for date_format in (
            "%b %d, %Y, %I:%M:%S %p",
            "%B %d, %Y, %I:%M:%S %p",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S",
        ):
            try:
                parsed = datetime.strptime(text, date_format)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    rendered = parsed.isoformat(timespec="seconds")
    return rendered.replace("+00:00", "Z")


def _distance_m(row: Dict[str, Any]) -> float | None:
    meters = _safe_float(
        _first_export_value(
            row,
            "distance",
            "distance_m",
            "distance_meters",
            "distance_metres",
        )
    )
    if meters is not None:
        return meters
    kilometers = _safe_float(
        _first_export_value(row, "distance_km", "distance_kilometers")
    )
    # Official Strava archive CSVs use meters for the unqualified Distance column.
    # Convert only columns whose names explicitly declare kilometers.
    return kilometers * 1000.0 if kilometers is not None else None


def _archive_activity(row: Dict[str, Any]) -> tuple[str, Dict[str, Any]] | None:
    activity_id = _activity_id(
        _first_export_value(row, "activity_id", "id")
    )
    if not activity_id:
        return None
    sport_type = _first_export_value(row, "activity_type", "sport_type", "type")
    start_date = _export_datetime(
        _first_export_value(row, "activity_date", "start_date_local", "start_date")
    )
    activity = {
        "id": _activity_id_value(activity_id),
        "name": _first_export_value(row, "activity_name", "name"),
        "sport_type": sport_type,
        "type": sport_type,
        "start_date": start_date,
        "start_date_local": start_date,
        "elapsed_time": _safe_float(_first_export_value(row, "elapsed_time")),
        "moving_time": _safe_float(_first_export_value(row, "moving_time")),
        "distance": _distance_m(row),
        "total_elevation_gain": _safe_float(
            _first_export_value(row, "elevation_gain", "total_elevation_gain")
        ),
        "average_heartrate": _safe_float(
            _first_export_value(row, "average_heart_rate", "average_heartrate")
        ),
        "max_heartrate": _safe_float(
            _first_export_value(row, "max_heart_rate", "max_heartrate")
        ),
        "average_watts": _safe_float(
            _first_export_value(row, "average_watts", "average_power")
        ),
        "weighted_average_watts": _safe_float(
            _first_export_value(
                row,
                "weighted_average_power",
                "weighted_average_watts",
            )
        ),
        "kilojoules": _safe_float(_first_export_value(row, "kilojoules")),
        "source_archive_file": _first_export_value(row, "filename"),
        "import_source": STRAVA_BULK_EXPORT_SOURCE,
    }
    return activity_id, {
        key: value
        for key, value in activity.items()
        if value not in (None, "")
    }


def _safe_archive_name(value: str) -> str | None:
    normalized = str(value or "").replace("\\", "/").strip()
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts:
        return None
    parts = tuple(part for part in path.parts if part not in ("", "."))
    return PurePosixPath(*parts).as_posix() if parts else None


class _StravaExportSource:
    def __init__(self, export_path: Path):
        self.export_path = export_path
        self.archive: ZipFile | None = None
        self.root: Path | None = None
        self.activities_csv_path: Path | None = None
        self.activities_csv_name = ""
        self._zip_names: dict[str, str] = {}

    def __enter__(self) -> "_StravaExportSource":
        if self.export_path.is_file() and self.export_path.name.lower() == "activities.csv":
            self.root = self.export_path.parent.resolve()
            self.activities_csv_path = self.export_path.resolve()
            self.activities_csv_name = self.export_path.name
            return self
        if self.export_path.is_dir():
            self.root = self.export_path.resolve()
            candidates = sorted(
                (
                    path
                    for path in self.export_path.rglob("*")
                    if path.is_file() and path.name.lower() == "activities.csv"
                ),
                key=lambda path: (len(path.parts), str(path)),
            )
            if candidates:
                self.activities_csv_path = candidates[0].resolve()
                self.activities_csv_name = self.activities_csv_path.relative_to(self.root).as_posix()
                return self
        elif self.export_path.is_file():
            self.archive = ZipFile(self.export_path)
            self._zip_names = {
                safe_name: info.filename
                for info in self.archive.infolist()
                if not info.is_dir() and (safe_name := _safe_archive_name(info.filename))
            }
            names = sorted(
                (name for name in self._zip_names if PurePosixPath(name).name.lower() == "activities.csv"),
                key=lambda name: (len(PurePosixPath(name).parts), name),
            )
            if names:
                self.activities_csv_name = names[0]
                return self
        self.close()
        raise FileNotFoundError(f"Could not find activities.csv inside Strava export: {self.export_path}")

    def __exit__(self, *_: Any) -> None:
        self.close()

    def close(self) -> None:
        if self.archive is not None:
            self.archive.close()
            self.archive = None

    @contextmanager
    def open_activities_csv(self) -> Iterator[TextIO]:
        if self.activities_csv_path is not None:
            with self.activities_csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
                yield handle
            return
        if self.archive is None:
            raise FileNotFoundError("Strava archive is not open")
        stored_name = self._zip_names[self.activities_csv_name]
        with self.archive.open(stored_name) as raw_handle:
            with io.TextIOWrapper(raw_handle, encoding="utf-8-sig", newline="") as handle:
                yield handle

    @contextmanager
    def open_recording(self, filename: str) -> Iterator[Any]:
        safe_name = _safe_archive_name(filename)
        if safe_name is None:
            raise FileNotFoundError("Unsafe or empty archive recording path")

        if self.archive is not None:
            csv_parent = PurePosixPath(self.activities_csv_name).parent
            candidates = [safe_name]
            if str(csv_parent) not in ("", "."):
                candidates.insert(0, (csv_parent / safe_name).as_posix())
            stored_name = next(
                (self._zip_names[candidate] for candidate in candidates if candidate in self._zip_names),
                None,
            )
            if stored_name is None:
                raise FileNotFoundError("Referenced activity recording is missing from the archive")
            with self.archive.open(stored_name) as raw_handle:
                if safe_name.lower().endswith(".gz"):
                    with gzip.GzipFile(fileobj=raw_handle) as decompressed:
                        yield decompressed
                else:
                    yield raw_handle
            return

        if self.root is None or self.activities_csv_path is None:
            raise FileNotFoundError("Strava export directory is not open")
        candidates = [self.activities_csv_path.parent / safe_name, self.root / safe_name]
        for candidate in candidates:
            resolved = candidate.resolve()
            if not resolved.is_relative_to(self.root) or not resolved.is_file():
                continue
            with resolved.open("rb") as raw_handle:
                if safe_name.lower().endswith(".gz"):
                    with gzip.GzipFile(fileobj=raw_handle) as decompressed:
                        yield decompressed
                else:
                    yield raw_handle
            return
        raise FileNotFoundError("Referenced activity recording is missing from the export")


@contextmanager
def _seekable_recording(source: _StravaExportSource, filename: str) -> Iterator[Any]:
    with source.open_recording(filename) as recording:
        with tempfile.SpooledTemporaryFile(max_size=RECORDING_SPOOL_MEMORY_BYTES) as buffered:
            total = 0
            while True:
                chunk = recording.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_ARCHIVE_RECORDING_BYTES:
                    raise ValueError("Activity recording exceeds the local import size limit")
                buffered.write(chunk)
            buffered.seek(0)
            yield buffered


def _initialize_recording_worker(export_path: str) -> None:
    global _worker_export_source
    _worker_export_source = _StravaExportSource(Path(export_path)).__enter__()
    atexit.register(_worker_export_source.close)


def _parse_archive_recording(
    source: _StravaExportSource,
    data_dir: Path,
    activity_id: str,
    recording_name: str,
) -> Dict[str, Any]:
    try:
        with _seekable_recording(source, recording_name) as recording:
            payload = parse_activity_recording(recording, recording_name)
    except FileNotFoundError:
        return {"status": "missing"}
    except Exception:
        return {"status": "failed"}

    stream_path = _streams_dir(data_dir) / f"{activity_id}.json"
    laps_path = _laps_dir(data_dir) / f"{activity_id}.json"
    streams_created = not stream_path.exists()
    if streams_created:
        write_json(stream_path, payload["streams"])
    laps = payload["laps"].get("laps") or []
    laps_created = bool(laps) and not laps_path.exists()
    if laps_created:
        write_json(laps_path, payload["laps"])
    return {
        "status": "parsed",
        "streams_created": streams_created,
        "laps_created": laps_created,
        "streams_available": stream_path.exists(),
        "lap_count": len(laps),
        "laps_available": laps_path.exists(),
    }


def _recording_worker(job: tuple[str, str, str]) -> Dict[str, Any]:
    if _worker_export_source is None:
        raise RuntimeError("Archive recording worker was not initialized")
    data_dir, activity_id, recording_name = job
    return _parse_archive_recording(
        _worker_export_source,
        Path(data_dir),
        activity_id,
        recording_name,
    )


def _recording_worker_ready() -> bool:
    return _worker_export_source is not None


def _apply_recording_result(
    activity: Dict[str, Any],
    format_name: str,
    result: Dict[str, Any],
    recording_stats: Dict[str, Any],
) -> None:
    status = result["status"]
    if status != "parsed":
        recording_stats[f"recordings_{status}"] += 1
        return

    recording_stats["recordings_parsed"] += 1
    formats = recording_stats["recording_formats"]
    formats[format_name] = int(formats.get(format_name, 0)) + 1
    recording_stats["streams_created"] += int(result["streams_created"])
    recording_stats["laps_created"] += int(result["laps_created"])
    activity["archive_recording_format"] = format_name
    activity["archive_recording_parsed"] = True
    activity["archive_streams_available"] = result["streams_available"]
    activity["archive_lap_count"] = result["lap_count"]
    activity["archive_laps_available"] = result["laps_available"]


def _import_recordings(
    source: _StravaExportSource,
    data_dir: Path,
    activities_by_id: Dict[str, Dict[str, Any]],
    jobs: list[tuple[str, str, str]],
    recording_stats: Dict[str, Any],
) -> None:
    identifiers = [activity_id for activity_id, _, _ in jobs]
    parallel = (
        os.name != "nt"
        and len(jobs) >= MIN_PARALLEL_RECORDINGS
        and len(set(identifiers)) == len(identifiers)
    )
    workers = min(len(jobs), MAX_RECORDING_WORKERS, os.cpu_count() or 1) if parallel else 1
    executor = None
    if workers > 1:
        try:
            executor = ProcessPoolExecutor(
                max_workers=workers,
                initializer=_initialize_recording_worker,
                initargs=(str(source.export_path),),
            )
        except (OSError, NotImplementedError):
            executor = None
        if executor is not None:
            try:
                if not executor.submit(_recording_worker_ready).result():
                    raise BrokenProcessPool("Archive recording worker did not initialize")
            except (BrokenProcessPool, OSError, NotImplementedError):
                executor.shutdown(wait=True, cancel_futures=True)
                executor = None

    if executor is not None:
        with executor:
            for start in range(0, len(jobs), workers * 2):
                batch = jobs[start : start + workers * 2]
                worker_jobs = (
                    (str(data_dir), activity_id, recording_name)
                    for activity_id, recording_name, _ in batch
                )
                for job, result in zip(batch, executor.map(_recording_worker, worker_jobs)):
                    activity_id, _, format_name = job
                    _apply_recording_result(
                        activities_by_id[activity_id], format_name, result, recording_stats
                    )
        return

    for activity_id, recording_name, format_name in jobs:
        activity = activities_by_id[activity_id]
        stream_path = _streams_dir(data_dir) / f"{activity_id}.json"
        laps_path = _laps_dir(data_dir) / f"{activity_id}.json"
        if stream_path.exists() and (
            laps_path.exists() or activity.get("archive_recording_parsed") is True
        ):
            recording_stats["recordings_skipped_existing"] += 1
            continue
        result = _parse_archive_recording(source, data_dir, activity_id, recording_name)
        _apply_recording_result(activity, format_name, result, recording_stats)


def import_strava_export(data_dir: Path, export_path: Path) -> Dict[str, Any]:
    export_path = export_path.expanduser()
    existing_activities = _activity_index(read_json(_activities_path(data_dir), default={}) or {})
    activities_by_id = dict(existing_activities)
    rows = 0
    skipped = 0
    created = 0
    updated = 0
    recording_stats: Dict[str, Any] = {
        "recordings_referenced": 0,
        "recordings_parsed": 0,
        "recordings_missing": 0,
        "recordings_unsupported": 0,
        "recordings_failed": 0,
        "recordings_skipped_existing": 0,
        "streams_created": 0,
        "laps_created": 0,
        "recording_formats": {},
    }

    with _StravaExportSource(export_path) as source:
        recording_jobs: list[tuple[str, str, str]] = []
        with source.open_activities_csv() as handle:
            raw_rows = list(csv.DictReader(handle))
        activities_csv = source.activities_csv_name
        for raw_row in raw_rows:
            rows += 1
            parsed = _archive_activity(_normalized_export_row(raw_row))
            if not parsed:
                skipped += 1
                continue
            activity_id, archive_activity = parsed
            existing = activities_by_id.get(activity_id)
            if existing:
                merged = dict(archive_activity)
                merged.update(existing)
                for metadata_key in ("source_archive_file", "import_source"):
                    if archive_activity.get(metadata_key):
                        merged[metadata_key] = archive_activity[metadata_key]
                activities_by_id[activity_id] = merged
                updated += 1
            else:
                activities_by_id[activity_id] = archive_activity
                created += 1

            recording_name = archive_activity.get("source_archive_file")
            if not recording_name:
                continue
            recording_stats["recordings_referenced"] += 1
            format_name = recording_format(str(recording_name))
            if format_name is None:
                recording_stats["recordings_unsupported"] += 1
                continue

            stream_path = _streams_dir(data_dir) / f"{activity_id}.json"
            laps_path = _laps_dir(data_dir) / f"{activity_id}.json"
            activity = activities_by_id[activity_id]
            if stream_path.exists() and (
                laps_path.exists() or activity.get("archive_recording_parsed") is True
            ):
                recording_stats["recordings_skipped_existing"] += 1
                continue
            recording_jobs.append((activity_id, str(recording_name), format_name))

        _import_recordings(source, data_dir, activities_by_id, recording_jobs, recording_stats)

    write_json(_activities_path(data_dir), activities_by_id)
    state = read_json(_state_path(data_dir), default={}) or {}
    if not isinstance(state, dict):
        state = {}
    imported_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    archive_import = {
        "archive_name": export_path.name,
        "activities_csv": activities_csv,
        "imported_at": imported_at,
        "activity_count": len(activities_by_id),
        "rows": rows,
        "created": created,
        "updated": updated,
        "skipped": skipped,
        **recording_stats,
    }
    state["archive_import"] = archive_import
    state["activity_count"] = len(activities_by_id)
    write_json(_state_path(data_dir), state)
    return archive_import
