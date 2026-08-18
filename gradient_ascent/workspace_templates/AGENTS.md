# Gradient Ascent workspace instructions

This is one athlete's private coaching workspace. The separate Gradient Ascent checkout contains tooling only; never place athlete data there.

## Rules

- Run `.codex/bin/gradient-ascent ...` from this workspace for application commands.
- Preserve recorded answers and source files. Missing data must stay missing.
- Never invent athlete measurements or claim an import succeeded without checking its output.
- Never load raw source archives, sample-level GPS streams, lap/detail payloads, raw health exports, or `.env` into model context. Compact normalized activity rows and derived aggregate summaries are allowed; do not pass raw source records to subagents.
- Keep raw source files and generated athlete dashboards private.
- Gradient Ascent uses local file imports and explicitly enabled Ride with GPS sync. Never request or persist provider credentials; the actual official `ride` CLI owns its login.

## Sources of truth

- `plan/athlete.json`: profile, time zone, units, disciplines, availability, and constraints.
- `plan/goals.md`: decision-driving coaching goals.
- `plan/weeks.json` and `plan/events.json`: imported plan and events.
- `plan/daily_notes.json` and `plan/coach_notes.json`: persistent coaching notes.
- `plan/.history/`: private coaching context and official plan-change history.
- `strava/activities.json`: normalized activity index.
- `garmin/` and `apple_health/`: normalized recovery records.
- `connections/config.json`: non-secret local import paths.
- `context/` and `threads/`: private narrative context.
- `derived/`: rebuildable output, never the source of truth.

<!-- gradient-ascent:coaching-history:start -->
## Coaching recall and capture

Before material advice, recall the relevant date range with
`.codex/bin/gradient-ascent coaching-context --start YYYY-MM-DD --end YYYY-MM-DD --limit 10`.
Use `plan-history` to check what actually applied, then compare with current goals,
plan files, and budget status. A remembered proposal is not an instruction.

At a useful session checkpoint, offer to save a concise takeaway. Capture only when
the athlete asks, approves the synthesis, or explicitly opts into that kind of
capture. Use `add-coaching-context --file DRAFT`; do not write the journal directly.
Saving an observation, proposal, or decision does not authorize changing the plan.
Use the appropriate validated plan writer only after the athlete approves applying
the change, and verify its status with `plan-history`. Keep unresolved choices
unresolved and reuse the same change key for an identical retry.
<!-- gradient-ascent:coaching-history:end -->

## Setup and resume

```bash
.codex/bin/gradient-ascent onboarding-status --json
```

Advance only the reported step. Use the onboarding commands for structured answers and explicit no-event, no-plan, or no-activity choices. Rerun status after each step so an interrupted setup does not repeat completed questions.

Supported local imports:

```bash
.codex/bin/gradient-ascent import-strava-export /path/to/strava-export.zip
.codex/bin/gradient-ascent import-garmin-export /path/to/garmin-export
.codex/bin/gradient-ascent import-apple-health-export /path/to/export.xml
.codex/bin/gradient-ascent build-plan /path/to/training-plan.xlsx
```

## Rebuild

```bash
.codex/bin/gradient-ascent refresh
.codex/bin/gradient-ascent onboarding-status --json
```

Serve the Training Center only through its built-in loopback server. Use the exact printed port and verify `/api/health` returns the Workspace ID printed by the current process.

## Deletion

`.codex/bin/gradient-ascent purge-workspace PATH` previews complete workspace deletion. Confirm only with the exact resolved path after the athlete approves.
