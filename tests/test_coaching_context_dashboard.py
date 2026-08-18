from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from gradient_ascent import coaching_history, training_center
from gradient_ascent.cli import _init_workspace
from gradient_ascent.storage import write_json


def scope(kind: str, start: str, end: str | None = None) -> dict:
    return {"kind": kind, "start_date": start, "end_date": end or start}


def entry(identifier: str, kind: str, scopes: list[dict], **extra: object) -> dict:
    return {
        "id": identifier,
        "revision": 1,
        "kind": kind,
        "title": identifier,
        "body": "Useful context",
        "rationale": "Why it matters",
        "scopes": scopes,
        "created_at": "2026-09-01T10:00:00+00:00",
        **extra,
    }


class CoachingContextDashboardTest(unittest.TestCase):
    def test_payload_contains_private_bounded_context_not_plan_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            _init_workspace(root, force=False)
            coaching_history.capture_coaching_entry(
                root,
                {
                    "idempotency_key": "synthetic-note",
                    "kind": "proposal",
                    "scopes": [scope("week", "2026-09-07", "2026-09-13")],
                    "title": "Review the week",
                    "body": "A proposed easier session",
                    "rationale": "Recovery evidence is incomplete.",
                },
            )
            write_json(
                root / "plan/coach_notes.json",
                {
                    "version": 1,
                    "notes": [
                        {
                            "id": "legacy",
                            "date": "2026-09-08",
                            "title": "Older note",
                            "note": "Keep the old observation.",
                        }
                    ],
                },
            )
            coaching_history.initialize_plan_history(root)
            payload = training_center._build_payload(root)[0]
            context = payload["coachingContext"]
            self.assertEqual(context["entries"][0]["kind"], "proposal")
            self.assertEqual(context["legacy_notes"][0]["body"], "Keep the old observation.")
            self.assertNotIn("before_content", json.dumps(context))
            self.assertNotIn("after_content", json.dumps(context))
            self.assertNotIn("goal_measurement.py", json.dumps(context["entries"]))

    def test_invalid_optional_journal_does_not_hide_or_reset_dashboard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            _init_workspace(root, force=False)
            path = root / "plan/.history/journal.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"version":999,"private":"PRIVATE_MARKER"}', encoding="utf-8")
            path.chmod(0o600)
            before = path.read_bytes()
            payload = training_center._build_payload(root)[0]
            self.assertFalse(payload["coachingContext"]["history"]["available"])
            self.assertEqual(
                payload["coachingContext"]["history"]["unavailable_reason"],
                "invalid_coaching_history",
            )
            self.assertNotIn("PRIVATE_MARKER", json.dumps(payload["coachingContext"]))
            self.assertEqual(path.read_bytes(), before)

    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for renderer tests")
    def test_scope_recall_and_applied_labels_are_distinct_and_escaped(self) -> None:
        context = {
            "entries": [
                entry("day-note", "observation", [scope("day", "2026-09-08")]),
                entry(
                    "week-proposal",
                    "proposal",
                    [scope("week", "2026-09-07", "2026-09-13")],
                    body="<script>private</script>",
                    thread_id="safe-thread",
                ),
                entry("month-decision", "decision", [scope("month", "2026-09-01", "2026-09-30")]),
                entry("season-note", "observation", [scope("season", "2026-01-01", "2026-12-31")]),
                entry("unrelated", "observation", [scope("day", "2026-10-05")]),
            ],
            "plan_changes": [
                {
                    "id": "change-one",
                    "kind": "change",
                    "status": "applied",
                    "title": "Workout changed",
                    "reason": "Approved recovery adjustment",
                    "scopes": [scope("day", "2026-09-08")],
                    "date": "2026-09-08T12:00:00+00:00",
                    "files": ["plan/weeks.json"],
                }
            ],
            "legacy_notes": [],
            "summary": {},
            "history": {"available": True, "recovery_required": 0},
        }
        result = self._render(context)
        self.assertIn("day-note", result)
        self.assertIn("season-note", result)
        self.assertNotIn("unrelated", result)
        self.assertIn("Proposal", result)
        self.assertIn("Agreed decision", result)
        self.assertIn("Applied", result)
        self.assertIn("Approved recovery adjustment", result)
        self.assertIn("&lt;script&gt;private&lt;/script&gt;", result)
        self.assertNotIn("<script>private", result)
        self.assertIn("codex://threads/safe-thread", result)
        self.assertIn("coaching-context --start 2026-09-08 --end 2026-09-08", result)

    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for renderer tests")
    def test_unavailable_history_is_not_reported_as_empty_success(self) -> None:
        result = self._render(
            {
                "entries": [],
                "plan_changes": [],
                "legacy_notes": [],
                "summary": {},
                "history": {"available": False, "unavailable_reason": "invalid_coaching_history"},
            }
        )
        self.assertIn("Coaching history unavailable", result)
        self.assertNotIn("No coaching context saved", result)

    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for renderer tests")
    def test_truncated_global_history_does_not_claim_an_older_period_is_empty(self) -> None:
        context = {
            "entries": [
                entry("recent-note", "observation", [scope("day", "2026-10-05")])
            ],
            "plan_changes": [],
            "legacy_notes": [],
            "summary": {"truncated": True},
            "history": {"available": True},
        }
        result = self._render(context)
        self.assertIn("Not in loaded history", result)
        self.assertIn("Recall this period for older context", result)
        self.assertNotIn("No saved context", result)
        self.assertNotIn("No coaching context saved", result)
        self.assertNotIn("recent-note", result)
        self.assertIn("coaching-context --start 2026-09-08 --end 2026-09-08", result)

        context["summary"]["truncated"] = False
        complete = self._render(context)
        self.assertIn("No saved context", complete)
        self.assertIn("No coaching context saved for this period yet", complete)
        self.assertNotIn("Not in loaded history", complete)

    def test_all_view_levels_and_coach_prompt_use_the_context_component(self) -> None:
        html = training_center.HTML_TEMPLATE
        for label in (
            "Day coaching context",
            "Week coaching context",
            "Month coaching context",
            "Season coaching context",
        ):
            self.assertIn(label, html)
        self.assertIn(
            "Recall saved coaching context and official plan history before advising", html
        )
        self.assertIn("Do not apply plan changes without my approval", html)
        self.assertIn("Start with coaching-context --start ${start} --end ${end}", html)

    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for renderer tests")
    def test_open_context_is_restored_without_opening_unrelated_periods(self) -> None:
        html = training_center.HTML_TEMPLATE
        start = html.index("    function coachingDisclosureState(")
        end = html.index("\n    function coachNotesForDay(", start)
        script = (
            html[start:end]
            + "\nconst old={querySelectorAll:()=>[{dataset:{coachingContext:'week-a'}}]};const next=[{dataset:{coachingContext:'week-a'},open:false},{dataset:{coachingContext:'week-b'},open:false}];restoreCoachingDisclosures({querySelectorAll:()=>next},coachingDisclosureState(old));console.log(JSON.stringify(next.map(item=>item.open)));"
        )
        result = subprocess.run(
            [shutil.which("node"), "-e", script], check=True, text=True, capture_output=True
        )
        self.assertEqual(json.loads(result.stdout), [True, False])

    def _render(self, context: dict) -> str:
        html = training_center.HTML_TEMPLATE
        start = html.index("    function coachingScopeOverlaps(")
        end = html.index("\n    function coachNotesForDay(", start)
        script = "const DATA=" + json.dumps({"coachingContext": context}) + ";\n"
        script += "function escapeHtml(value){return String(value ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('\"','&quot;').replaceAll(\"'\",'&#39;');}\n"
        script += "function codexThreadUrl(prompt){return 'codex://new?prompt='+encodeURIComponent(prompt);}\n"
        script += html[start:end]
        script += "\nconsole.log(renderCoachingContext('2026-09-08','2026-09-08',{key:'synthetic',label:'Day context'}));"
        result = subprocess.run(
            [shutil.which("node"), "-e", script], check=True, text=True, capture_output=True
        )
        return result.stdout


if __name__ == "__main__":
    unittest.main()
