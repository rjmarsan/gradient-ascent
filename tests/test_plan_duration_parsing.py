import unittest

from gradient_ascent.training_center import _planned_load_for_day


class PlanDurationParsingTest(unittest.TestCase):
    def test_compound_whole_session_duration_is_one_duration(self):
        for text in (
            "Endurance ride 1h30min",
            "Endurance ride 1h 30min",
            "Endurance ride1h30min",
            "Endurance ride 1 hour and 30 minutes",
            "1hr30min total including 3x10min threshold",
        ):
            with self.subTest(text=text):
                load = _planned_load_for_day(text, [])
                self.assertEqual(load["hours"], 1.5)
                self.assertIsNotNone(load["estimated_tss"])
        load = _planned_load_for_day("Endurance ride 1h30min", [])
        self.assertEqual(load["estimated_tss"], 68.3)

    def test_compound_ranges_and_repeated_units_keep_both_bounds(self):
        for text in (
            "Endurance ride 1h30min–2h15min",
            "Endurance ride 90min to 2h15min",
            "Endurance ride 1h 30min - 2h",
        ):
            with self.subTest(text=text):
                load = _planned_load_for_day(text, [])
                self.assertEqual(load["hours_min"], 1.5)
                self.assertEqual(load["hours_max"], 2 if text.endswith("2h") else 2.25)

    def test_compound_interval_components_and_invalid_values_stay_unknown(self):
        for text in (
            "3x1h30min threshold",
            "3x10min threshold; 1h30min recovery between efforts",
            "Endurance ride -1h30min",
            "Endurance ride 1h90min",
            "Endurance ride 2h15min–1h30min",
        ):
            with self.subTest(text=text):
                load = _planned_load_for_day(text, [])
                self.assertIsNone(load["hours"])
                self.assertIsNone(load["estimated_tss"])


if __name__ == "__main__":
    unittest.main()
