"""Conservative estimates from local power streams, never provider-measured NP.

The 30-second rolling-power basis follows TrainingPeaks' published definition:
https://help.trainingpeaks.com/hc/en-us/articles/204071804-Normalized-Power
Only observed intervals are used. Missing telemetry is never filled with zero or
extended through a pause, and the cache contains aggregate metrics only.
"""

from __future__ import annotations

import json
import math
import os
import re
import stat
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .storage import write_json


METHOD = "power_stream_30s_v2"
CACHE_VERSION = 2
MAX_SAMPLES = 250_000
MAX_OBSERVED_SECONDS = 172_800
MAX_TIMELINE_SECONDS = 7 * 86_400
MAX_STREAM_BYTES = 64 * 1024 * 1024
MAX_CACHE_BYTES = 8 * 1024 * 1024
MAX_GAP_SECONDS = 5
MIN_OBSERVED_SECONDS = 600
MIN_ROLLING_COVERAGE = 0.8
MAX_POWER_WATTS = 10_000
_RECORDING_ID = re.compile(r"recording-[a-f0-9]{64}\Z")


def finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def estimate_normalized_power(times: Any, watts: Any) -> dict[str, Any] | None:
    """Time-weighted one-second bins and complete 30-second rolling windows."""
    if (
        not isinstance(times, list)
        or not isinstance(watts, list)
        or len(times) != len(watts)
        or not 2 <= len(times) <= MAX_SAMPLES
    ):
        return None
    previous: tuple[float, float] | None = None
    latest_timestamp: float | None = None
    window: deque[float] = deque()
    window_sum = bin_seconds = bin_energy = fourth_sum = 0.0
    observed = windows = 0

    def reset() -> None:
        nonlocal window_sum, bin_seconds, bin_energy
        window.clear()
        window_sum = bin_seconds = bin_energy = 0.0

    for raw_time, raw_power in zip(times, watts):
        timestamp, power = finite_number(raw_time), finite_number(raw_power)
        if timestamp is not None and 0 <= timestamp <= MAX_TIMELINE_SECONDS:
            if latest_timestamp is not None and timestamp <= latest_timestamp:
                # Replayed/backtracked intervals must never count twice, even
                # if an intervening invalid power sample reset the window.
                return None
            latest_timestamp = timestamp
        if (
            timestamp is None
            or power is None
            or not 0 <= timestamp <= MAX_TIMELINE_SECONDS
            or not 0 <= power <= MAX_POWER_WATTS
        ):
            previous = None
            reset()
            continue
        if previous is not None:
            remaining = timestamp - previous[0]
            if remaining > MAX_GAP_SECONDS:
                reset()
            else:
                while remaining > 1e-9:
                    portion = min(remaining, 1.0 - bin_seconds)
                    bin_energy += previous[1] * portion
                    bin_seconds += portion
                    remaining -= portion
                    if bin_seconds >= 1.0 - 1e-9:
                        observed += 1
                        if observed > MAX_OBSERVED_SECONDS:
                            return None
                        if len(window) == 30:
                            window_sum -= window.popleft()
                        window.append(bin_energy)
                        window_sum += bin_energy
                        if len(window) == 30:
                            fourth_sum += (max(0.0, window_sum) / 30.0) ** 4
                            windows += 1
                        bin_seconds = bin_energy = 0.0
        previous = timestamp, power
    if observed < MIN_OBSERVED_SECONDS or windows / observed < MIN_ROLLING_COVERAGE:
        return None
    return {
        "method": METHOD,
        "estimated_normalized_power_w": round((fourth_sum / windows) ** 0.25, 6),
        "observed_duration_s": observed,
        "rolling_window_duration_s": windows,
        "gap_limit_s": MAX_GAP_SECONDS,
    }


def _safe_owner(info: os.stat_result) -> bool:
    return (not hasattr(os, "getuid") or info.st_uid == os.getuid()) and not stat.S_IMODE(
        info.st_mode
    ) & 0o022


@contextmanager
def _stream_directory(data_dir: Path) -> Iterator[int | None]:
    descriptor = -1
    usable = False
    try:
        try:
            if hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY"):
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                descriptor = os.open(data_dir, flags)
                for part in ("recordings", "streams"):
                    if not _safe_owner(os.fstat(descriptor)):
                        break
                    child = os.open(part, flags, dir_fd=descriptor)
                    os.close(descriptor)
                    descriptor = child
                else:
                    usable = _safe_owner(os.fstat(descriptor))
        except OSError:
            usable = False
        yield descriptor if usable else None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _signature(info: os.stat_result) -> list[int]:
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def _regular(info: os.stat_result, limit: int) -> bool:
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_nlink == 1
        and info.st_size <= limit
        and _safe_owner(info)
    )


