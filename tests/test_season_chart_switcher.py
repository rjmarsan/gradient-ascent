import copy
import json
import shutil
import subprocess
import unittest
from datetime import date, timedelta

from gradient_ascent import training_center
from gradient_ascent.training_load import build_training_load
from gradient_ascent.training_load_projection import build_training_load_projection


TODAY = date(2026, 1, 5)


def chart_data():
    recorded = build_training_load(
        [
            {"date": "2026-01-01", "totals": {"estimated_tss": 100}},
            {"date": TODAY.isoformat(), "totals": {"estimated_tss": 50}},
        ],
        as_of=TODAY,
    )
    targets = [
        {
            "date": (TODAY + timedelta(days=index)).isoformat(),
            "target_tss": score,
            "tss_source": "coach_budget_allocation",
            "status": "provisional",
        }
        for index, score in enumerate((50, 0, 75, 25, 100, 50), start=1)
    ]
    return {
        "weeks": [
            {
                "start_date": "2026-01-05",
                "end_date": "2026-01-11",
                "phase": "Synthetic build",
                "planned_load": {
                    "estimated_tss": 350,
                    "estimated_tss_min": 330,
                    "estimated_tss_max": 370,
                    "tss_source": "coach_budget",
                    "budget_status": "provisional",
                    "qualifier": "Coach budget · provisional",
                    "note": "A synthetic explicit budget.",
                },
                "totals": {"estimated_tss": 50},
            }
        ],
        "days": [
            {"date": (date(2026, 1, 1) + timedelta(days=index)).isoformat()} for index in range(11)
        ],
        "phases": [
            {"name": "Synthetic build", "start_date": "2026-01-01", "end_date": "2026-12-31"}
        ],
        "events": [{"date": "2026-01-10", "name": "Synthetic race", "status": "tentative"}],
        "trainingLoad": recorded,
        "trainingLoadProjection": build_training_load_projection(
            recorded, targets, as_of=TODAY, end=date(2026, 1, 11)
        ),
    }


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for chart regressions")
class SeasonChartSwitcherTest(unittest.TestCase):
    def node(self, source):
        result = subprocess.run(
            [shutil.which("node"), "-e", source],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def run_chart(self, expression, *, data=None, mode="plan", today=TODAY.isoformat()):
        template = training_center.HTML_TEMPLATE
        source = template[
            template.index("    function phaseTone(") : template.index(
                "\n    function renderWeekSelect("
            )
        ]
        return self.node(
            f"const DATA={json.dumps(data if data is not None else chart_data())};\n"
            f"const state={{seasonChart:{json.dumps(mode)},selectedDate:'2026-01-08',calendarYear:'2026',selectedWeekStart:'2026-01-05'}};\n"
            f"const TODAY={json.dumps(today)};\n"
            "const dayLabel=value=>value;\n"
            "const utcDate=value=>new Date(value+'T00:00:00Z');\n"
            "const escapeHtml=value=>String(value??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('\"','&quot;');\n"
            "const eventIsSkipped=event=>Boolean(event?.markers?.skip)||['cancelled','canceled','skipped'].includes(event?.status);\n"
            "const todayAnchorDate=()=>TODAY;\n"
            "const dayByDate=value=>(DATA.days||[]).find(day=>day.date===value);\n"
            + source
            + f"\nconsole.log(JSON.stringify({expression}));"
        )

    def test_plan_is_the_default_data_backed_chart_with_accessible_switcher(self):
        rendered = self.run_chart(
            "renderSeasonHorizon(DATA.weeks[0],{scope:'calendar',year:'2026'})", mode=None
        )
        self.assertIn('role="group" aria-label="Season chart"', rendered)
        self.assertRegex(rendered, r'data-season-chart="plan"[^>]*aria-pressed="true"')
        self.assertRegex(rendered, r'data-season-chart="load"[^>]*aria-pressed="false"')
        self.assertIn('class="season-target-line"', rendered)
        self.assertIn('class="season-recorded-area"', rendered)
        self.assertIn("TSS/week", rendered)
        self.assertIn("Coach budget · provisional", rendered)
        self.assertNotIn('class="season-ctl-line"', rendered)
        self.assertNotIn('class="season-atl-line"', rendered)
        self.assertIn('class="season-today-marker"', rendered)
        self.assertIn('class="season-selected-range"', rendered)
        self.assertIn("Synthetic race", rendered)

    def test_load_mode_keeps_actuals_and_separate_dashed_future_scenario(self):
        rendered = self.run_chart(
            "renderSeasonHorizon(DATA.weeks[0],{scope:'calendar',year:'2026'})", mode="load"
        )
        for text in (
            'class="season-ctl-line"',
            'class="season-atl-line"',
            'class="season-projected-ctl-line"',
            'class="season-projected-atl-line"',
            "TSS/day",
            "Conditional projection",
            "Starts tomorrow",
            "remaining today’s plan excluded",
            "Provisional scenario",
        ):
            self.assertIn(text, rendered)
        self.assertNotIn('class="season-target-line"', rendered)
        self.assertRegex(rendered, r'data-season-chart="load"[^>]*aria-pressed="true"')
        self.assertIn('id="season-load-summary-calendar"', rendered)
        self.assertNotIn('id="season-load-summary-current"', rendered)

    def test_projection_uses_exact_values_and_retains_zero_without_mutation(self):
        data = chart_data()
        before = copy.deepcopy(data)
        result = self.run_chart(
            "performanceProjectionSeries(DATA.trainingLoadProjection,{start_date:'2026-01-05',end_date:'2026-01-11'},TODAY,DATA.trainingLoad)",
            data=data,
        )
        self.assertEqual(
            [row["date"] for row in result["rows"]],
            [row["date"] for row in data["trainingLoadProjection"]["rows"]],
        )
        self.assertEqual(result["rows"][1]["target_tss"], 0)
        self.assertEqual(result["rows"][2]["ctl"], data["trainingLoadProjection"]["rows"][2]["ctl"])
        self.assertEqual(result["runs"][0][0]["date"], TODAY.isoformat())
        self.assertEqual(result["rows"][0]["left"], 100 / 7)
        self.assertEqual(result["through_date"], "2026-01-11")
        self.assertEqual(data, before)

    def test_stale_conflicting_or_noncontiguous_projection_is_never_drawn(self):
        original = chart_data()
        invalid = []
        duplicate = copy.deepcopy(original)
        duplicate["trainingLoadProjection"]["rows"].insert(
            1, copy.deepcopy(duplicate["trainingLoadProjection"]["rows"][0])
        )
        invalid.append(duplicate)
        gap = copy.deepcopy(original)
        del gap["trainingLoadProjection"]["rows"][1]
        invalid.append(gap)
        anchor = copy.deepcopy(original)
        anchor["trainingLoadProjection"]["anchor"]["ctl"] += 1
        invalid.append(anchor)
        wrong_source = copy.deepcopy(original)
        wrong_source["trainingLoadProjection"]["rows"][0]["tss_source"] = "weekly_hours_budget"
        invalid.append(wrong_source)
        for data in invalid:
            with self.subTest(data=data["trainingLoadProjection"]["rows"][0]):
                result = self.run_chart(
                    "performanceProjectionSeries(DATA.trainingLoadProjection,{start_date:'2026-01-01',end_date:'2026-12-31'},TODAY,DATA.trainingLoad)",
                    data=data,
                )
                self.assertEqual(result["rows"], [])
                self.assertEqual(result["runs"], [])
        stale = self.run_chart(
            "renderSeasonHorizon(DATA.weeks[0])", data=original, mode="load", today="2026-01-06"
        )
        self.assertNotIn('class="season-projected-ctl-line"', stale)
        self.assertIn("Rebuild local insights", stale)

    def test_missing_day_stop_is_visible_and_future_dates_are_not_filled(self):
        data = chart_data()
        first = data["trainingLoadProjection"]["rows"][0]
        data["trainingLoadProjection"] = build_training_load_projection(
            data["trainingLoad"],
            [{key: first[key] for key in ("date", "target_tss", "tss_source", "status")}],
            as_of=TODAY,
            end=date(2026, 1, 11),
        )
        result, rendered = self.run_chart(
            "[performanceProjectionSeries(DATA.trainingLoadProjection,{start_date:'2026-01-01',end_date:'2026-12-31'},TODAY,DATA.trainingLoad),renderSeasonHorizon(DATA.weeks[0])]",
            data=data,
            mode="load",
        )
        self.assertEqual([row["date"] for row in result["rows"]], ["2026-01-06"])
        self.assertIn("2026-01-07", rendered)
        self.assertIn("daily TSS target", rendered)

    def test_single_projected_day_at_year_boundary_has_visible_markers(self):
        today = date(2026, 12, 31)
        recorded = build_training_load(
            [{"date": today.isoformat(), "totals": {"estimated_tss": 84}}], as_of=today
        )
        data = {
            "trainingLoad": recorded,
            "trainingLoadProjection": build_training_load_projection(
                recorded,
                [
                    {
                        "date": "2027-01-01",
                        "target_tss": 100,
                        "tss_source": "source_target",
                        "status": "prescribed",
                    }
                ],
                as_of=today,
                end=date(2027, 1, 1),
            ),
        }
        rendered = self.run_chart(
            "renderPerformanceLoadChart(performanceLoadSeries(DATA.trainingLoad,{start_date:'2027-01-01',end_date:'2027-12-31'},TODAY),performanceProjectionSeries(DATA.trainingLoadProjection,{start_date:'2027-01-01',end_date:'2027-12-31'},TODAY,DATA.trainingLoad))",
            data=data,
            today=today.isoformat(),
        )
        self.assertIn('class="season-projected-ctl-dot"', rendered)
        self.assertIn('class="season-projected-atl-dot"', rendered)
        self.assertIn("2027-01-01", rendered)
        self.assertNotIn("No recorded CTL/ATL data in this season", rendered)

    def test_preference_is_allowlisted_and_switching_preserves_navigation_and_focus(self):
        template = training_center.HTML_TEMPLATE
        initial = template[
            template.index("    function initialSeasonChart(") : template.index(
                "\n    function initialRideSidebarOpen("
            )
        ]
        actions = template[
            template.index("    function syncNavigationUrl(") : template.index(
                "\n    function setView("
            )
        ]
        result = self.node(
            """
const SEASON_CHART_STORAGE_KEY='gradient-ascent-season-chart';
let stored='unexpected';
const calls=[];
const window={location:{href:'http://127.0.0.1:8787/training_center.html?view=weeks&date=2026-01-08',search:'?chart=nope'},localStorage:{getItem:()=>stored,setItem:(key,value)=>{stored=value;calls.push(['store',key,value]);}},history:{state:null,replaceState:(_state,_title,url)=>{window.location.href=url;calls.push(['url',url]);}}};
const state={view:'weeks',selectedDate:'2026-01-08',selectedWeekStart:'2026-01-05',calendarYear:'2026',seasonChart:'plan'};
const document={querySelector:selector=>({selector})};
const renderSeasonOverview=()=>calls.push(['season']);
const renderWeek=()=>calls.push(['week']);
const restoreSeasonFocus=(root,key)=>calls.push(['focus',root.selector,key]);
"""
            + initial
            + actions
            + """
const defaults=[initialSeasonChart()];
stored='load';defaults.push(initialSeasonChart());
window.location.search='?chart=plan';defaults.push(initialSeasonChart());
setSeasonChart('load','calendar');setSeasonChart('invalid','current');
console.log(JSON.stringify({defaults,state,stored,calls}));
"""
        )
        self.assertEqual(result["defaults"], ["plan", "load", "plan"])
        self.assertEqual(result["state"]["selectedDate"], "2026-01-08")
        self.assertEqual(result["state"]["selectedWeekStart"], "2026-01-05")
        self.assertEqual(result["state"]["seasonChart"], "load")
        self.assertEqual(result["stored"], "load")
        self.assertEqual(sum(call[0] == "season" for call in result["calls"]), 1)
        self.assertEqual(sum(call[0] == "week" for call in result["calls"]), 1)
        self.assertIn(["focus", '[data-season-jump="calendar"]', "chart-load"], result["calls"])
        self.assertTrue(
            any(call[0] == "url" and "chart=load" in call[1] for call in result["calls"])
        )
