import unittest

from gradient_ascent.insights import AggregateTotals, _normalize_activity


class ArchiveMetricsTest(unittest.TestCase):
    def test_power_and_duration_derive_kilojoules_for_archive_ride(self) -> None:
        activity = _normalize_activity(
            {
                "id": 123,
                "sport_type": "Ride",
                "moving_time": 3600,
                "average_watts": 200,
            },
            ftp_w=None,
        )

        self.assertEqual(activity["kilojoules"], 720.0)
        self.assertTrue(activity["is_meaningful_ride"])

    def test_virtual_and_ebike_rides_contribute_meaningful_hours(self) -> None:
        totals = AggregateTotals()
        for sport_type in ("Virtual Ride", "E-Bike Ride"):
            activity = _normalize_activity(
                {
                    "sport_type": sport_type,
                    "moving_time": 3600,
                    "average_watts": 150,
                },
                ftp_w=None,
            )
            self.assertTrue(activity["is_meaningful_ride"])
            totals.add_activity(activity)

        finalized = totals.finalize()
        self.assertEqual(finalized["meaningful_ride_count"], 2)
        self.assertEqual(finalized["meaningful_ride_time_s"], 7200.0)
