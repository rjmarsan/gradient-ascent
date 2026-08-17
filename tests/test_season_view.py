import json
import shutil
import subprocess
import unittest

from gradient_ascent import training_center


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for Season regressions")
class SeasonViewTest(unittest.TestCase):
    def run_season(self, expression, *, data=None):
        template = training_center.HTML_TEMPLATE
        source = template[
            template.index("    function phaseTone(") : template.index(
                "\n    function renderWeekSelect("
            )
        ]
        script = (
            f"const DATA={json.dumps(data or {})};\n"
            "const state={selectedDate:'2026-08-15',calendarYear:'2026',selectedWeekStart:'2026-08-10'};\n"
            "const TODAY='2026-08-17';\n"
            "const dayLabel=value=>value;\n"
            "const utcDate=value=>new Date(value+'T00:00:00Z');\n"
            "const escapeHtml=value=>String(value??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('\"','&quot;');\n"
            "const eventIsSkipped=event=>Boolean(event?.markers?.skip)||['cancelled','canceled','skipped'].includes(event?.status);\n"
            "const todayAnchorDate=()=>TODAY;\n"
            "const dayByDate=value=>(DATA.days||[]).find(day=>day.date===value);\n"
            + source
            + f"\nconsole.log(JSON.stringify({expression}));"
        )
        result = subprocess.run(
            [shutil.which("node"), "-e", script],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        return json.loads(result.stdout)

    def test_explicit_year_domain_works_without_phases_and_clips_real_phases(self):
        data = {
            "phases": [
                {"name": "Outside", "start_date": "2025-01-01", "end_date": "2025-02-01"},
                {"name": "Build", "start_date": "2025-12-22", "end_date": "2026-01-11"},
                {"name": "Finish", "start_date": "2026-12-28", "end_date": "2027-01-03"},
            ]
        }
        result = self.run_season(
            "[seasonHorizonLayout(DATA.phases,{start_date:'2026-08-10',end_date:'2026-08-16'},state.selectedDate,TODAY,{start_date:'2026-01-01',end_date:'2026-12-31'}),seasonHorizonLayout([],null,null,TODAY,{start_date:'2025-01-01',end_date:'2025-12-31'})]",
            data=data,
        )
        current, empty = result
        self.assertEqual((current["start_date"], current["end_date"]), ("2026-01-01", "2026-12-31"))
        self.assertEqual([phase["name"] for phase in current["phases"]], ["Build", "Finish"])
        self.assertEqual(current["phases"][0]["left"], 0)
        self.assertAlmostEqual(current["phases"][0]["width"], 100 * 11 / 365)
        self.assertAlmostEqual(current["phases"][-1]["left"] + current["phases"][-1]["width"], 100)
        self.assertEqual(empty["phases"], [])
        self.assertEqual(empty["selection"]["width"], 0)
        self.assertIsNone(empty["today_marker"])

    def test_full_season_has_unique_ids_real_race_dates_and_budget_provenance(self):
        week = {
            "start_date": "2026-08-10",
            "end_date": "2026-08-16",
            "phase": "Build",
            "planned_load": {
                "estimated_tss": 441,
                "estimated_tss_min": 400,
                "estimated_tss_max": 480,
                "estimated": False,
                "tss_source": "coach_budget",
                "budget_status": "provisional",
                "budget_ceiling_tss": 500,
                "qualifier": "Coach budget · provisional",
                "note": "Synthetic coaching rationale.",
            },
            "totals": {"estimated_tss": 375.4},
        }
        data = {
            "weeks": [week],
            "phases": [{"name": "Build", "start_date": "2026-01-01", "end_date": "2026-12-31"}],
            "days": [{"date": "2026-08-17", "events": []}],
            "events": [
                {"id": "a", "date": "2026-08-22", "name": "A & B criterium", "priority": "A"},
                {"id": "b", "date": "2026-08-23", "name": "Road race", "status": "tentative"},
                {
                    "id": "c",
                    "date": "2026-09-01",
                    "name": "Skipped event",
                    "markers": {"skip": True},
                },
                {"id": "d", "date": "2027-01-01", "name": "Next year"},
            ],
        }
        full, mini = self.run_season(
            "[renderSeasonHorizon(DATA.weeks[0],{scope:'calendar',year:'2026'}),renderSeasonHorizon(DATA.weeks[0])]",
            data=data,
        )
        self.assertIn('data-season-jump="calendar"', full)
        self.assertIn('id="season-load-summary-calendar"', full)
        self.assertIn('id="season-load-summary-current"', mini)
        self.assertNotIn('id="season-load-summary-current"', full)
        self.assertIn('data-season-start="2026-01-01"', full)
        self.assertIn('data-season-end="2026-12-31"', full)
        self.assertIn("2026 training load", full)
        self.assertIn("central estimate 441 TSS", full)
        self.assertIn("Coach budget · provisional", full)
        self.assertIn("planning ceiling 500 TSS", full)
        self.assertIn("Synthetic coaching rationale", full)
        self.assertIn("coach-budget weeks", full)
        self.assertNotIn("IF 0.55–0.85", full)
        self.assertIn("not measured fitness", full)
        self.assertIn("data-season-race-marker", full)
        self.assertIn("A &amp; B criterium", full)
        self.assertIn("tentative", full)
        self.assertNotIn("Skipped event", full)
        self.assertNotIn("Next year", full)
        self.assertIn("data-season-open-week", full)
        self.assertIn('class="season-today-marker"', full)
        self.assertIn('class="season-selected-range"', full)

    def test_event_without_a_loaded_day_is_visible_but_not_a_dead_navigation_button(self):
        data = {
            "weeks": [],
            "phases": [],
            "days": [],
            "events": [{"date": "2026-10-10", "name": "Future goal", "status": "confirmed"}],
        }
        rendered = self.run_season(
            "renderSeasonHorizon(null,{scope:'calendar',year:'2026'})", data=data
        )
        self.assertIn("Future goal", rendered)
        self.assertIn("No loaded day for this event", rendered)
        self.assertRegex(rendered, r"<button[^>]*data-season-race-marker[^>]* disabled>")
        self.assertNotIn('role="slider"', rendered)

    def test_event_list_stays_open_when_its_focused_event_is_redrawn(self):
        template = training_center.HTML_TEMPLATE
        helpers = template[
            template.index("    function seasonFocusKey(") : template.index(
                "\n    function renderCalendar("
            )
        ]
        render = template[
            template.index("    function renderSeasonOverview(") : template.index(
                "\n    function renderCalendarYearSelect("
            )
        ]
        script = (
            """
const calls=[];
const DATA={weeks:[{start_date:'2026-08-10'}]};
const state={selectedWeekStart:'2026-08-10',calendarYear:'2026'};
let details={open:true};
const active={getAttribute:()=> 'event-2026-08-22'};
const root={contains:node=>node===active,
  set innerHTML(value){details={open:false};document.activeElement=null;},
  querySelector(selector){
    if(selector==='.season-event-list')return details;
    if(selector==='[data-season-jump]')return {};
    return {disabled:false,focus(options){calls.push({open:details.open,options});}};
  }};
const document={activeElement:active,getElementById:()=>root};
function renderSeasonHorizon(){return '';}
function bindSeasonHorizon(){}
"""
            + helpers
            + render
            + "\nrenderSeasonOverview();console.log(JSON.stringify({open:details.open,calls}));"
        )
        result = subprocess.run(
            [shutil.which("node"), "-e", script],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(
            json.loads(result.stdout),
            {"open": True, "calls": [{"open": True, "options": {"preventScroll": True}}]},
        )

    def test_calendar_renders_the_season_chart_before_the_month_grid(self):
        template = training_center.HTML_TEMPLATE
        section = template[
            template.index('<section id="calendar-view"') : template.index(
                '<section id="weeks-view"'
            )
        ]
        self.assertIn("<h2>Season</h2>", section)
        self.assertLess(section.index('id="season-overview"'), section.index('id="calendar-grid"'))
        render = template[
            template.index("    function renderCalendar(") : template.index(
                "\n    function renderCalendarYearSelect("
            )
        ]
        self.assertIn("renderSeasonOverview()", render)
        self.assertIn("function renderSeasonOverview(", render)
        self.assertIn('scope: "calendar"', render)

    def test_navigation_url_keeps_the_selected_season_on_reload(self):
        template = training_center.HTML_TEMPLATE
        source = template[
            template.index("    function syncNavigationUrl(") : template.index(
                "\n    function setView("
            )
        ]
        script = (
            "const replacements=[];\n"
            "const state={view:'calendar',selectedDate:'2026-08-15',calendarYear:'2025'};\n"
            "const window={location:{href:'http://127.0.0.1:8787/training_center.html?view=weeks&date=2026-08-01&keep=yes#chart'},history:{state:{keep:true},replaceState(state,title,url){replacements.push({state,url});window.location.href=url;}}};\n"
            + source
            + "\nsyncNavigationUrl();syncNavigationUrl();state.view='weeks';syncNavigationUrl();console.log(JSON.stringify(replacements));"
        )
        result = subprocess.run(
            [shutil.which("node"), "-e", script],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        first, second = json.loads(result.stdout)
        self.assertIn("view=calendar", first["url"])
        self.assertIn("date=2026-08-15", first["url"])
        self.assertIn("year=2025", first["url"])
        self.assertIn("keep=yes", first["url"])
        self.assertTrue(first["url"].endswith("#chart"))
        self.assertEqual(first["state"], {"keep": True})
        self.assertIn("view=weeks", second["url"])
        self.assertNotIn("year=", second["url"])
        self.assertIn('requested === "season"', template)


if __name__ == "__main__":
    unittest.main()
