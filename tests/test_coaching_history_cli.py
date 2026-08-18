import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from gradient_ascent import cli
from gradient_ascent.config import Config
from gradient_ascent.storage import write_json


def run(root, *args):
    out = io.StringIO()
    with (
        patch("sys.argv", ["gradient-ascent", *args]),
        patch.object(cli, "load_config", return_value=Config(root)),
        redirect_stdout(out),
    ):
        cli.main()
    return json.loads(out.getvalue())


def workspace(root):
    cli._init_workspace(root, force=False)
    write_json(
        root / "plan/weeks.json",
        [{"start_date": "2026-09-01", "end_date": "2026-09-07", "days": {"Tue": "Original ride"}}],
    )


def edit(root, path):
    from gradient_ascent.plan_changes import plan_file_fingerprints

    write_json(
        path,
        {
            "version": 1,
            "change": {
                "idempotency_key": "synthetic-edit",
                "title": "Private title",
                "rationale": "PRIVATE_RATIONALE",
            },
            "expected_files": {"plan/weeks.json": plan_file_fingerprints(root)["plan/weeks.json"]},
            "days": [{"date": "2026-09-01", "workout": "New prescribed ride"}],
        },
    )


class CoachingHistoryCliTest(unittest.TestCase):
    def test_guidance_install_is_explicit_and_keeps_existing_instructions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            workspace(root)
            (root / "AGENTS.md").write_bytes(b"Custom instructions")
            before = (root / "plan/weeks.json").read_bytes()
            initial = run(root, "init-plan-history")
            self.assertNotIn("guidance", initial)
            self.assertEqual((root / "AGENTS.md").read_bytes(), b"Custom instructions")
            installed = run(root, "init-plan-history", "--install-guidance")
            self.assertEqual(installed["id"], initial["id"])
            self.assertTrue(installed["guidance"]["installed"])
            self.assertTrue((root / "AGENTS.md").read_bytes().startswith(b"Custom instructions"))
            repeated = run(root, "init-plan-history", "--install-guidance")
            self.assertFalse(repeated["guidance"]["installed"])
            self.assertEqual((root / "plan/weeks.json").read_bytes(), before)
            self.assertEqual(len(run(root, "plan-history")["changes"]), 1)

    def test_capture_recall_baseline_and_explicit_details(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, draft = Path(tmp) / "workspace", Path(tmp) / "note.json"
            workspace(root)
            write_json(
                draft,
                {
                    "kind": "observation",
                    "idempotency_key": "cli-note",
                    "title": "Private title",
                    "body": "PRIVATE_BODY",
                    "rationale": "PRIVATE_RATIONALE",
                    "scopes": [
                        {"kind": "day", "start_date": "2026-09-01", "end_date": "2026-09-01"}
                    ],
                },
            )
            initialized = run(root, "init-plan-history")
            captured = run(root, "add-coaching-context", "--file", str(draft), "--no-rebuild")
            self.assertTrue(captured["created"])
            self.assertNotIn("PRIVATE_", json.dumps(captured))
            context = run(
                root,
                "coaching-context",
                "--start",
                "2026-09-01",
                "--end",
                "2026-09-01",
                "--kind",
                "observation",
                "--revisions",
            )
            self.assertEqual(context["entries"][0]["body"], "PRIVATE_BODY")
            detailed = run(root, "plan-history", "--details", initialized["id"])
            self.assertIn("after_content", detailed["files"]["plan/weeks.json"])
            self.assertIn(
                "plan/weeks.json", run(root, "plan-history", "--fingerprints")["fingerprints"]
            )
            clean = run(root, "plan-history")["drift"]
            self.assertTrue(clean["baseline_present"])
            self.assertEqual(clean["drifted_count"], 0)
            (root / "plan/weeks.json").write_bytes(b"PRIVATE_MANUAL_EDIT")
            for flags in ((), ("--fingerprints",)):
                output = run(root, "plan-history", *flags)
                self.assertEqual(output["drift"]["drifted_files"], ["plan/weeks.json"])
                self.assertNotIn("PRIVATE_MANUAL_EDIT", json.dumps(output))
            self.assertEqual(run(root, "reconcile-plan-history")["recovery_required"], 0)

    def test_applied_plan_survives_separately_reported_rebuild_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, draft = Path(tmp) / "workspace", Path(tmp) / "edit.json"
            workspace(root)
            edit(root, draft)
            with (
                patch.object(cli, "build_insights", side_effect=ValueError("PRIVATE_PATH")),
                patch("gradient_ascent.training_center.build_training_center") as dashboard,
            ):
                result = run(root, "update-plan", "--file", str(draft))
            self.assertEqual(result["status"], "applied")
            self.assertFalse(result["rebuilt"])
            self.assertEqual(result["rebuild_status"], "failed")
            self.assertEqual(result["rebuild_error"], {"stage": "insights", "type": "ValueError"})
            dashboard.assert_not_called()
            self.assertNotIn("PRIVATE_", json.dumps(result))
            self.assertEqual(
                json.loads((root / "plan/weeks.json").read_text())[0]["days"]["Tue"],
                "New prescribed ride",
            )
            retried = run(root, "update-plan", "--file", str(draft), "--no-rebuild")
            self.assertEqual(retried["id"], result["id"])
            self.assertFalse(retried["created"])

    def test_replaced_root_between_local_stages_never_gets_dashboard(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, draft = Path(tmp) / "workspace", Path(tmp) / "edit.json"
            workspace(root)
            edit(root, draft)

            def replace(*_args):
                root.rename(Path(tmp) / "old")
                workspace(root)
                return {}

            with (
                patch.object(cli, "build_insights", side_effect=replace),
                patch("gradient_ascent.training_center.build_training_center") as dashboard,
            ):
                result = run(root, "update-plan", "--file", str(draft))
            self.assertEqual(result["status"], "applied")
            self.assertEqual(
                result["rebuild_error"], {"stage": "dashboard", "type": "RuntimeError"}
            )
            dashboard.assert_not_called()

    def test_existing_sanctioned_commands_accept_history_metadata_flags(self):
        cases = (
            ["build-plan", "source.csv"],
            ["import-calendar", "source.csv"],
            ["update-tss-budgets", "--file", "budgets.json"],
            ["onboarding-profile"],
            [
                "onboarding-goals",
                "--north-star",
                "a",
                "--goal",
                "b",
                "--why",
                "c",
                "--success",
                "d",
                "--coaching-implication",
                "e",
                "--evidence",
                "f",
            ],
            ["onboarding-event", "--name", "race", "--date", "2026-09-01", "--priority", "A"],
            ["update-goal-files", "--goals-file", "goals.md"],
        )
        for args in cases:
            with (
                self.subTest(command=args[0]),
                patch(
                    "sys.argv",
                    [
                        "gradient-ascent",
                        *args,
                        "--reason",
                        "Reviewed",
                        "--decision-id",
                        "entry-123",
                        "--change-key",
                        "retry-key",
                    ],
                ),
            ):
                parsed = cli._parse_args()
                self.assertEqual(
                    (parsed.reason, parsed.decision_id, parsed.change_key),
                    ("Reviewed", "entry-123", "retry-key"),
                )

    def test_explicit_external_outputs_are_artifacts_and_note_ack_is_compact(self):
        from gradient_ascent.coaching_history import plan_history

        with tempfile.TemporaryDirectory() as tmp:
            root, artifact = Path(tmp) / "workspace", Path(tmp) / "artifact"
            workspace(root)
            source = Path(tmp) / "source.csv"
            source.write_text("Week,Phase,Mon\n2026-09-07 – 2026-09-13,Base,Easy ride\n")
            external_plan = run(
                root, "build-plan", str(source), "--out-dir", str(artifact / "plan")
            )
            external_calendar = run(
                root, "import-calendar", str(source), "--out", str(artifact / "calendar.json")
            )
            self.assertNotIn("history", external_plan)
            self.assertNotIn("history", external_calendar)
            self.assertFalse((artifact / "plan/.history").exists())
            self.assertEqual(plan_history(root), [])
            official = run(
                root,
                "build-plan",
                str(source),
                "--out-dir",
                str(root / "plan"),
                "--reason",
                "PRIVATE_RATIONALE",
                "--change-key",
                "official-import",
            )
            self.assertEqual(official["history"]["status"], "applied")
            self.assertNotIn("PRIVATE_", json.dumps(official))
            note = run(
                root,
                "add-coach-note",
                "--date",
                "2026-09-07",
                "--note",
                "PRIVATE_BODY",
                "--idempotency-key",
                "compact-note",
                "--no-rebuild",
            )
            repeat = run(
                root,
                "add-coach-note",
                "--date",
                "2026-09-07",
                "--note",
                "PRIVATE_BODY",
                "--idempotency-key",
                "compact-note",
                "--no-rebuild",
            )
            self.assertEqual(note["id"], repeat["id"])
            self.assertFalse(repeat["created"])
            self.assertNotIn("PRIVATE_", json.dumps(note))
            self.assertNotIn(str(root), json.dumps(note))

    def test_explicit_recovery_command_reports_restored_without_rebuild(self):
        from gradient_ascent import coaching_history

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            workspace(root)
            original = coaching_history._write_target
            count = 0

            def interrupt(*args):
                nonlocal count
                count += 1
                if count == 2:
                    raise OSError("private failure")
                return original(*args)

            with (
                patch.object(coaching_history, "_write_target", side_effect=interrupt),
                self.assertRaises(RuntimeError),
            ):
                coaching_history.apply_plan_change(
                    root,
                    updates={"plan/goals.md": b"Goal", "plan/weeks.json": b"[]"},
                    request={
                        "idempotency_key": "recover-cli",
                        "title": "Synthetic",
                        "rationale": "Synthetic",
                    },
                )
            identifier = coaching_history.plan_history(root)[-1]["id"]
            restored = run(
                root, "recover-plan-change", identifier, "--action", "restore", "--no-rebuild"
            )
            self.assertEqual(restored["status"], "restored")
            self.assertEqual(restored["rebuild_status"], "skipped")
