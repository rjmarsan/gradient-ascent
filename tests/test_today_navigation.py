import json
import shutil
import subprocess
import unittest

from gradient_ascent import training_center


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for browser date tests")
class TodayNavigationTest(unittest.TestCase):
    def _functions(self, first: str, following: str) -> str:
        html = training_center.HTML_TEMPLATE
        start = html.index(f"    function {first}(")
        end = html.index(f"\n    {following}", start)
        return html[start:end]

    def _run(self, script: str) -> dict:
        result = subprocess.run(
            [shutil.which("node"), "-e", script],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        return json.loads(result.stdout)

    def _date_harness(self) -> str:
        return (
            """
const RealDate = Date;
let now = '2026-08-17T06:59:59Z';
globalThis.Date = class extends RealDate {
  constructor(...args) { super(...(args.length ? args : [now])); }
  static now() { return new RealDate(now).getTime(); }
};
const ATHLETE_TIME_ZONE = 'America/Los_Angeles';
let TODAY = isoDateInTimeZone(new Date(), ATHLETE_TIME_ZONE);
const DATA = {days: [{date:'2026-08-16'}, {date:'2026-08-17'}]};
const state = {selectedDate:'2020-01-01', selectedWeekStart:'2019-12-30'};
const calls = [];
const label = {textContent:''};
const tab = {dataset:{view:'today'}, title:'', listeners:[],
  querySelector() { return label; },
  addEventListener(type, callback) { this.listeners.push(callback); }};
const document = {querySelector() { return tab; },
  querySelectorAll() { return [tab]; }};
function renderCalendar() { calls.push('calendar'); }
function renderWeek() { calls.push('week'); }
function renderCoachRail() { calls.push('coach'); }
function renderTodayDashboard() { calls.push('today'); }
function renderMonthRail() { calls.push('month'); }
function renderRideSidebar() { calls.push('ride'); }
function selectDate(date) { calls.push(['select', date]); }
function setView(view) { calls.push(['view', view]); }
"""
            + self._functions("todayAnchorDate", "function escapeHtml(")
            + self._functions("renderTodayTabLabel", "function renderCalendar(")
        )

    def test_midnight_refresh_preserves_navigation_and_does_not_rebind_tabs(self) -> None:
        self.assertTrue("let TODAY = isoDateInTimeZone(" in training_center.HTML_TEMPLATE)
        result = self._run(
            self._date_harness()
            + """
renderTabs();
const before = {...state};
const unchanged = refreshCurrentDate();
const unchangedCalls = [...calls];
now = '2026-08-17T07:00:00Z';
const changed = refreshCurrentDate();
const changedCalls = [...calls];
const repeated = refreshCurrentDate();
console.log(JSON.stringify({unchanged, unchangedCalls, changed, changedCalls,
  repeated, today:TODAY, before, state, label:label.textContent,
  listeners:tab.listeners.length, finalCalls:calls}));
"""
        )
        self.assertFalse(result["unchanged"])
        self.assertEqual(result["unchangedCalls"], [])
        self.assertTrue(result["changed"])
        self.assertFalse(result["repeated"])
        self.assertEqual(result["today"], "2026-08-17")
        self.assertEqual(result["state"], result["before"])
        self.assertEqual(result["label"], "Today")
        self.assertEqual(result["listeners"], 1)
        self.assertEqual(
            result["changedCalls"], ["calendar", "week", "coach", "today", "month", "ride"]
        )
        self.assertEqual(result["finalCalls"], result["changedCalls"])

    def test_today_tab_refreshes_clock_before_choosing_loaded_day(self) -> None:
        result = self._run(
            self._date_harness()
            + """
renderTabs();
now = '2026-08-17T07:00:00Z';
tab.listeners[0]();
DATA.days = [{date:'2026-08-15'}];
const past = [todayAnchorDate(), todayAnchorLabel()];
DATA.days = [{date:'2026-08-20'}];
const future = [todayAnchorDate(), todayAnchorLabel()];
DATA.days = [];
const empty = [todayAnchorDate(), todayAnchorLabel()];
console.log(JSON.stringify({calls, past, future, empty}));
"""
        )
        self.assertIn(["select", "2026-08-17"], result["calls"])
        self.assertEqual(result["past"], ["2026-08-15", "Latest"])
        self.assertEqual(result["future"], ["2026-08-20", "Next"])
        self.assertEqual(result["empty"], ["2026-08-17", "Today"])

    def test_lifecycle_refreshes_on_focus_visible_and_a_modest_timer(self) -> None:
        result = self._run(
            """
const callbacks = {}, timers = [];
let refreshes = 0;
const window = {
  addEventListener(name, callback) { callbacks[name] = callback; },
  setInterval(callback, delay) { timers.push({callback, delay}); }
};
const document = {visibilityState:'hidden',
  addEventListener(name, callback) { callbacks[name] = callback; }};
function refreshCurrentDate() { refreshes += 1; return false; }
"""
            + self._functions("bindCurrentDateLifecycle", "function bindGlobalControls(")
            + """
bindCurrentDateLifecycle();
callbacks.visibilitychange();
const hidden = refreshes;
document.visibilityState = 'visible';
callbacks.visibilitychange();
callbacks.focus();
timers[0].callback();
console.log(JSON.stringify({hidden, refreshes, delays:timers.map(item=>item.delay)}));
"""
        )
        self.assertEqual(result["hidden"], 0)
        self.assertEqual(result["refreshes"], 3)
        self.assertEqual(result["delays"], [60000])
        controls = self._functions("bindGlobalControls", "async function loadRuntimeState(")
        self.assertIn(
            '"jump-to-today").addEventListener("click", () => {\n        refreshCurrentDate();',
            controls,
        )
        self.assertTrue("      bindCurrentDateLifecycle();" in training_center.HTML_TEMPLATE)

    def test_background_week_redraw_preserves_only_the_focused_horizon_control(self) -> None:
        result = self._run(
            """
const DATA = {weeks:[{start_date:'2026-08-17',end_date:'2026-08-23',days:[]}]};
const state = {selectedWeekStart:'2026-08-17'};
const calls = [];
let missing = false, disabled = false;
const select = {value:'2026-08-17'};
const root = {
  contains(node) { return node?.scope === 'current'; },
  querySelector(selector) {
    if (missing) return null;
    return {disabled, focus(options) { calls.push({selector, options}); }};
  },
  set innerHTML(value) { document.activeElement = null; }
};
const document = {
  activeElement:null,
  getElementById(id) { return id === 'week-list' ? root : select; },
};
const escapeHtml = value => String(value ?? '');
const renderSeasonHorizon = () => '';
const weekDistanceLabel = () => '';
function hydrateWeekActivityDetails() {}
function updateWeekNavButtons() {}
function renderWeekDay() {}
function bindWeekCards() {}
function bindSeasonHorizon() {}
function requestAnimationFrame() {}
function syncWeekIntervalLists() {}
"""
            + self._functions("seasonFocusKey", "function renderCalendar(")
            + self._functions("renderWeek", "function bindSeasonHorizon(")
            + """
const results = [];
for (const scenario of [
  {key:'today'},
  {key:'track'},
  {key:null},
  {key:'today', missing:true},
  {key:'today', disabled:true},
  {key:'track', scope:'calendar'}
]) {
  calls.length = 0;
  missing = Boolean(scenario.missing);
  disabled = Boolean(scenario.disabled);
  document.activeElement = {scope:scenario.scope || 'current',
    getAttribute(name) { return name === 'data-season-focus' ? scenario.key : null; }};
  renderWeek();
  results.push([...calls]);
}
console.log(JSON.stringify({results}));
"""
        )
        today, slider, unrelated, missing, disabled, other_chart = result["results"]
        self.assertEqual(
            today, [{"selector": '[data-season-focus="today"]', "options": {"preventScroll": True}}]
        )
        self.assertEqual(
            slider,
            [{"selector": '[data-season-focus="track"]', "options": {"preventScroll": True}}],
        )
        self.assertEqual((unrelated, missing, disabled, other_chart), ([], [], [], []))
