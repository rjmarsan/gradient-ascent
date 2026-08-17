import csv
import io
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path


def step(duration=60, *, target=None):
    return {
        "name": "Steady",
        "duration_s": duration,
        "intensity": "active",
        "target": target or {"type": "open"},
    }


def workout(**changes):
    return {
        "id": "tempo-session",
        "date": "2026-08-18",
        "name": "Tempo",
        "description": "Explicit prescription",
        "sport": "cycling",
        "steps": [step()],
        **changes,
    }


class PlannedWorkoutsTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "plan").mkdir()

    def write(self, filename, value):
        (self.root / "plan" / filename).write_text(json.dumps(value), encoding="utf-8")

    def load(self, **options):
        from gradient_ascent.planned_workouts import load_planned_workouts

        return load_planned_workouts(self.root, **options)

    def test_legacy_prose_stays_unstructured_and_explicit_entries_remain_independent(self):
        self.write(
            "weeks.json",
            [
                {
                    "start_date": "2026-08-17",
                    "days": {"Mon": "Strength or rest", "Tue": "3x10 tempo if fresh"},
                }
            ],
        )
        self.write("workouts.json", {"version": 1, "workouts": [workout()]})
        before = {path.name: path.read_bytes() for path in (self.root / "plan").iterdir()}
        entries = self.load()
        self.assertEqual(len(entries), 3)
        prose = next(row for row in entries if row["id"] == "week-2026-08-17-mon")
        self.assertEqual(
            (prose["sport"], prose["steps"], prose["structured"]), ("unspecified", [], False)
        )
        explicit = next(row for row in entries if row["id"] == "tempo-session")
        self.assertEqual(explicit["device_description"], "")
        self.assertEqual(explicit["source"], "plan/workouts.json")
        self.assertEqual(len(self.load(start=date(2026, 8, 18), end=date(2026, 8, 18))), 2)
        self.assertEqual(
            before, {path.name: path.read_bytes() for path in (self.root / "plan").iterdir()}
        )

    def test_events_preserve_status_location_priority_and_stable_ids(self):
        from gradient_ascent.planned_workouts import serialize_plan_csv, serialize_plan_ics

        events = [
            {
                "id": "race-one",
                "date": "2026-08-22",
                "name": "Committed race",
                "location": "City, State",
                "priority": "A",
                "markers": {"commitment": True},
            },
            {
                "id": "race-two",
                "date": "2026-08-23",
                "name": "Maybe race",
                "priority": "B",
                "markers": {"maybe": True},
            },
            {
                "id": "race-three",
                "date": "2026-08-24",
                "name": "Skipped race",
                "status": "confirmed",
                "markers": {"skip": True},
            },
            {"id": "race-four", "date": "2026-08-25", "name": "Unmarked race"},
        ]
        self.write("events.json", events)
        rows = self.load()
        self.assertEqual(
            [row["status"] for row in rows], ["confirmed", "tentative", "cancelled", "tentative"]
        )
        self.assertTrue(all(not row["structured"] and row["steps"] == [] for row in rows))
        self.assertTrue(
            all(
                row["source"] == "plan/events.json" and row["id"].startswith("event-")
                for row in rows
            )
        )
        self.assertEqual((rows[0]["location"], rows[0]["priority"]), ("City, State", "A"))
        first_id = rows[0]["id"]
        events[0]["name"] = "Renamed race"
        self.write("events.json", events)
        self.assertEqual(self.load()[0]["id"], first_id)
        csv_rows = list(csv.DictReader(io.StringIO(serialize_plan_csv(rows).decode())))
        self.assertEqual(csv_rows[2]["status"], "cancelled")
        calendar = serialize_plan_ics(rows).replace(b"\r\n ", b"").decode()
        self.assertIn("STATUS:CANCELLED\r\n", calendar)
        self.assertIn("STATUS:TENTATIVE\r\n", calendar)
        self.assertIn("LOCATION:City\\, State\r\n", calendar)
        self.assertIn("X-GRADIENT-ASCENT-PRIORITY:A\r\n", calendar)

    def test_non_monday_weeks_map_named_days_inside_declared_range(self):
        self.write(
            "weeks.json",
            [
                {
                    "start_date": "2026-08-18",
                    "end_date": "2026-08-24",
                    "days": {"Mon": "Next Monday", "Tue": "First Tuesday", "Sun": "Sunday"},
                },
                {
                    "start_date": "2027-01-31",
                    "end_date": "2027-02-06",
                    "days": {
                        "Mon": "February Monday",
                        "Sat": "Last Saturday",
                        "Sun": "First Sunday",
                    },
                },
                {
                    "start_date": "2026-09-01",
                    "end_date": "2026-09-03",
                    "days": {"Mon": "Outside range", "Thu": "Inside range"},
                },
            ],
        )
        dates = {row["name"]: row["date"] for row in self.load()}
        self.assertEqual(dates["First Tuesday"], "2026-08-18")
        self.assertEqual(dates["Next Monday"], "2026-08-24")
        self.assertEqual(dates["Sunday"], "2026-08-23")
        self.assertEqual(dates["February Monday"], "2027-02-01")
        self.assertEqual(dates["Last Saturday"], "2027-02-06")
        self.assertEqual(dates["First Sunday"], "2027-01-31")
        self.assertEqual(dates["Inside range"], "2026-09-03")
        self.assertNotIn("Outside range", dates)

    def test_event_cancellation_wins_and_malformed_markers_fail_closed(self):
        self.write(
            "events.json",
            [
                {
                    "id": "cancelled",
                    "date": "0001-01-01",
                    "name": "Historical example",
                    "status": "canceled",
                    "markers": {"commitment": True},
                    "priority": 0,
                }
            ],
        )
        entry = self.load()[0]
        self.assertEqual((entry["status"], entry["priority"]), ("cancelled", "0"))
        from gradient_ascent.planned_workouts import serialize_plan_ics

        self.assertIn(b"DTSTART;VALUE=DATE:00010101\r\n", serialize_plan_ics([entry]))
        self.write("events.json", [{"date": "2026-08-22", "name": "Invalid", "markers": False}])
        with self.assertRaises(ValueError):
            self.load()

    def test_explicit_repeat_groups_are_flattened_without_inference(self):
        power = {"type": "power", "unit": "percent_ftp", "low": 85, "high": 95}
        entries = [
            workout(
                description="Rich calendar text " * 100,
                device_description="Device cue",
                steps=[step(300), {"repeat": 3, "steps": [step(120, target=power), step(60)]}],
            )
        ]
        self.write("workouts.json", {"version": 1, "workouts": entries})
        result = self.load()[0]
        self.assertEqual(len(result["steps"]), 7)
        self.assertEqual(sum(item["duration_s"] for item in result["steps"]), 840)
        self.assertEqual(result["steps"][1]["target"], power)
        self.assertEqual(result["device_description"], "Device cue")
        from gradient_ascent.fit_workout import encode_workout_fit

        self.assertEqual(encode_workout_fit(result)[8:12], b".FIT")

    def test_invalid_schema_and_ambiguous_targets_fail_closed(self):
        bad = [
            workout(id="../escape"),
            workout(sport="running"),
            workout(date="20260818"),
            workout(extra=True),
            workout(name="é" * 128),
            workout(device_description="x" * 255),
            workout(steps=[]),
            workout(steps=[step(True)]),
            workout(steps=[step(86400), step(1)]),
            workout(steps=[{**step(), "extra": 1}]),
            workout(steps=[step(target={"type": "power", "unit": "watts", "low": 0, "high": 100})]),
            workout(
                steps=[
                    step(target={"type": "power", "unit": "percent_ftp", "low": 100, "high": 99})
                ]
            ),
            workout(steps=[step(target={"type": "open", "low": 1})]),
            workout(steps=[{"repeat": True, "steps": [step()]}]),
            workout(steps=[{"repeat": 26, "steps": [step(), step()]}]),
            workout(steps=[{"repeat": 2, "steps": [{"repeat": 2, "steps": [step()]}]}]),
        ]
        for value in bad:
            with self.subTest(value=value):
                self.write("workouts.json", {"version": 1, "workouts": [value]})
                with self.assertRaises(ValueError):
                    self.load()
        for document in (
            {"version": True, "workouts": []},
            {"version": 1, "workouts": [], "extra": 1},
            {"version": 1, "workouts": [workout(), workout()]},
        ):
            self.write("workouts.json", document)
            with self.assertRaises(ValueError):
                self.load()

    def test_symlink_and_duplicate_json_keys_are_rejected(self):
        outside = self.root / "outside.json"
        outside.write_text('{"version":1,"workouts":[]}', encoding="utf-8")
        linked = self.root / "plan" / "workouts.json"
        linked.symlink_to(outside)
        with self.assertRaises(ValueError):
            self.load()
        linked.unlink()
        linked.write_text('{"version":1,"version":1,"workouts":[]}', encoding="utf-8")
        with self.assertRaises(ValueError):
            self.load()

    def test_calendar_serializers_are_stable_escaped_and_spreadsheet_safe(self):
        from gradient_ascent.planned_workouts import serialize_plan_csv, serialize_plan_ics

        self.write(
            "workouts.json",
            {
                "version": 1,
                "workouts": [
                    workout(
                        name="=2+2", description=" @formula\nBackslash \\ comma, semi; " + "🚲" * 70
                    )
                ],
            },
        )
        entries = self.load()
        body = serialize_plan_csv(entries)
        row = list(csv.DictReader(io.StringIO(body.decode("utf-8"))))[0]
        self.assertEqual(row["name"], "'=2+2")
        self.assertTrue(row["description"].startswith("' @formula"))
        self.assertEqual(body, serialize_plan_csv(entries))
        calendar = serialize_plan_ics(entries)
        self.assertEqual(calendar, serialize_plan_ics(entries))
        self.assertTrue(calendar.endswith(b"END:VCALENDAR\r\n"))
        self.assertNotIn(b"\n", calendar.replace(b"\r\n", b""))
        for line in calendar.split(b"\r\n"):
            self.assertLessEqual(len(line), 75)
            line.decode("utf-8")
        unfolded = calendar.replace(b"\r\n ", b"").decode("utf-8")
        self.assertIn("DTSTART;VALUE=DATE:20260818\r\n", unfolded)
        self.assertIn("DTEND;VALUE=DATE:20260819\r\n", unfolded)
        self.assertIn("DTSTAMP:20260818T000000Z\r\n", unfolded)
        self.assertIn("Backslash \\\\ comma\\, semi\\;", unfolded)
        renamed = [{**entries[0], "name": "New title"}]
        uid = next(line for line in unfolded.split("\r\n") if line.startswith("UID:"))
        self.assertIn(uid, serialize_plan_ics(renamed).replace(b"\r\n ", b"").decode("utf-8"))
