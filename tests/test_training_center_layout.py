import json
import re
import shutil
import subprocess
import unittest
from html.parser import HTMLParser

from gradient_ascent import training_center


class _DetailsParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.details = []

    def handle_starttag(self, tag, attrs):
        if tag == "details":
            self.details.append(dict(attrs))


class TrainingCenterLayoutTest(unittest.TestCase):
    def _rules(self, selector):
        return [
            dict(
                (name.strip(), value.strip())
                for declaration in match.split(";")
                if ":" in declaration
                for name, value in [declaration.split(":", 1)]
            )
            for match in re.findall(
                rf"(?m)^\s*{re.escape(selector)}\s*\{{([^{{}}]*)\}}",
                training_center.HTML_TEMPLATE,
            )
        ]

    def _functions(self, first, following):
        html = training_center.HTML_TEMPLATE
        return html[
            html.index(f"    function {first}(") : html.index(f"\n    function {following}(")
        ]

    def _run(self, source):
        result = subprocess.run(
            [shutil.which("node"), "-e", source],
            text=True,
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_primary_header_columns_and_offsets_are_explicit_at_both_breakpoints(self):
        desktop, compact = self._rules("body.primary-shell .topbar")
        self.assertEqual(desktop["height"], "var(--coach-header-height)")
        self.assertEqual(desktop["margin-bottom"], "0")
        self.assertEqual(desktop["grid-template-areas"], '". tabs actions"')
        self.assertEqual(
            desktop["grid-template-columns"],
            "minmax(0, 1fr) auto minmax(max-content, 1fr)",
        )
        self.assertEqual(compact["height"], "auto")
        self.assertEqual(compact["grid-template-columns"], "minmax(0, 1fr) max-content")
        self.assertEqual(
            " ".join(compact["grid-template-areas"].split()),
            '"brand actions" "tabs tabs"',
        )
        self.assertEqual(
            self._rules("body.primary-shell .workspace")[0]["margin-top"],
            "calc(-1 * var(--coach-header-height))",
        )
        self.assertEqual(
            self._rules("body.primary-shell .center-stage")[0]["padding"],
            "var(--coach-header-height) 0 0",
        )
        self.assertEqual(
            self._rules("body.primary-shell .topbar-actions")[0]["flex-wrap"], "nowrap"
        )

    def test_sidebar_session_and_week_events_use_plain_backgrounds(self):
        session = self._rules("body.primary-shell .coach-rail .session-card")[-1]
        self.assertEqual(session["background"], "transparent")
        event = self._rules(".week-status-events .event-chip")[-1]
        for key, value in {
            "background": "transparent",
            "border": "0",
            "border-radius": "0",
            "padding": "0",
            "text-transform": "none",
        }.items():
            self.assertEqual(event[key], value)

    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for renderer tests")
    def test_week_totals_are_closed_by_default_and_keep_all_values_and_provenance(self):
        week = {
            "start_date": "2026-09-07",
            "planned_load": {
                "hours_label": "6–10h",
                "duration_source": "source_weekly_hours",
                "tss_value_label": "0 TSS",
                "qualifier": "Coach budget · provisional",
                "note": "<script>not markup</script>",
                "budget_ceiling_label": "50 TSS",
            },
            "totals": {"activity_count": 1},
            "actual_hours_label": "1.5h",
            "estimated_tss_label": "40 TSS",
            "tss_qualifier": "Calculated · 79.3% power coverage",
            "tss_description": 'Recorded <power> & supplied "FTP"',
        }
        source = "const document={activeElement:null};\n"
        source += "function escapeHtml(v){return String(v??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('\"','&quot;');}\n"
        source += self._functions("weekTotalsDisclosureState", "renderWeek")
        source += f"\nconst week={json.dumps(week)};console.log(JSON.stringify(renderWeekLoadOverview(week)));"
        markup = self._run(source)
        parser = _DetailsParser()
        parser.feed(markup)
        self.assertEqual(len(parser.details), 1)
        self.assertNotIn("open", parser.details[0])
        self.assertEqual(parser.details[0]["data-week-totals"], "2026-09-07")
        for value in (
            "Week totals",
            "Scheduled hours",
            "6–10h",
            "TSS budget",
            "0 TSS",
            "ceiling 50 TSS",
            "Recorded hours",
            "1.5h",
            "Recorded TSS",
            "40 TSS",
            "79.3% power coverage",
            "&lt;script&gt;not markup&lt;/script&gt;",
            "Recorded &lt;power&gt; &amp; supplied &quot;FTP&quot;",
        ):
            self.assertIn(value, markup)
        self.assertNotIn("<script>", markup)

    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for renderer tests")
    def test_week_redraw_keeps_only_the_current_totals_disclosure_and_focus(self):
        source = """
const DATA={weeks:[
  {start_date:'2026-09-07',end_date:'2026-09-13',days:[]},
  {start_date:'2026-09-14',end_date:'2026-09-20',days:[]}
]};
const state={selectedWeekStart:'2026-09-07'};
const calls=[];
const eventFocusRestores=[];
const select={value:''};
let details=null;
let eventDetails={open:false};
function nextDetails(week,open){
  const summary={focus(options){calls.push(options);document.activeElement=this;}};
  return {dataset:{weekTotals:week},open,querySelector(){return summary;}};
}
const root={
  querySelector(selector){
    if(selector==='.season-event-list') return eventDetails;
    if(selector==='details[data-week-totals]') return details;
    if(selector==='details[data-week-totals] > summary') return details?.querySelector('summary');
    return null;
  },
  set innerHTML(markup){
    eventDetails={open:false};
    const tag=markup.match(/<details[^>]*data-week-totals="([^"]+)"[^>]*>/);
    details=tag?nextDetails(tag[1],/\\sopen(?:\\s|>)/.test(tag[0])):null;
    document.activeElement=null;
  }
};
const document={activeElement:null,getElementById(id){return id==='week-list'?root:select;}};
const escapeHtml=value=>String(value??'');
const seasonFocusKey=()=>document.activeElement?.seasonFocus||null;
const coachingDisclosureState=()=>new Set();
const weekStatusForToday=()=>({label:'Recorded',status:'recorded_history'});
const weekStatusCopy=()=>'';
const renderSeasonHorizon=()=>'';
const renderCoachingContext=()=>'';
const weekDistanceLabel=()=>'';
function hydrateWeekActivityDetails(){}
function updateWeekNavButtons(){}
function renderWeekDay(){}
function bindWeekCards(){}
function bindSeasonHorizon(){}
function restoreCoachingDisclosures(){}
function restoreSeasonFocus(root,key){if(key)eventFocusRestores.push({key,open:eventDetails.open});}
function requestAnimationFrame(){}
function syncWeekIntervalLists(){}
"""
        source += self._functions("renderWeekBudgetDetail", "bindSeasonHorizon")
        source += """
renderWeek();
const initial=details.open;
details.open=true;
document.activeElement=details.querySelector('summary');
renderWeek();
const same={open:details.open,focused:document.activeElement===details.querySelector('summary'),calls:[...calls]};
calls.length=0;
document.activeElement={unrelated:true};
renderWeek();
const unrelated={open:details.open,calls:[...calls]};
document.activeElement=details.querySelector('summary');
state.selectedWeekStart='2026-09-14';
renderWeek();
const next={open:details.open,calls:[...calls]};
eventDetails.open=true;
document.activeElement={seasonFocus:'event-2026-09-20'};
renderWeek();
console.log(JSON.stringify({initial,same,unrelated,next,eventFocusRestores}));
"""
        result = self._run(source)
        self.assertFalse(result["initial"])
        self.assertEqual(
            result["same"],
            {"open": True, "focused": True, "calls": [{"preventScroll": True}]},
        )
        self.assertEqual(result["unrelated"], {"open": True, "calls": []})
        self.assertEqual(result["next"], {"open": False, "calls": []})
        self.assertEqual(result["eventFocusRestores"], [{"key": "event-2026-09-20", "open": True}])


if __name__ == "__main__":
    unittest.main()
