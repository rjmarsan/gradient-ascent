from copy import deepcopy
import io
import struct
import unittest

import fitdecode
from fitdecode.utils import compute_crc

from gradient_ascent.fit_workout import encode_workout_fit


def workout():
    return {
        "id": "synthetic-tempo-1",
        "date": "2026-08-18",
        "name": "Tempo vélo",
        "description": "Synthetic workout for file-format verification.",
        "sport": "cycling",
        "steps": [
            {
                "name": "Warm up",
                "duration_s": 600,
                "intensity": "warmup",
                "target": {"type": "power", "unit": "percent_ftp", "low": 45, "high": 60},
            },
            {
                "name": "Tempo",
                "duration_s": 1200,
                "intensity": "active",
                "target": {"type": "power", "unit": "watts", "low": 175, "high": 195},
            },
            {
                "name": "Cool down",
                "duration_s": 300,
                "intensity": "cooldown",
                "target": {"type": "open"},
            },
        ],
    }


def decode(data):
    with fitdecode.FitReader(
        io.BytesIO(data),
        check_crc=fitdecode.CrcCheck.RAISE,
        error_handling=fitdecode.ErrorHandling.RAISE,
    ) as reader:
        return [frame for frame in reader if isinstance(frame, fitdecode.FitDataMessage)]


class FITWorkoutTest(unittest.TestCase):
    def test_independent_decoder_verifies_workout_messages_targets_and_crc(self):
        data = encode_workout_fit(workout())
        self.assertEqual(data[:2], bytes((14, 0x20)))
        self.assertEqual(data[8:12], b".FIT")
        self.assertEqual(struct.unpack_from("<I", data, 4)[0], len(data) - 16)
        self.assertEqual(struct.unpack_from("<H", data, 12)[0], compute_crc(data[:12]))
        self.assertEqual(struct.unpack_from("<H", data, len(data) - 2)[0], compute_crc(data[:-2]))
        messages = decode(data)
        self.assertEqual(
            [message.name for message in messages],
            ["file_id", "workout", "workout_step", "workout_step", "workout_step"],
        )
        self.assertEqual(messages[0].get_value("type"), "workout")
        self.assertEqual(messages[0].get_value("manufacturer"), "development")
        self.assertEqual(messages[1].get_value("sport"), "cycling")
        self.assertEqual(messages[1].get_value("num_valid_steps"), 3)
        self.assertEqual(messages[1].get_value("wkt_name"), "Tempo vélo")
        self.assertEqual(messages[1].get_value("wkt_description"), workout()["description"])
        steps = messages[2:]
        self.assertEqual([step.get_value("message_index") for step in steps], [0, 1, 2])
        self.assertEqual([step.get_value("duration_time") for step in steps], [600, 1200, 300])
        self.assertEqual(
            [step.get_value("intensity") for step in steps], ["warmup", "active", "cooldown"]
        )
        self.assertEqual(steps[0].get_value("target_type"), "power")
        self.assertEqual(steps[0].get_value("target_power_zone"), 0)
        self.assertEqual(steps[0].get_value(5, raw_value=True), 45)
        self.assertEqual(steps[0].get_value(6, raw_value=True), 60)
        self.assertEqual(steps[1].get_value(5, raw_value=True), 1175)
        self.assertEqual(steps[1].get_value(6, raw_value=True), 1195)
        self.assertEqual(steps[2].get_value("target_type"), "open")
        corrupted = bytearray(data)
        corrupted[-3] ^= 1
        with self.assertRaises(fitdecode.FitCRCError):
            decode(corrupted)

    def test_deterministic_identity_and_full_utf8_names(self):
        value = workout()
        value["name"] = "é" * 127
        value["steps"][0]["name"] = "x" * 254
        encoded = encode_workout_fit(value)
        self.assertEqual(encoded, encode_workout_fit(deepcopy(value)))
        messages = decode(encoded)
        self.assertEqual(messages[1].get_value("wkt_name"), value["name"])
        self.assertEqual(messages[2].get_value("wkt_step_name"), value["steps"][0]["name"])
        changed = deepcopy(value)
        changed["id"] = "other-workout"
        self.assertNotEqual(
            messages[0].get_value("serial_number"),
            decode(encode_workout_fit(changed))[0].get_value("serial_number"),
        )

    def test_fifty_steps_and_zero_percent_are_supported(self):
        value = workout()
        step = {
            "name": "Recover",
            "duration_s": 1,
            "intensity": "recovery",
            "target": {"type": "power", "unit": "percent_ftp", "low": 0, "high": 0},
        }
        value["steps"] = [deepcopy(step) for _ in range(50)]
        messages = decode(encode_workout_fit(value))
        self.assertEqual(messages[1].get_value("num_valid_steps"), 50)
        self.assertEqual(messages[-1].get_value("message_index"), 49)
        self.assertEqual(messages[-1].get_value(5, raw_value=True), 0)

    def test_explicit_device_description_preserves_richer_plan_text(self):
        value = workout()
        value["description"] = "Long-form coaching context. " * 30
        value["device_description"] = "Follow the targets; stop if unwell."
        messages = decode(encode_workout_fit(value))
        self.assertEqual(messages[1].get_value("wkt_description"), value["device_description"])

    def test_invalid_or_ambiguous_workouts_fail_without_silent_conversion(self):
        cases = []
        for key, value in (
            ("sport", "running"),
            ("name", "é" * 128),
            ("description", "x" * 255),
            ("date", "not-a-date"),
            ("id", ""),
            ("steps", []),
        ):
            item = workout()
            item[key] = value
            cases.append(item)
        item = workout()
        item["steps"] = [deepcopy(item["steps"][0]) for _ in range(51)]
        cases.append(item)
        for key, value in (
            ("duration_s", True),
            ("duration_s", 0),
            ("duration_s", 86401),
            ("intensity", "unknown"),
            ("name", "embedded\0name"),
            ("repeat", 2),
        ):
            item = workout()
            item["steps"][0][key] = value
            cases.append(item)
        for target in (
            {"type": "heart_rate", "low": 100, "high": 120},
            {"type": "power", "unit": "watts", "low": 0, "high": 100},
            {"type": "power", "unit": "watts", "low": 1, "high": 3001},
            {"type": "power", "unit": "percent_ftp", "low": 301, "high": 301},
            {"type": "power", "unit": "percent_ftp", "low": True, "high": 100},
            {"type": "power", "unit": "percent_ftp", "low": 100, "high": 90},
            {"type": "open", "low": 10},
        ):
            item = workout()
            item["steps"][0]["target"] = target
            cases.append(item)
        item = workout()
        item["steps"][0]["duration_s"] = 86400
        cases.append(item)
        for index, item in enumerate(cases):
            with self.subTest(index=index), self.assertRaises(ValueError):
                encode_workout_fit(item)


if __name__ == "__main__":
    unittest.main()
