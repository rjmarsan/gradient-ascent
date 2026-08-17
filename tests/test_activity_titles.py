import unittest

from gradient_ascent.activity_titles import is_placeholder_title, select_activity_title


class ActivityTitlesTest(unittest.TestCase):
    def test_known_placeholders_use_meaningful_planned_name(self):
        for title in (
            None,
            "",
            "  Private ride  ",
            "Imported Ride",
            "Untitled activity",
            "405588560",
            "405875999",
            "08/14/26",
            "2026-08-14",
            "2026-08-14T07:35:22-07:00",
            "2026-08-14 14:35:22Z",
            "405588560.fit",
            "405875999.tcx.gz",
            "2026-08-14.gpx",
            "Ridewithgps 405588560",
            "recording-" + "a" * 64,
        ):
            with self.subTest(title=title):
                self.assertTrue(is_placeholder_title(title))
                self.assertEqual(
                    select_activity_title(title, planned_name="3x10 threshold"),
                    "3x10 threshold",
                )

    def test_genuine_provider_names_are_preserved(self):
        for title in (
            "3x10 threshold",
            "2026 Spring Classic",
            "Morning Ride",
            "Private ride with friends",
            "08/14/26 recovery spin",
            "2026-08-14 Alpine Loop",
            "Alpine Loop.fit",
            "1984",
            "12345",
            "08/99/26",
        ):
            with self.subTest(title=title):
                self.assertFalse(is_placeholder_title(title))
                self.assertEqual(select_activity_title(title, planned_name="Other plan"), title)

    def test_authored_override_wins_even_when_placeholder_shaped(self):
        for authored in ("405588560", "08/14/26", "Private ride", "My chosen name"):
            with self.subTest(authored=authored):
                self.assertEqual(
                    select_activity_title(
                        "405875999", planned_name="Tempo", authored_title=authored
                    ),
                    authored,
                )

    def test_short_ids_only_count_when_provenance_matches(self):
        self.assertFalse(is_placeholder_title("12345"))
        self.assertTrue(is_placeholder_title("12345", source_ids=(12345,)))
        self.assertTrue(is_placeholder_title("12345.fit", source_ids=(12345,)))
        self.assertFalse(is_placeholder_title("12345.fit"))
        self.assertEqual(
            select_activity_title("12345", source_ids=(12345,), planned_name="Endurance"),
            "Endurance",
        )

    def test_unhelpful_plans_and_source_urls_never_become_fallback_names(self):
        for planned in (None, "", "No plan", "No planned session", "405588560", "2026-08-14"):
            with self.subTest(planned=planned):
                self.assertEqual(select_activity_title("405875999", planned_name=planned), "Ride")
        self.assertEqual(
            select_activity_title(
                "https://ridewithgps.com/trips/405588560",
                source_ids=("405588560",),
                planned_name="Endurance",
            ),
            "Endurance",
        )
        self.assertEqual(
            select_activity_title("Private ride", fallback="Private ride"), "Private ride"
        )

    def test_only_whole_valid_dates_or_timestamps_are_placeholders(self):
        for title in ("2026-08-14T99:35:22Z", "2026-02-30", "Race 2026-08-14T07:00:00Z"):
            with self.subTest(title=title):
                self.assertFalse(is_placeholder_title(title))
        self.assertTrue(is_placeholder_title("08-14-2026"))
        self.assertTrue(is_placeholder_title("2026/08/14"))

    def test_source_identifier_iterators_cannot_leak_through_fallbacks(self):
        self.assertEqual(
            select_activity_title("405588560", planned_name="12345", source_ids=iter(("12345",))),
            "Ride",
        )
        self.assertEqual(
            select_activity_title("Private ride", fallback="12345", source_ids=("12345",)),
            "Ride",
        )


if __name__ == "__main__":
    unittest.main()
