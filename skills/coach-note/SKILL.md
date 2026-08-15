---
name: coach-note
description: Add a synthesized coach note after a ride discussion, data deep dive, or coaching insight that should persist in the training center with a Codex session link.
---

# Coach Note

## Purpose

Use this skill to persist coach-authored observations, not transcripts. A coach note should capture the useful coaching takeaway from chat, Strava/Garmin evidence, or a ride analysis so future sessions can find it in the training center.

Coach notes are stored in `$COACH_WORKSPACE_DIR/plan/coach_notes.json` and rendered in `$COACH_WORKSPACE_DIR/derived/training_center.html`.
Dashboard labels and ride reactions live in `$COACH_WORKSPACE_DIR/plan/dashboard_labels.json`.

## What To Write

- Write a concise synthesis, not the user's words verbatim.
- Anchor the note to a specific `YYYY-MM-DD` date.
- Include the ride title or Strava activity id when known.
- Include a Codex thread link. The CLI infers `codex://threads/$CODEX_THREAD_ID` when running in Codex Desktop.
- Do not quote Strava private notes unless explicitly asked. Summarize the coaching implication instead.
- Keep tags short, comma-separated, and practical, for example `hr-drift`, `race`, `fueling`, `fatigue`, `plan-adjustment`.

## Workflow

From the coaching workspace root, add the note with the CLI:

```bash
WORKSPACE_DIR="${COACH_WORKSPACE_DIR:-${COACH_DATA_DIR:-$PWD}}"
COACH_CLI="$WORKSPACE_DIR/.codex/bin/gradient-ascent"
test -x "$COACH_CLI" || COACH_CLI="$(command -v gradient-ascent)"

"$COACH_CLI" add-coach-note \
  --date YYYY-MM-DD \
  --title "Short title" \
  --note "Concise coaching synthesis." \
  --ride-id ACTIVITY_ID \
  --activity-name "Strava ride title" \
  --tags "hr-drift,threshold"
```

The command writes `$COACH_WORKSPACE_DIR/plan/coach_notes.json` and rebuilds the training center by default.

If the takeaway also deserves a lightweight top-level reaction on the ride card, add one after the note:

```bash
"$COACH_CLI" react-to-ride \
  --ride-id ACTIVITY_ID \
  --emoji "🔥"
```

Use one reaction as a compact coaching signal, not as a replacement for the written note when there is a real coaching takeaway to preserve.

Then verify the generated local artifacts exist and contain the new note:

```bash
test -s "$WORKSPACE_DIR/plan/coach_notes.json"
test -s "$WORKSPACE_DIR/derived/training_center.html"
```

Keep the athlete workspace and generated Training Center private.

## Note Shape

Each entry should include:
- `date`: the ride/day date.
- `title`: short coaching title.
- `note`: synthesized coach observation.
- `ride_id` and/or `activity_name` when known.
- `tags`: short topic tags.
- `codex_url`: the Codex thread link.

If the CLI cannot infer the thread id, pass `--codex-thread-id` or `--codex-url`.
