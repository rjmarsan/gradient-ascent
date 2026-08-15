---
name: coach-setup-plan
description: Import a training calendar CSV or XLSX, build events and week plans, or record that the rider has no current plan.
---

# Coach plan setup

## Workflow

1. Confirm the current directory is the private athlete workspace.

Resolve the workspace-local launcher:

```bash
WORKSPACE_DIR="${COACH_WORKSPACE_DIR:-${COACH_DATA_DIR:-$PWD}}"
COACH_CLI="$WORKSPACE_DIR/.codex/bin/gradient-ascent"
test -x "$COACH_CLI" || COACH_CLI="$(command -v gradient-ascent)"
```

2. If the rider has a CSV or XLSX calendar, ask for its path only when it is not already known:

```bash
"$COACH_CLI" build-plan /path/to/calendar.xlsx
```

Preserve event markers from generic `Events` or cycling-discipline columns, including `[team]`, `[commit]`, `[maybe]`, and `[skip]`. Prefer ISO date ranges; legacy `MM/DD/YY` remains accepted.

3. If the rider has no current plan:

```bash
"$COACH_CLI" onboarding-choice plan none
```

4. Rebuild and verify:

```bash
"$COACH_CLI" refresh
```

Inspect `plan/weeks.json`, `plan/events.json`, and `derived/training_center.html`.

## Rules

- Preserve completed profile, goals, and source decisions.
- Do not require activity or recovery data to import a plan.
- Do not invent missing workout duration, intensity, or training load.
- Accept the shipped weekly matrix or a daily `Date,Workout` layout. If no workouts are recognized, stop without replacing the current plan and show the rider `examples/calendar/sample-training-calendar.csv` or `examples/calendar/sample-daily-plan.csv`.
- Keep athlete plan files private.

## Response contract

End with whether the import succeeded, which plan and event files changed, whether the Training Center was rebuilt, and the exact next missing input.
