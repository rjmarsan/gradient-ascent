---
name: coach-setup-plan
description: Import a training calendar, preserve events and week plans, or optionally author explicit cycling workouts and export a private planned schedule.
---

# Coach plan setup

For an accepted FTP change, use `ftp-history` followed by the effective-dated
`set-ftp` command with the rider's value, date, and rationale. Never overwrite the
profile's FTP scalar independently. Review changed workout wattages and affected
budget fingerprints as separate coaching decisions; see the
[dated FTP workflow](https://github.com/rjmarsan/gradient-ascent/blob/main/docs/training-load.md#change-ftp-without-rewriting-earlier-load).

## Workflow

1. Confirm the current directory is the private athlete workspace.

Resolve the workspace-local launcher:

```bash
WORKSPACE_DIR="${COACH_WORKSPACE_DIR:-${COACH_DATA_DIR:-$PWD}}"
COACH_CLI="$WORKSPACE_DIR/.codex/bin/gradient-ascent"
test -x "$COACH_CLI" || COACH_CLI="$(command -v gradient-ascent)"
```

Before revising an existing plan, recall the relevant dates with `coaching-context`
and inspect `plan-history`. Current canonical files and confirmed applied changes
matter more than an old proposal. Keep drafting, saving a coaching decision, and
applying a plan change separate; confirm the rider requested the actual mutation
immediately before using a writer. The
[coaching history guide](https://github.com/rjmarsan/gradient-ascent/blob/main/docs/coaching-history.md)
describes expected file hashes, change reasons, and recovery of an interrupted write.

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

## Coach-authored weekly TSS budgets

When the rider asks to create or revise a training plan, treat its weekly TSS budget
as an explicit coaching decision. Read `plan/goals.md`, relevant plan weeks and
events, recent compact recorded-load summaries and their completeness, recovery
constraints, and the intended key sessions. Explain which goal the proposed load
serves, what it protects, and what would trigger a reduction or review. Do not
derive a budget from available hours, invent historical targets, or prescribe extra
riding merely to fill a TSS shortfall. A budget is not a measured result or a quota.

Preserve imported weekly `TSS Target` and daily `Planned TSS` values. Fully prescribed
workouts can support a separately labeled load calculation; rough estimates from
session descriptions are not a weekly coaching budget. If the information is
insufficient, leave the budget unset and identify the specific decision needed.

Draft agreed budgets outside the workspace. For automated drafts, obtain the current
week fingerprints and copy the matching value into `expected_plan_fingerprint`.
Then use the validated, locked command:

```bash
"$COACH_CLI" tss-budget-status --fingerprints
"$COACH_CLI" update-tss-budgets --file /path/to/tss-budgets-draft.json
"$COACH_CLI" tss-budget-status
```

The private `plan/tss_budgets.json` is separate from imported calendar files and
survives reimport. The normal update rebuilds local insights and the dashboard from
the current plan without configured imports or provider requests; `--no-rebuild`
defers both rebuilds. Do not write the budget file directly. An imported source target wins unless
the rider explicitly chooses an override; a conflicting draft is rejected otherwise.
A change to relevant goals, profile, events, or prescriptions can
make a stored budget require review; review and deliberately reissue it instead of
silently accepting the old target. Use `--replace` only when the rider intends to
replace the complete set of coach budgets. Reapproving a changed plan requires its
current fingerprint. See the
[public training-load guide](https://github.com/rjmarsan/gradient-ascent/blob/main/docs/training-load.md#coach-authored-budgets)
and [synthetic budget example](https://github.com/rjmarsan/gradient-ascent/blob/main/examples/calendar/sample-tss-budgets.json)
for the draft schema and precedence rules. Example numbers are not a prescription.

When the rider asks for a future CTL/ATL scenario, first inspect any current daily
allocations with `"$COACH_CLI" tss-budget-status --daily`. If a week still needs
allocation, make explicit coaching decisions for every date, including rest as zero
and any provisional event assumptions. Store those full-day targets in the budget's
optional `daily_tss` list; the sum must equal its weekly central target. Preserve
existing daily source prescriptions unless the rider deliberately approves the
separate `override_daily_source` flag. A weekly source override does not authorize
changing a daily prescription.

Save through the same validated budget command. Omitting `daily_tss` preserves an
existing allocation when the central target is unchanged; use `daily_tss: null`
to clear it, or supply a complete replacement when changing the target. Do not
divide a weekly total evenly, infer exact targets from hours, or invent allocations
merely to extend a chart. Daily targets are not executable device workouts.

Explain the Training Center's **Plan / CTL · ATL** switcher accurately: Plan is the
default weekly-budget comparison; CTL/ATL is modeled load. Its conditional future
curve starts tomorrow from today's recorded state, excludes the remainder of today's
plan, and stops at the first missing daily target. Report provisional decisions,
source conflicts, and missing dates instead of claiming an unbroken forecast.

## Optional device workouts and schedule export

Offer this only when the rider asks for a downloadable schedule or executable device
workouts. Review [the workout export guide](https://github.com/rjmarsan/gradient-ascent/blob/main/docs/workout-export.md)
and [synthetic schema example](https://github.com/rjmarsan/gradient-ascent/blob/main/examples/workouts/structured-workouts.json)
(`docs/workout-export.md` and `examples/workouts/structured-workouts.json` in the
public source checkout). Do not read private raw activity or health data to manufacture
a prescription.

For device-ready cycling workouts, ask for any missing step durations, intensity,
and explicit power ranges or an intentional open target. Store only the agreed
prescription in the private workspace's `plan/workouts.json` version-1 document,
using the validated `update-plan --file` command with the current expected hashes.
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
When budgets were requested, also report the affected weeks, agreed targets or
ranges, provisional versus confirmed decisions, source conflicts, and budgets
still missing or requiring review.
