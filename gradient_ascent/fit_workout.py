"""Encode a bounded cycling FIT Workout using only the standard library.

Protocol/schema references:
https://developer.garmin.com/fit/protocol/
https://developer.garmin.com/fit/file-types/workout/
https://developer.garmin.com/fit/cookbook/encoding-workout-files/

This is a workout instruction file (type 5), not a completed activity, navigation
course, or calendar entry. No provider account, device, or file is accessed here.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timezone
import hashlib
import struct
from typing import Any

MAX_WORKOUT_STEPS = 50
MAX_STRING_BYTES = 254  # FIT field-size byte also includes the string's terminating NUL.
MAX_DURATION_SECONDS = 86400
MAX_PERCENT_FTP = 300
MAX_POWER_WATTS = 3000
_FIT_EPOCH = datetime(1989, 12, 31, tzinfo=timezone.utc)
_INTENSITY = {"active": 0, "rest": 1, "warmup": 2, "cooldown": 3, "recovery": 4}
_ENUM = 0x00
_STRING = 0x07
_UINT16 = 0x84
_UINT32 = 0x86
_UINT32Z = 0x8C


def _integer(value: Any, minimum: int, maximum: int, label: str) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{label} must be an integer from {minimum} to {maximum}.")
    return value


def _text(value: Any, label: str, *, required: bool = False) -> bytes:
    if not isinstance(value, str) or "\0" in value or (required and not value.strip()):
        raise ValueError(f"{label} must be valid text.")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        raise ValueError(f"{label} must be valid UTF-8 text.") from None
    if len(encoded) > MAX_STRING_BYTES:
        raise ValueError(f"{label} exceeds the FIT limit of {MAX_STRING_BYTES} UTF-8 bytes.")
    return encoded + b"\0"


def _field(number: int, base_type: int, payload: bytes) -> tuple[int, int, bytes]:
    return number, base_type, payload


def _enum(number: int, value: int) -> tuple[int, int, bytes]:
    return _field(number, _ENUM, struct.pack("<B", value))


def _u16(number: int, value: int) -> tuple[int, int, bytes]:
    return _field(number, _UINT16, struct.pack("<H", value))


def _u32(number: int, value: int, *, zero_invalid: bool = False) -> tuple[int, int, bytes]:
    return _field(number, _UINT32Z if zero_invalid else _UINT32, struct.pack("<I", value))


def _message(global_number: int, fields: list[tuple[int, int, bytes]]) -> bytes:
    # Local message zero is redefined as needed; all multibyte fields are little-endian.
    definition = bytearray(struct.pack("<BBBH B", 0x40, 0, 0, global_number, len(fields)))
    payload = bytearray(b"\0")
    for number, base_type, value in fields:
        if not 1 <= len(value) <= 255:
            raise ValueError("A FIT field exceeds its supported size.")
        definition.extend((number, len(value), base_type))
        payload.extend(value)
    return bytes(definition + payload)


def _crc(data: bytes) -> int:
    # FIT uses the reflected CRC-16/IBM polynomial, with an initial value of zero.
    crc = 0
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = (crc >> 1) ^ (0xA001 if crc & 1 else 0)
    return crc


def _power_target(target: Any) -> tuple[int, int, int]:
    if not isinstance(target, Mapping):
        raise ValueError("Each workout step needs an explicit target.")
    if target.get("type") == "open" and set(target) == {"type"}:
        return 2, 0, 0
    if target.get("type") != "power" or set(target) != {"type", "unit", "low", "high"}:
        raise ValueError("FIT export supports only open or bounded power targets.")
    unit = target["unit"]
    if unit == "percent_ftp":
        minimum, maximum, offset = 0, MAX_PERCENT_FTP, 0
    elif unit == "watts":
        # 1000 is also the upper reserved relative-power boundary in the FIT
        # convention. Do not encode ambiguous absolute zero as 1000% FTP.
        minimum, maximum, offset = 1, MAX_POWER_WATTS, 1000
    else:
        raise ValueError("Power targets must use percent_ftp or watts.")
    low = _integer(target["low"], minimum, maximum, "Power target low")
    high = _integer(target["high"], minimum, maximum, "Power target high")
    if low > high:
        raise ValueError("Power target low must not exceed high.")
    return 4, low + offset, high + offset


def encode_workout_fit(workout: Mapping[str, Any]) -> bytes:
    """Return deterministic FIT Workout bytes for already flattened cycling steps.

    Names are preserved up to 254 UTF-8 bytes; some devices display fewer characters.
    Absolute zero-watt targets are rejected because the FIT offset boundary is
    ambiguous. Use an open target or an explicit zero-percent FTP target instead.
    """
    if not isinstance(workout, Mapping) or workout.get("sport") != "cycling":
        raise ValueError("FIT workout export currently supports cycling only.")
    identifier = _text(workout.get("id"), "Workout id", required=True)
    name = _text(workout.get("name"), "Workout name", required=True)
    description = _text(
        workout.get("device_description", workout.get("description", "")),
        "Workout device description",
    )
    try:
        day = date.fromisoformat(workout["date"])
        if day.isoformat() != workout["date"]:
            raise ValueError
        created = int(
            (datetime.combine(day, datetime.min.time(), timezone.utc) - _FIT_EPOCH).total_seconds()
        )
    except (KeyError, TypeError, ValueError):
        raise ValueError("Workout date must be an ISO calendar date.") from None
    if not 0 <= created < 0xFFFFFFFF:
        raise ValueError("Workout date is outside the FIT timestamp range.")
    steps = workout.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_WORKOUT_STEPS:
        raise ValueError(f"FIT workouts require 1 to {MAX_WORKOUT_STEPS} flattened steps.")

    encoded_steps = []
    total = 0
    for index, step in enumerate(steps):
        if not isinstance(step, Mapping) or set(step) != {
            "name",
            "duration_s",
            "intensity",
            "target",
        }:
            raise ValueError("Workout steps must be flattened and use the supported fields.")
        duration = _integer(step["duration_s"], 1, MAX_DURATION_SECONDS, "Step duration")
        total += duration
        if total > MAX_DURATION_SECONDS:
            raise ValueError("A workout cannot exceed 24 hours.")
        intensity = (
            _INTENSITY.get(step["intensity"]) if isinstance(step["intensity"], str) else None
        )
        if intensity is None:
            raise ValueError("Unsupported workout step intensity.")
        target_type, low, high = _power_target(step["target"])
        encoded_steps.append(
            _message(
                27,
                [
                    _u16(254, index),
                    _field(0, _STRING, _text(step["name"], "Workout step name")),
                    _enum(1, 0),  # duration_type=time
                    _u32(2, duration * 1000),
                    _enum(3, target_type),
                    _u32(4, 0),  # custom target rather than a numbered device zone
                    _u32(5, low),
                    _u32(6, high),
                    _enum(7, intensity),
                ],
            )
        )

    # Development manufacturer 255 avoids claiming a Garmin/Wahoo product identity.
    # The stable opaque serial identifies this planned workout, not an athlete/device.
    serial = (
        int.from_bytes(
            hashlib.sha256(b"gradient-ascent-workout:" + identifier).digest()[:4], "little"
        )
        or 1
    )
    file_id = _message(
        0,
        [
            _enum(0, 5),
            _u16(1, 255),
            _u16(2, 0),
            _u32(3, serial, zero_invalid=True),
            _u32(4, created),
        ],
    )
    summary = _message(
        26,
        [
            _enum(4, 2),
            _u16(6, len(steps)),
            _field(8, _STRING, name),
            _field(17, _STRING, description),
        ],
    )
    body = file_id + summary + b"".join(encoded_steps)
    header = struct.pack("<BBHI4s", 14, 0x20, 21213, len(body), b".FIT")
    header += struct.pack("<H", _crc(header))
    result = header + body
    return result + struct.pack("<H", _crc(result))
