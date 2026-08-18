# Private coaching context and plan history

Coaching context answers **what did we observe, consider, or decide?** Plan history
answers **what actually changed in the official local plan?** They are separate:
saving a note, a proposal, or even an agreed decision never applies a workout,
changes a TSS budget, or contacts a provider.

Use these commands from the private athlete workspace. Keep the workspace, journal,
snapshots, and command output private; the public checkout contains tooling and
synthetic examples only.

## Recall before advising

```bash
.codex/bin/gradient-ascent coaching-context --start 2026-04-06 --end 2026-04-12 --limit 10
.codex/bin/gradient-ascent coaching-context --start 2026-04-01 --end 2026-04-30 --kind decision
.codex/bin/gradient-ascent plan-history
.codex/bin/gradient-ascent plan-history --details CHANGE_ID
```

Recall matches overlapping date scopes and returns the latest revision of each
entry by default. Add `--revisions` when investigating how an entry changed. Check
the current goals, plan, and budget status alongside recalled context. A proposal
may still be unapproved; a decision may have been superseded or never applied.
Only an applied plan-history record, checked against the current plan, proves a
particular change happened.

Recall includes compact official-change metadata and readable legacy day notes,
with counts when a result is truncated. Snapshot contents are returned only by an
explicit `plan-history --details CHANGE_ID` request. A current-drift warning means
an official file differs from its last recorded state; it does not invent who
changed it, why, or an applied-history entry for that manual edit.

## Capture an approved takeaway

Use `$coach-note` for “remember this”, “what did we decide?”, or a session wrap-up.
An ordinary advice conversation is read-only by default. At a useful checkpoint,
the coach can offer a short synthesis; save it when the rider asks, approves that
synthesis, or has explicitly opted into that kind of capture. Do not save raw
transcripts, private provider-note quotations, GPS, health exports, or credentials.

Draft one JSON object in a private file outside the athlete workspace, then run:

```bash
.codex/bin/gradient-ascent add-coaching-context --file /path/to/context-draft.json
```

See the [synthetic proposal example](../examples/coaching/sample-context.json).
Its numbers and choices are not a training prescription. The draft fields are:

| Field | Meaning |
| --- | --- |
| `kind` | `observation`, `proposal`, or `decision`; use `decision` only for an actual agreed choice. |
| `scopes` | One to sixteen `{kind,start_date,end_date}` objects using ISO dates. A `day` has one date; a `week` spans one to seven days; a `month` is a complete calendar month; a `season` spans at most 366 days. |
| `title`, `body`, `rationale` | A short heading, concise synthesis, and why it matters. Limits are 256, 16,384, and 4,096 UTF-8 bytes respectively. |
| `idempotency_key` | A stable lowercase key for this exact capture: letters, digits, `_` and `-`, up to 128 characters. Reuse it for an identical retry. |
| `conditions` | Optional list of at most sixteen short conditions or review triggers. |
| `evidence` | Optional references `{kind,ref,summary?}` to `daily_summary`, `weekly_summary`, an allowlisted `plan_file`, `activity`, `coaching_entry`, or `transaction`. Reference compact evidence; do not copy raw records. |
| `thread_id`, `related_ids` | Optional Codex thread identifier and related coaching-entry identifiers. |
| `tags`, `activity_name` | Optional short tags and a safe activity label. Use an `activity` evidence reference when the actual activity ID is known. |

An identical retry returns the existing capture. Reusing a key with different
content is rejected. To correct an entry, first recall its current `id` and
`revision`, then submit a new key with `id` and `expected_revision`. The old revision
remains available. An entry's kind cannot change: record a later decision separately
and relate it to the earlier proposal.

Existing `add-coach-note` ride/day notes remain supported. On supported platforms,
new notes go to the journal; the old `plan/coach_notes.json` is retained and read
alongside it. Identical content and thread information are deduplicated, or use
`--idempotency-key` for an explicit retry key. An unsupported platform can use the
legacy file only when history is empty, and reports history as unavailable. A
nonempty journal fails closed rather than being ignored or replaced. Capturing a
note remains distinct from applying a plan change.

## Apply a separately approved plan change

After the rider explicitly approves the mutation, use the appropriate validated
writer: `build-plan`, `update-goal-files`, `update-tss-budgets`, `set-ftp`, or `update-plan`.
The existing import, goals, and budget writers accept `--reason`, `--decision-id`,
and `--change-key` to explain the change, link a saved decision, and safely retry
one operation. `update-plan` takes those semantics from its JSON `change` object,
not those command-line flags. A reason or decision link does not grant permission.

For a dated workout-text/source-load edit or an independent structured workout:

```bash
.codex/bin/gradient-ascent plan-history --fingerprints
.codex/bin/gradient-ascent update-plan --file /path/to/approved-plan-draft.json
.codex/bin/gradient-ascent plan-history
```

The version-1 plan draft has `change` metadata (`idempotency_key`, `title`,
`rationale`, optionally `decision_id`), `expected_files` containing the exact
current SHA-256 for every edited file, and one or both of:

- `days`: entries `{date,workout,load?}`. Each date must belong to exactly one
  existing source week. `load`, when supplied, contains explicit `hours_min`,
  `hours_max`, `tss_min`, and `tss_max` source values. Omitting or clearing it removes
  that day's old source-load values rather than attaching them to new prose.
- `workouts`: `{upsert:[...],remove:[...]}` using the independently authoritative
  [structured workout schema](workout-export.md). Removal values are workout IDs.

Use the actual hash from `plan-history --fingerprints`; use JSON `null` only when
the edited file is absent. If a hash changed, reread the plan and review the draft
instead of bypassing the conflict. The editor preserves unrelated days, recorded
activities, and separately stored coach budgets. A changed prescription can make a
budget need review; do not silently reapprove or redistribute it.

## History and interrupted changes

Private history lives under `plan/.history/`: a journal and content-addressed file
snapshots. A baseline records only the current state; it does not invent historical
edits. The journal retains reasons, linked decisions, before/after hashes, and
transaction status. It is not a public Git history or a transcript archive.

For an existing workspace, explicitly start its current-state baseline with:

```bash
.codex/bin/gradient-ascent init-plan-history
```

To also append the reviewed recall/capture instructions to the existing workspace
`AGENTS.md`, use `init-plan-history --install-guidance`. This optional installation
preserves custom instructions, uses a marked section, and is safe to repeat. A
normal refresh does not create a retrospective baseline. Reinitializing with
`init-workspace --force` preserves existing authoritative plan files; it is not a
way to reset the athlete's plan.

`plan/.history/` is ignored by the workspace's Git defaults. The first history
write adds that rule to older workspaces before creating private journal or snapshot
files. Arrange an appropriate private local backup; do not put history in public Git.
The history engine is bounded: an official file may be at most 8 MiB, a change's
before or after file set at most 32 MiB, and the journal at most 32 MiB. If a limit
or unsupported platform blocks a write, keep the existing files and report the
blocker rather than bypassing history or discarding older entries.

Multi-file updates are recoverable, not magically atomic. If a command reports an
interrupted or uncertain change, inspect its details first:

```bash
.codex/bin/gradient-ascent reconcile-plan-history
.codex/bin/gradient-ascent plan-history --details CHANGE_ID
```

Reconciliation classifies the files that exist now; it does not finish or roll back
the plan. Only after the rider chooses the intended result, use
`recover-plan-change CHANGE_ID --action finish` or `--action restore`. Recovery
refuses unexpected divergent files. Do not guess, overwrite a newer edit, or claim
success while the transaction still needs recovery.
