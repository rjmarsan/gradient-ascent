import json
import shutil
import subprocess
import unittest
from html.parser import HTMLParser

from gradient_ascent import training_center


class _VisibleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)

    @classmethod
    def read(cls, html):
        parser = cls()
        parser.feed(html)
        return " ".join(" ".join(parser.parts).split())


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for the Week card renderer")
class WeekCardStatsTest(unittest.TestCase):
    def render(self, day):
        template = training_center.HTML_TEMPLATE

        def functions_between(first, following):
            start = template.index(f"    function {first}(")
            return template[start : template.index(f"\n    function {following}(", start)]

        source = "\n".join(
            (
                functions_between("escapeHtml", "truncate"),
                functions_between("formatTssNumber", "formatCoverageNumber"),
                functions_between("compactStat", "numericLabel"),
                functions_between("renderWeekRideFooter", "renderIndependentWorkouts"),
                functions_between("weekDayCue", "renderWeekDay"),
            )
        )
        script = (
            source
            + f"\nconst day = {json.dumps(day)};"
            + "\nconst before = JSON.stringify(day);"
            + "\nconsole.log(JSON.stringify({html: renderWeekRideFooter(day),"
            + " options: dayLoadOptions(day), unchanged: before === JSON.stringify(day)}));"
        )
        result = subprocess.run(
            [shutil.which("node"), "-e", script],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        rendered = json.loads(result.stdout)
        self.assertTrue(rendered["unchanged"])
        return rendered

    @staticmethod
    def day(*, recorded=True, value=75.4, qualifier="Calculated", description=None):
        return {
            "date": "2026-07-28",
            "has_synced_ride": recorded,
            "planned": "45–60min easy spin",
            "planned_load": {
                "estimated_tss": 25.5,
                "tss_value_label": "15–36 TSS",
                "hours_label": "0.75–1h",
                "estimated": True,
                "qualifier": "Rough forecast",
                "note": "The original plan remains available.",
            },
            "metrics": {
                "estimated_tss": value,
                "moving_hours": 1.5,
                "tss_estimated": qualifier.startswith("Calculated"),
                "tss_qualifier": qualifier,
                "tss_description": description or "Calculated using the configured FTP.",
            },
        }

    def test_recorded_card_uses_actual_stats_without_repeating_plain_provenance(self):
        for qualifier in ("Calculated", "Source"):
            with self.subTest(qualifier=qualifier):
                result = self.render(self.day(qualifier=qualifier))
                html = result["html"]
                visible = _VisibleText.read(html)
                self.assertNotIn('class="week-plan-summary"', html)
                self.assertIn('aria-label="Recorded day stats"', html)
                self.assertNotIn("Scheduled", visible)
                self.assertNotIn("Rough forecast", visible)
                self.assertNotIn(qualifier, visible)
                self.assertIn("Recorded", visible)
                self.assertIn("75 TSS", visible)
                self.assertIn("1hr 30min", visible)
                self.assertIn('title="Calculated using the configured FTP."', html)
                self.assertEqual(result["options"]["qualifier"], qualifier)

    def test_unrecorded_days_keep_their_original_plan_and_forecast(self):
        for day_date in ("2026-07-28", "2026-09-01"):
            with self.subTest(date=day_date):
                day = self.day(recorded=False)
                day["date"] = day_date
                result = self.render(day)
                visible = _VisibleText.read(result["html"])
                self.assertIn('aria-label="Scheduled day stats"', result["html"])
                self.assertIn('class="week-plan-summary"', result["html"])
                self.assertIn("Scheduled 15–36 TSS 0.75–1h Rough forecast", visible)
                self.assertNotIn("Recorded", visible)

    def test_zero_and_unsupported_recorded_load_never_fall_back_to_the_plan(self):
        for value, qualifier, label in (
            (0, "Source", "0 TSS"),
            (None, "1 ride without load", "-- TSS"),
        ):
            with self.subTest(value=value):
                result = self.render(self.day(value=value, qualifier=qualifier))
                visible = _VisibleText.read(result["html"])
                self.assertNotIn('class="week-plan-summary"', result["html"])
                self.assertIn(label, visible)
                self.assertIn("1hr 30min", visible)
                if value is None:
                    self.assertIn("1 ride without load", visible)

    def test_partial_load_keeps_only_the_meaningful_warning_and_full_tooltip(self):
        qualifier = "Calculated · 79.3% power coverage · 1 ride without load"
        description = 'Calculated from available power; <missing> is not "zero".'
        result = self.render(self.day(qualifier=qualifier, description=description))
        visible = _VisibleText.read(result["html"])
        self.assertIn("79.3% power coverage · 1 ride without load", visible)
        self.assertNotIn("Calculated", visible)
        self.assertIn("&lt;missing&gt; is not &quot;zero&quot;", result["html"])
        self.assertEqual(result["options"], {"qualifier": qualifier, "description": description})
        incomplete = self.render(self.day(qualifier="Source · Power data incomplete"))
        self.assertIn("Power data incomplete", _VisibleText.read(incomplete["html"]))
        self.assertNotIn("Source", _VisibleText.read(incomplete["html"]))

    def test_recorded_presence_does_not_depend_on_loaded_activity_details_or_sport(self):
        for activities in ([], [{"sport_type": "Walk", "meaningful": False}]):
            with self.subTest(activities=activities):
                day = self.day(value=0, qualifier="Source")
                day["activities"] = activities
                result = self.render(day)
                self.assertNotIn('class="week-plan-summary"', result["html"])
                self.assertIn("Recorded 0 TSS", _VisibleText.read(result["html"]))


if __name__ == "__main__":
    unittest.main()
