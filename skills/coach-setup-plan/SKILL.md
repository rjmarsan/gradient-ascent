---
name: coach-setup-plan
description: Import a training calendar, preserve events and week plans, or optionally author explicit cycling workouts and export a private planned schedule.
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

## Optional device workouts and schedule export

Offer this only when the rider asks for a downloadable schedule or executable device
workouts. Review [the workout export guide](https://github.com/rjmarsan/gradient-ascent/blob/main/docs/workout-export.md)
and [synthetic schema example](https://github.com/rjmarsan/gradient-ascent/blob/main/examples/workouts/structured-workouts.json)
(`docs/workout-export.md` and `examples/workouts/structured-workouts.json` in the
public source checkout). Do not read private raw activity or health data to manufacture
a prescription.

For device-ready cycling workouts, ask for any missing step durations, intensity,
and explicit power ranges or an intentional open target. Store only the agreed
prescription in the private workspace's `plan/workouts.json` version-1 document.
That file is independently authoritative: preserve existing entries and never
silently replace weekly prose, events, or another workout. Weekly prose and races
remain calendar-only unless the rider separately authors an exact structured workout.
Keep `[skip]` events cancelled and `[maybe]` events tentative; do not turn either into
a commitment.

Use the normal exporter to validate the schema and selected date range:

```bash
"$COACH_CLI" export-plan --format zip --start YYYY-MM-DD --end YYYY-MM-DD
"$COACH_CLI" export-plan --format fit --workout WORKOUT_ID
```

ZIP is the default and includes a readable plan, static ICS calendar, CSV schedule,
manifest, and FIT files for explicit structured workouts. FIT requires a selected
workout ID. Use `--out PATH` only for a rider-chosen location; do not pass `--overwrite`
without approval to replace that file. If validation fails, explain the missing or
unsupported field without inventing a replacement. Report calendar-entry and
device-ready workout counts accurately, including when no FIT files can be produced.

The Training Center also exposes **More > Export planned schedule**. Export is
local-only: do not upload to Garmin/Wahoo, claim that copying a FIT file schedules a
device-calendar date, or publish a Site without a separate explicit request and
review of its audience and contents.

## Rules

- Preserve completed profile, goals, and source decisions.
- Do not require activity or recovery data to import a plan.
- Do not invent missing workout duration, intensity, or training load.
- Structured targets must be explicitly authored; never infer them from weekly prose.
- Accept the shipped weekly matrix or a daily `Date,Workout` layout. If no workouts are recognized, stop without replacing the current plan and show the rider `examples/calendar/sample-training-calendar.csv` or `examples/calendar/sample-daily-plan.csv`.
- Keep athlete plan files private.

## Response contract

End with whether the import succeeded, which plan and event files changed, whether
the Training Center was rebuilt, and the exact next missing input. If an export was
requested, also report its path, date range, calendar-entry count, device-ready
workout count, and any validation errors. Do not claim a provider upload or device
transfer occurred unless it was separately requested and verified.
