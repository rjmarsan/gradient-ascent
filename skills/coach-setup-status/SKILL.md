---
name: coach-setup-status
description: Inspect Gradient Ascent setup status and identify the single highest-value next setup action.
---

# Coach setup status

Give a concrete, read-only setup report unless the rider explicitly asks for a change.

## Workflow

1. Confirm the current directory is the private athlete workspace.

Resolve the workspace-local launcher:

```bash
WORKSPACE_DIR="${COACH_WORKSPACE_DIR:-${COACH_DATA_DIR:-$PWD}}"
COACH_CLI="$WORKSPACE_DIR/.codex/bin/gradient-ascent"
test -x "$COACH_CLI" || COACH_CLI="$(command -v gradient-ascent)"
```

2. Inspect resumable onboarding:

```bash
export COACH_WORKSPACE_DIR="${COACH_WORKSPACE_DIR:-$PWD}"
"$COACH_CLI" onboarding-status --json
```

3. Inspect local source state when relevant:

```bash
"$COACH_CLI" connections-status --json --summary
```

4. Check high-value artifacts when present:

- `plan/goals.md`, `plan/weeks.json`, and `plan/events.json`
- `strava/state.json` and `strava/activities.json`
- `garmin/` and `apple_health/`
- `derived/post_sync_summary.json`
- `derived/training_center.html`

5. Summarize profile and goals, plan or explicit no-plan choice, activity archive or explicit no-history choice, optional recovery coverage, and dashboard presence.

6. Recommend exactly one next skill when there is a clear best move: `$coach-setup-plan`, `$coach-setup-activities`, or `$coach-setup-recovery`.

## Rules

- Separate optional enrichment from real blockers.
- Never recommend provider credentials.
- Do not overclaim readiness when workspace files or the dashboard are missing.

## Response contract

End with a ready or not-ready assessment, plan/activity/recovery status, dashboard presence, and one best next action if needed.
