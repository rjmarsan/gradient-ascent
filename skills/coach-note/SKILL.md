---
name: coach-note
description: Recall private coaching context or save an approved observation, proposal, decision, or session takeaway for a day, week, month, or season. Keep note capture separate from applying a training-plan change.
---

# Coach Note

## Purpose

Use this skill for “what did we decide?”, “remember this”, or an approved coaching-session wrap-up. Save a concise synthesis, not a transcript. Read the [coaching history guide](https://github.com/rjmarsan/gradient-ascent/blob/main/docs/coaching-history.md) for the capture schema, revisions, and official plan-change history.

The private coaching journal supports day, week, month, and season context. Existing ride/day notes in `plan/coach_notes.json` remain supported. Neither kind of note changes the training plan.

## What To Write

- Write a concise synthesis, not the user's words verbatim.
- Choose the smallest useful date scope; include more than one scope only when the takeaway genuinely applies to each.
- Distinguish an observation, an unapproved proposal, and a decision the rider actually agreed to. A saved decision is not proof that the plan was updated.
- Include the ride title or Strava activity id when known.
- Include a Codex thread link. The CLI infers `codex://threads/$CODEX_THREAD_ID` when running in Codex Desktop.
- Do not quote Strava private notes unless explicitly asked. Summarize the coaching implication instead.
- Keep tags short, comma-separated, and practical, for example `hr-drift`, `race`, `fueling`, `fatigue`, `plan-adjustment`.

## Workflow

From the coaching workspace root, resolve the local CLI and recall relevant context before adding another entry:

```bash
WORKSPACE_DIR="${COACH_WORKSPACE_DIR:-${COACH_DATA_DIR:-$PWD}}"
COACH_CLI="$WORKSPACE_DIR/.codex/bin/gradient-ascent"
test -x "$COACH_CLI" || COACH_CLI="$(command -v gradient-ascent)"

"$COACH_CLI" coaching-context --start YYYY-MM-DD --end YYYY-MM-DD --limit 10
"$COACH_CLI" plan-history
```

For a lasting takeaway, offer one capture checkpoint at a natural wrap-up. Save only
when the rider asks, approves the proposed synthesis, or has explicitly opted into
that kind of capture. An ordinary advice question is not permission to save notes or
apply a plan. Keep the body to what happened or was decided, why it matters, the
conditions that could change it, and any unresolved next action. Do not silently
turn a proposal into a decision.

Draft the approved entry in a private JSON file outside the athlete workspace, then:

```bash
"$COACH_CLI" add-coaching-context --file /path/to/coaching-context-draft.json
"$COACH_CLI" coaching-context --start YYYY-MM-DD --end YYYY-MM-DD --limit 10
```

Reuse the same idempotency key when retrying the same capture. To correct an existing
entry, use the journal's revision mechanism instead of adding a duplicate. A plan
change needs its own explicit authorization and sanctioned writer; use
`plan-history` to verify that it actually applied. Report separately what was saved,
what was only proposed, and whether any plan files changed.

For an explicitly requested legacy ride-card note, the existing command still works:

```bash
"$COACH_CLI" add-coach-note \
  --date YYYY-MM-DD \
  --title "Short title" \
  --note "Concise coaching synthesis." \
  --ride-id ACTIVITY_ID \
  --activity-name "Strava ride title" \
  --tags "hr-drift,threshold"
```

On supported platforms, this command saves the new note in the coaching journal
and rebuilds the Training Center by default. Existing `plan/coach_notes.json`
entries remain readable and are not rewritten. Only an unsupported platform with
empty history uses the legacy file, reporting history as unavailable. Identical
content and thread information produce an idempotent capture; use an explicit
`--idempotency-key` when retrying a known operation. Do not duplicate the takeaway.

If the takeaway also deserves a lightweight top-level reaction on the ride card, add one after the note:

```bash
"$COACH_CLI" react-to-ride \
  --ride-id ACTIVITY_ID \
  --emoji "🔥"
```

Use one reaction as a compact coaching signal, not as a replacement for the written note when there is a real coaching takeaway to preserve.

Then recall the date and verify the generated Training Center:

```bash
"$COACH_CLI" coaching-context --start YYYY-MM-DD --end YYYY-MM-DD --limit 10
test -s "$WORKSPACE_DIR/derived/training_center.html"
```

Keep the athlete workspace and generated Training Center private.

## Legacy ride-note shape

Each entry should include:
- `date`: the ride/day date.
- `title`: short coaching title.
- `note`: synthesized coach observation.
- `ride_id` and/or `activity_name` when known.
- `tags`: short topic tags.
- `codex_url`: the Codex thread link.

If the CLI cannot infer the thread id, pass `--codex-thread-id` or `--codex-url`.