def _estimate_file(directory: int, name: str, expected: os.stat_result) -> dict[str, Any] | None:
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
    try:
        if _signature(os.fstat(descriptor)) != _signature(expected):
            return None
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            body = handle.read(MAX_STREAM_BYTES + 1)
            unchanged = _signature(os.fstat(handle.fileno())) == _signature(expected)
        if not unchanged or len(body) > MAX_STREAM_BYTES:
            return None
        payload = json.loads(body)
        if not isinstance(payload, dict) or not isinstance(payload.get("streams"), list):
            return None
        selected = {}
        for stream in payload["streams"]:
            if isinstance(stream, dict) and stream.get("type") in {"time", "watts"}:
                key = stream["type"]
                if key in selected:
                    return None
                selected[key] = stream.get("data")
        return estimate_normalized_power(selected.get("time"), selected.get("watts"))
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _valid_estimate(value: Any) -> bool:
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "method",
            "estimated_normalized_power_w",
            "observed_duration_s",
            "rolling_window_duration_s",
            "gap_limit_s",
        }
        or value.get("method") != METHOD
        or type(value.get("gap_limit_s")) is not int
        or value.get("gap_limit_s") != MAX_GAP_SECONDS
        or type(value.get("observed_duration_s")) is not int
        or type(value.get("rolling_window_duration_s")) is not int
    ):
        return False
    power = finite_number(value.get("estimated_normalized_power_w"))
    observed = finite_number(value.get("observed_duration_s"))
    windows = finite_number(value.get("rolling_window_duration_s"))
    return (
        power is not None
        and 0 <= power <= MAX_POWER_WATTS
        and observed is not None
        and MIN_OBSERVED_SECONDS <= observed <= MAX_OBSERVED_SECONDS
        and windows is not None
        and MIN_ROLLING_COVERAGE * observed <= windows <= observed
    )


def _read_cache(path: Path) -> dict[str, Any]:
    try:
        info = path.lstat()
        if not _regular(info, MAX_CACHE_BYTES):
            return {}
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as handle:
            if _signature(os.fstat(handle.fileno())) != _signature(info):
                return {}
            body = handle.read(MAX_CACHE_BYTES + 1)
        if len(body) > MAX_CACHE_BYTES:
            return {}
        value = json.loads(body)
        if (
            isinstance(value, dict)
            and type(value.get("version")) is int
            and value.get("version") == CACHE_VERSION
            and isinstance(value.get("entries"), dict)
        ):
            return value["entries"]
    except (OSError, ValueError, UnicodeError):
        pass
    return {}


def enrich_recording_power(
    data_dir: Path, activities: list[dict[str, Any]], output_dir: Path
) -> list[dict[str, Any]]:
    """Add aggregate estimates to canonical copies; never modify source recordings."""
    candidates = [
        activity
        for activity in activities
        if (activity.get("source") or {}).get("provider") == "recording"
        and (
            finite_number(activity.get("weighted_average_watts")) is None
            or finite_number(activity.get("weighted_average_watts")) < 0
        )
    ]
    if not candidates:
        return activities
    cache_path = output_dir / "power_metrics_cache.json"
    if output_dir.is_symlink() or cache_path.is_symlink():
        raise ValueError("Power metrics cache cannot use symbolic links.")
    cache = _read_cache(cache_path)
    updates: dict[str, Any] = {}
    estimates: dict[str, dict[str, Any]] = {}
    with _stream_directory(data_dir) as directory:
        if directory is None:
            return activities
        for activity in candidates:
            identifier = activity.get("provider_id")
            if not isinstance(identifier, str) or _RECORDING_ID.fullmatch(identifier) is None:
                continue
            try:
                info = os.stat(f"{identifier}.json", dir_fd=directory, follow_symlinks=False)
                if not _regular(info, MAX_STREAM_BYTES):
                    continue
                signature = _signature(info)
                previous = cache.get(identifier)
                if (
                    isinstance(previous, dict)
                    and previous.get("signature") == signature
                    and (
                        previous.get("estimate") is None
                        or _valid_estimate(previous.get("estimate"))
                    )
                ):
                    estimate = previous.get("estimate")
                else:
                    estimate = _estimate_file(directory, f"{identifier}.json", info)
                updates[identifier] = {"signature": signature, "estimate": estimate}
                if estimate is not None:
                    estimates[activity["id"]] = estimate
            except (OSError, ValueError, UnicodeError, TypeError):
                continue
    if output_dir.is_symlink() or cache_path.is_symlink():
        raise ValueError("Power metrics cache cannot use symbolic links.")
    if updates != cache:
        write_json(cache_path, {"version": CACHE_VERSION, "entries": updates})
    return [
        {
            **activity,
            "estimated_normalized_power_w": estimates[activity["id"]][
                "estimated_normalized_power_w"
            ],
            "power_load_estimate": dict(estimates[activity["id"]]),
        }
        if activity.get("id") in estimates
        else activity
        for activity in activities
    ]
