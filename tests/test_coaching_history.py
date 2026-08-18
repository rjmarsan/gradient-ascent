import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def request(key="change-one"):
    return {
        "idempotency_key": key,
        "title": "Synthetic plan change",
        "rationale": "Explicit synthetic decision.",
    }


def entry(key="note-one", **changes):
    return {
        **request(key),
        "kind": "observation",
        "body": "Synthetic observation.",
        "scopes": [{"kind": "week", "start_date": "2026-09-01", "end_date": "2026-09-07"}],
        **changes,
    }


def setup(root):
    root.mkdir(mode=0o700)
    (root / "plan").mkdir(mode=0o700)
    (root / "plan/weeks.json").write_bytes(b"[]\n")


class CoachingHistoryTest(unittest.TestCase):
    def test_drift_without_baseline_is_unknown_and_read_only(self):
        from gradient_ascent import coaching_history as history

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            setup(root)
            result = history.plan_history_drift(root)
            self.assertFalse(result["baseline_present"])
            self.assertEqual(result["checked_files"], 0)
            self.assertEqual(result["drifted_files"], [])
            self.assertEqual(set(result["unknown_files"]), history.ALLOWED_PLAN_FILES)
            self.assertFalse((root / "plan/.history").exists())
            with mock.patch.object(history, "_secure_files_supported", return_value=False):
                self.assertEqual(history.plan_history_drift(root), result)

    def test_drift_baseline_manual_edit_creation_and_applied_head(self):
        from gradient_ascent import coaching_history as history

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            setup(root)
            history.initialize_plan_history(root)
            journal = root / "plan/.history/journal.json"
            unchanged = journal.read_bytes()
            clean = history.plan_history_drift(root)
            self.assertTrue(clean["baseline_present"])
            self.assertEqual(clean["checked_files"], len(history.ALLOWED_PLAN_FILES))
            self.assertEqual(clean["drifted_count"], 0)
            (root / "plan/weeks.json").write_bytes(b"Manual edit")
            (root / "calendar.json").write_bytes(b"{}")
            result = history.plan_history_drift(root)
            self.assertEqual(result["drifted_files"], ["calendar.json", "plan/weeks.json"])
            self.assertEqual(result["drifted_count"], 2)
            self.assertEqual(journal.read_bytes(), unchanged)
            history.apply_plan_change(
                root, updates={"plan/weeks.json": b"Reviewed edit"}, request=request()
            )
            self.assertEqual(history.plan_history_drift(root)["drifted_files"], ["calendar.json"])

    def test_drift_pending_is_excluded_and_restore_uses_before_snapshot(self):
        from gradient_ascent import coaching_history as history

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            setup(root)
            history.initialize_plan_history(root)
            (root / "plan/weeks.json").write_bytes(b"Reviewed starting state")
            write = history._write_target

            def interrupt(directory, name, body):
                if name == "plan/weeks.json":
                    raise OSError("interrupted")
                return write(directory, name, body)

            with (
                mock.patch.object(history, "_write_target", side_effect=interrupt),
                self.assertRaises(RuntimeError),
            ):
                history.apply_plan_change(
                    root,
                    updates={"plan/goals.md": b"Goal", "plan/weeks.json": b"After"},
                    request=request(),
                )
            identifier = history.plan_history(root)[-1]["id"]
            pending = history.plan_history_drift(root)
            self.assertEqual(
                pending["excluded_unresolved_files"], ["plan/goals.md", "plan/weeks.json"]
            )
            self.assertEqual(pending["drifted_files"], [])
            history.recover_plan_change(root, identifier, action="restore")
            self.assertEqual((root / "plan/weeks.json").read_bytes(), b"Reviewed starting state")
            self.assertEqual(history.plan_history_drift(root)["drifted_files"], [])
            (root / "plan/weeks.json").write_bytes(b"[]\n")
            self.assertEqual(history.plan_history_drift(root)["drifted_files"], ["plan/weeks.json"])

    def test_applied_without_baseline_only_establishes_its_own_file_head(self):
        from gradient_ascent import coaching_history as history

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            setup(root)
            history.apply_plan_change(root, updates={"plan/goals.md": b"Goal"}, request=request())
            result = history.plan_history_drift(root)
            self.assertFalse(result["baseline_present"])
            self.assertEqual(result["checked_files"], 1)
            self.assertNotIn("plan/goals.md", result["unknown_files"])
            self.assertIn("plan/weeks.json", result["unknown_files"])

    def test_first_baseline_requires_unresolved_change_to_be_recovered(self):
        from gradient_ascent import coaching_history as history

        for action in ("finish", "restore"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "workspace"
                setup(root)
                write = history._write_target

                def interrupt(directory, name, body):
                    if name == "plan/weeks.json":
                        raise OSError("interrupted")
                    return write(directory, name, body)

                with (
                    mock.patch.object(history, "_write_target", side_effect=interrupt),
                    self.assertRaises(RuntimeError),
                ):
                    history.apply_plan_change(
                        root,
                        updates={"plan/goals.md": b"Goal", "plan/weeks.json": b"After"},
                        request=request(),
                    )
                journal = root / "plan/.history/journal.json"
                before = journal.read_bytes()
                identifier = history.plan_history(root)[-1]["id"]
                with self.assertRaisesRegex(RuntimeError, "unresolved"):
                    history.initialize_plan_history(root)
                self.assertEqual(journal.read_bytes(), before)
                self.assertEqual((root / "plan/weeks.json").read_bytes(), b"[]\n")
                self.assertEqual((root / "plan/goals.md").read_bytes(), b"Goal")
                history.recover_plan_change(root, identifier, action=action)
                self.assertTrue(history.initialize_plan_history(root)["created"])
                self.assertEqual(history.plan_history_drift(root)["drifted_files"], [])

    def test_drift_retries_when_a_sanctioned_writer_advances_history(self):
        from gradient_ascent import coaching_history as history

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            setup(root)
            history.initialize_plan_history(root)
            load = history._load
            advanced = False

            def advance(directory):
                nonlocal advanced
                document = load(directory)
                if not advanced:
                    advanced = True
                    history.apply_plan_change(
                        root, updates={"plan/weeks.json": b"Reviewed"}, request=request()
                    )
                return document

            with mock.patch.object(history, "_load", side_effect=advance):
                result = history.plan_history_drift(root)
            self.assertEqual(result["drifted_files"], [])
            self.assertEqual(history.plan_history(root)[-1]["status"], "applied")

    def test_drift_fails_with_controlled_error_if_history_keeps_changing(self):
        from gradient_ascent import coaching_history as history

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            setup(root)
            documents = [{**history._empty(), "test_revision": revision} for revision in range(6)]
            with (
                mock.patch.object(history, "_load", side_effect=documents),
                self.assertRaisesRegex(RuntimeError, "retry"),
            ):
                history.plan_history_drift(root)

    def test_capture_idempotence_revisions_recall_and_no_plan_mutation(self):
        from gradient_ascent.coaching_history import capture_coaching_entry, recall_coaching_history

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            setup(root)
            before = (root / "plan/weeks.json").read_bytes()
            first = capture_coaching_entry(root, entry())
            self.assertTrue(first["created"])
            self.assertEqual(capture_coaching_entry(root, entry()), {**first, "created": False})
            with self.assertRaises(ValueError):
                capture_coaching_entry(root, entry(body="Conflicting retry."))
            revised = capture_coaching_entry(
                root,
                entry("note-two", id=first["id"], expected_revision=1, body="Revised observation."),
            )
            self.assertEqual((revised["id"], revised["revision"]), (first["id"], 2))
            self.assertEqual(len(recall_coaching_history(root)), 1)
            self.assertEqual(len(recall_coaching_history(root, include_revisions=True)), 2)
            self.assertEqual(recall_coaching_history(root, start="2026-10-01"), [])
            self.assertEqual((root / "plan/weeks.json").read_bytes(), before)
            self.assertEqual((root / "plan/.history/journal.json").stat().st_mode & 0o777, 0o600)

    def test_baseline_apply_snapshots_idempotence_and_expected_hashes(self):
        from gradient_ascent.coaching_history import (
            apply_plan_change,
            initialize_plan_history,
            plan_history,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            setup(root)
            baseline = initialize_plan_history(root)
            self.assertTrue(baseline["created"])
            self.assertFalse(initialize_plan_history(root)["created"])
            before = hashlib.sha256(b"[]\n").hexdigest()
            updates = {"plan/weeks.json": b"[{}]\n", "plan/goals.md": b""}
            applied = apply_plan_change(
                root,
                updates=updates,
                request=request(),
                expected_hashes={"plan/weeks.json": before, "plan/goals.md": None},
            )
            self.assertEqual((applied["status"], applied["changed_files"]), ("applied", 2))
            self.assertFalse(apply_plan_change(root, updates=updates, request=request())["created"])
            self.assertFalse(
                apply_plan_change(
                    root,
                    updates=updates,
                    request=request(),
                    expected_hashes={"plan/weeks.json": before, "plan/goals.md": None},
                )["created"]
            )
            with self.assertRaises(ValueError):
                apply_plan_change(
                    root,
                    updates=updates,
                    request=request(),
                    expected_hashes={"plan/weeks.json": None, "plan/goals.md": None},
                )
            with self.assertRaises(ValueError):
                apply_plan_change(root, updates={"plan/weeks.json": b"[]"}, request=request())
            with self.assertRaises(ValueError):
                apply_plan_change(
                    root,
                    updates={"plan/weeks.json": b"[]"},
                    request=request("stale"),
                    expected_hashes={"plan/weeks.json": before},
                )
            history = plan_history(root, details=True)
            change = next(item for item in history if item["id"] == applied["id"])
            self.assertEqual(change["files"]["plan/weeks.json"]["before_content"], "[]\n")
            self.assertIsNone(change["files"]["plan/goals.md"]["before"])
            self.assertEqual(change["files"]["plan/goals.md"]["after_content"], "")
            for path in (root / "plan/.history/objects").iterdir():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_linked_decision_revision_is_pinned(self):
        from gradient_ascent.coaching_history import (
            apply_plan_change,
            capture_coaching_entry,
            plan_history,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            setup(root)
            decision = capture_coaching_entry(root, entry(kind="decision"))
            change = apply_plan_change(
                root,
                updates={"plan/goals.md": b"Goal"},
                request={**request(), "decision_id": decision["id"]},
            )
            capture_coaching_entry(
                root,
                entry(
                    "revised",
                    kind="decision",
                    id=decision["id"],
                    expected_revision=1,
                    rationale="Later rationale.",
                ),
            )
            saved = next(item for item in plan_history(root) if item["id"] == change["id"])
            self.assertEqual(saved["decision"]["revision"], 1)
            self.assertEqual(saved["decision"]["rationale"], "Explicit synthetic decision.")

    def test_exact_change_details_read_only_requested_snapshots(self):
        from gradient_ascent import coaching_history as history

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            setup(root)
            first = history.apply_plan_change(
                root, updates={"plan/goals.md": b"First"}, request=request()
            )
            history.apply_plan_change(
                root, updates={"plan/weeks.json": b"[{}]"}, request=request("second")
            )
            with mock.patch.object(history, "_snapshot_body", wraps=history._snapshot_body) as read:
                detail = history.plan_change_details(root, first["id"])
            self.assertEqual(read.call_count, 2)
            self.assertEqual(detail["files"]["plan/goals.md"]["after_content"], "First")
            with mock.patch.object(
                history, "_snapshot_body", side_effect=AssertionError("No snapshot read")
            ):
                compact = history.plan_change_by_key(root, "change-one")
            self.assertEqual((compact["id"], compact["status"]), (first["id"], "applied"))
            self.assertIsNone(history.plan_change_by_key(root, "missing-key"))
            self.assertEqual(len(history.plan_history(root, limit=history.MAX_RECORDS)), 2)
            with mock.patch.object(history, "MAX_HISTORY_DETAIL_BYTES", 1):
                with self.assertRaisesRegex(ValueError, "detail.*limit"):
                    history.plan_history(root, details=True)
            with self.assertRaisesRegex(ValueError, "not found"):
                history.plan_change_details(root, "change-missing")

    def test_partial_apply_blocks_next_change_and_reconcile_never_overwrites(self):
        from gradient_ascent import coaching_history as history

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            setup(root)
            write = history._write_target
            calls = 0

            def fail_second(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("sensitive error must not escape")
                return write(*args, **kwargs)

            with mock.patch.object(history, "_write_target", side_effect=fail_second):
                with self.assertRaisesRegex(RuntimeError, "recovery"):
                    history.apply_plan_change(
                        root,
                        updates={"plan/goals.md": b"New goal", "plan/weeks.json": b"[{}]"},
                        request=request(),
                    )
            before = (root / "plan/goals.md").read_bytes()
            self.assertEqual(history.reconcile_plan_history(root)["recovery_required"], 1)
            with self.assertRaisesRegex(RuntimeError, "recovery"):
                history.apply_plan_change(
                    root, updates={"calendar.json": b"{}"}, request=request("next")
                )
            self.assertEqual((root / "plan/goals.md").read_bytes(), before)
            self.assertEqual((root / "plan/weeks.json").read_bytes(), b"[]\n")

    def test_prepared_all_after_and_all_before_reconcile(self):
        from gradient_ascent import coaching_history as history

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            setup(root)
            write = history._save
            calls = 0

            def fail_terminal(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls >= 2:
                    raise OSError("interrupted terminal write")
                return write(*args, **kwargs)

            with mock.patch.object(history, "_save", side_effect=fail_terminal):
                with self.assertRaises(RuntimeError):
                    history.apply_plan_change(
                        root, updates={"plan/weeks.json": b"[{}]"}, request=request()
                    )
            self.assertEqual(history.reconcile_plan_history(root)["applied"], 1)
            with mock.patch.object(
                history, "_write_target", side_effect=OSError("failed before write")
            ):
                with self.assertRaises(RuntimeError):
                    history.apply_plan_change(
                        root, updates={"plan/weeks.json": b"[]"}, request=request("before")
                    )
            self.assertEqual(history.plan_history(root)[-1]["status"], "failed")
            noop = history.apply_plan_change(
                root, updates={"plan/weeks.json": b"[{}]"}, request=request("noop")
            )
            self.assertEqual(
                noop, {"id": None, "status": "unchanged", "created": False, "changed_files": 0}
            )

    def test_replaced_root_during_apply_never_writes_replacement(self):
        from gradient_ascent import coaching_history as history

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            setup(root)
            original = history._write_target

            def replace(*args, **kwargs):
                root.rename(root.with_name("old"))
                root.mkdir(mode=0o700)
                (root / "sentinel").write_text("replacement")
                return original(*args, **kwargs)

            with mock.patch.object(history, "_write_target", side_effect=replace):
                with self.assertRaisesRegex(RuntimeError, "generation changed"):
                    history.apply_plan_change(
                        root, updates={"plan/goals.md": b"Goal"}, request=request()
                    )
            self.assertEqual([path.name for path in root.iterdir()], ["sentinel"])

    def test_interrupted_restore_retains_explicit_intent_for_reconciliation(self):
        from gradient_ascent import coaching_history as history

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            setup(root)
            write = history._write_target
            count = 0

            def interrupt(*args, **kwargs):
                nonlocal count
                count += 1
                if count == 2:
                    raise OSError("interrupted")
                return write(*args, **kwargs)

            with mock.patch.object(history, "_write_target", side_effect=interrupt):
                with self.assertRaises(RuntimeError):
                    history.apply_plan_change(
                        root,
                        updates={"plan/goals.md": b"New", "plan/weeks.json": b"[{}]"},
                        request=request(),
                    )
            identifier = history.plan_history(root)[-1]["id"]
            save = history._save

            def fail_terminal(directory, document):
                if document["transactions"][-1]["events"][-1]["status"] == "restored":
                    raise OSError("terminal interrupted")
                return save(directory, document)

            with mock.patch.object(history, "_save", side_effect=fail_terminal):
                with self.assertRaises(RuntimeError):
                    history.recover_plan_change(root, identifier, action="restore")
            self.assertEqual((root / "plan/weeks.json").read_bytes(), b"[]\n")
            self.assertFalse((root / "plan/goals.md").exists())
            self.assertEqual(history.reconcile_plan_history(root)["restored"], 1)

    def test_explicit_finish_restore_and_divergence_refusal(self):
        from gradient_ascent import coaching_history as history

        for action in ("finish", "restore", "diverged"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "workspace"
                setup(root)
                write = history._write_target
                count = 0

                def interrupt(*args, **kwargs):
                    nonlocal count
                    count += 1
                    if count == 2:
                        raise OSError("interrupted")
                    return write(*args, **kwargs)

                with mock.patch.object(history, "_write_target", side_effect=interrupt):
                    with self.assertRaises(RuntimeError):
                        history.apply_plan_change(
                            root,
                            updates={"plan/goals.md": b"New", "plan/weeks.json": b"[{}]"},
                            request=request(),
                        )
                identifier = history.plan_history(root)[-1]["id"]
                if action == "diverged":
                    (root / "plan/weeks.json").write_bytes(b"Unrelated user edit")
                    with self.assertRaisesRegex(RuntimeError, "diverged"):
                        history.recover_plan_change(root, identifier, action="restore")
                    self.assertEqual(
                        (root / "plan/weeks.json").read_bytes(), b"Unrelated user edit"
                    )
                    self.assertEqual((root / "plan/goals.md").read_bytes(), b"New")
                else:
                    result = history.recover_plan_change(root, identifier, action=action)
                    self.assertEqual(
                        result["status"], "applied" if action == "finish" else "restored"
                    )
                    self.assertEqual(
                        (root / "plan/weeks.json").read_bytes(),
                        b"[{}]" if action == "finish" else b"[]\n",
                    )
                    self.assertEqual((root / "plan/goals.md").exists(), action == "finish")

    def test_allowlist_corruption_symlinks_and_unsupported_empty_fail_closed(self):
        from gradient_ascent import coaching_history as history

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            setup(root)
            for path in (
                ".env",
                "plan/../tokens.json",
                "plan/coaching_history.json",
                "strava/tokens.json",
            ):
                with self.assertRaises(ValueError):
                    history.apply_plan_change(root, updates={path: b"secret"}, request=request())
            with mock.patch.object(history, "_secure_files_supported", return_value=False):
                self.assertFalse(history.history_write_available(root))
                self.assertEqual(history.plan_history(root), [])
                self.assertEqual(history.recall_coaching_history(root), [])
                with self.assertRaisesRegex(RuntimeError, "platform"):
                    history.capture_coaching_entry(root, entry())
            (root / "plan/goals.md").symlink_to(root / "plan/weeks.json")
            with self.assertRaises((ValueError, OSError)):
                history.apply_plan_change(
                    root, updates={"plan/goals.md": b"Goal"}, request=request()
                )
            (root / "plan/goals.md").unlink()
            history.capture_coaching_entry(root, entry())
            with mock.patch.object(history, "_secure_files_supported", return_value=False):
                with self.assertRaisesRegex(RuntimeError, "existing coaching history"):
                    history.history_write_available(root)
            journal = root / "plan/.history/journal.json"
            journal.write_text('{"version":1,"entries":[')
            with self.assertRaises(ValueError):
                history.plan_history(root)

    def test_invalid_scopes_references_and_revision_cannot_write(self):
        from gradient_ascent import coaching_history as history

        invalid = (
            {"kind": "applied"},
            {"scopes": [{"kind": "day", "start_date": "2026-09-01", "end_date": "2026-09-02"}]},
            {"scopes": [{"kind": "month", "start_date": "2026-09-02", "end_date": "2026-09-30"}]},
            {"evidence": [{"kind": "plan_file", "ref": "../.env"}]},
            {"thread_id": "https://example.com/private"},
            {"expected_revision": True},
            {"unknown": "ignored"},
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            setup(root)
            for changes in invalid:
                with self.subTest(keys=list(changes)), self.assertRaises(ValueError):
                    history.capture_coaching_entry(root, entry(**changes))
            self.assertFalse((root / "plan/.history/journal.json").exists())

    def test_tags_canonical_activity_references_and_exact_entry_lookup(self):
        from gradient_ascent import coaching_history as history

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            setup(root)
            captured = history.capture_coaching_entry(
                root,
                entry(
                    activity_name="Standalone synthetic label",
                    tags=["recovery", "review"],
                    evidence=[
                        {"kind": "activity", "ref": "strava:123", "summary": "Synthetic ride"}
                    ],
                ),
            )
            selected = history.coaching_entry_by_id(root, captured["id"], revision=1)
            self.assertEqual(selected["tags"], ["recovery", "review"])
            self.assertEqual(selected["activity_name"], "Standalone synthetic label")
            self.assertEqual(selected["evidence"][0]["ref"], "strava:123")
            self.assertEqual(
                len(history.recall_coaching_history(root, limit=history.MAX_RECORDS)), 1
            )
            with self.assertRaises(ValueError):
                history.capture_coaching_entry(root, entry("bad-tags", tags=["x" * 65]))
            with self.assertRaises(ValueError):
                history.capture_coaching_entry(
                    root, entry("bad-ref", evidence=[{"kind": "transaction", "ref": "strava:123"}])
                )

    def test_snapshot_hardlinks_and_corruption_are_rejected(self):
        from gradient_ascent import coaching_history as history

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            setup(root)
            change = history.apply_plan_change(
                root, updates={"plan/goals.md": b"Private goal"}, request=request()
            )
            digest = hashlib.sha256(b"Private goal").hexdigest()
            snapshot = root / "plan/.history/objects" / digest
            alias = root / "alias"
            os.link(snapshot, alias)
            with self.assertRaises(ValueError):
                history.plan_change_details(root, change["id"])
            alias.unlink()
            snapshot.write_bytes(b"Corrupt goal")
            with self.assertRaisesRegex(ValueError, "integrity"):
                history.plan_change_details(root, change["id"])

    def test_shared_capability_and_nonempty_orphan_objects_never_allow_fallback(self):
        from gradient_ascent import coaching_history as history, recording_repair

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            setup(root)
            with mock.patch.object(recording_repair, "_secure_files_supported", return_value=False):
                self.assertFalse(history.history_write_available(root))
                (root / "plan/.history/objects").mkdir(parents=True, mode=0o700)
                with self.assertRaisesRegex(RuntimeError, "existing coaching history"):
                    history.history_write_available(root)
                journal = root / "plan/.history/journal.json"
                journal.write_text('{"version":1,"entries":[],"transactions":[]}')
                journal.chmod(0o600)
                (root / "plan/.history/objects/orphan").write_bytes(b"orphan")
                with self.assertRaisesRegex(RuntimeError, "existing coaching history"):
                    history.history_write_available(root)

    def test_prepared_directory_entries_are_fsynced_before_first_plan_write(self):
        from gradient_ascent import coaching_history as history

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            setup(root)
            fsync, write = os.fsync, history._write_target
            synced = set()

            def record(descriptor):
                info = os.fstat(descriptor)
                synced.add((info.st_dev, info.st_ino))
                return fsync(descriptor)

            def verify(*args, **kwargs):
                for path in (
                    root,
                    root / "plan",
                    root / "plan/.history",
                    root / "plan/.history/objects",
                ):
                    info = path.stat()
                    self.assertIn((info.st_dev, info.st_ino), synced)
                return write(*args, **kwargs)

            with (
                mock.patch.object(history.os, "fsync", side_effect=record),
                mock.patch.object(history, "_write_target", side_effect=verify),
            ):
                history.apply_plan_change(
                    root, updates={"plan/goals.md": b"Goal"}, request=request()
                )


if __name__ == "__main__":
    unittest.main()
