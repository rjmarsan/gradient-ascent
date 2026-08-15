---
name: coach-setup-activities
description: Set up Gradient Ascent activity history from an official Strava account archive, choose no history, or verify locally parsed ride files.
---

# Coach activity setup

## Purpose

Use this skill after the base plugin and workspace install is healthy. Activity setup is optional and file-based.

## Workflow

Resolve the workspace-local launcher once and use it for every command:

```bash
WORKSPACE_DIR="${COACH_WORKSPACE_DIR:-${COACH_DATA_DIR:-$PWD}}"
COACH_CLI="$WORKSPACE_DIR/.codex/bin/gradient-ascent"
test -x "$COACH_CLI" || COACH_CLI="$(command -v gradient-ascent)"
```

1. Inspect compact source state:

```bash
export COACH_WORKSPACE_DIR="${COACH_WORKSPACE_DIR:-$PWD}"
"$COACH_CLI" connections-status --json --summary
```

2. Send the rider to Strava's official [account download page](https://www.strava.com/athlete/download_my_account). Ask for the downloaded ZIP, extracted directory, or `activities.csv` path only when it is not already known.

3. Import locally and rebuild:

```bash
"$COACH_CLI" import-strava-export /path/to/strava-export.zip
"$COACH_CLI" refresh
```

The Training Center Connections view can also upload the ZIP or CSV into ignored workspace storage.

4. If the rider wants to continue without activity history, record the explicit choice:

```bash
"$COACH_CLI" onboarding-choice activities none
```

## Validation

Check:

- `strava/activities.json`
- `strava/state.json`
- `strava/streams/<activity_id>.json` for parsed FIT, TCX, or GPX files
- `strava/laps/<activity_id>.json` when device laps were present
- `derived/training_center.html`

Report parsed, missing, unsupported, corrupt, and preserved-existing recording counts accurately. Original recording files do not reconstruct Strava-specific segment efforts.

## Rules

- Use only athlete-provided local files.
- Never ask for provider credentials.
- Keep raw archives and samples out of model context.
- Do not stage or publish raw activity files.
- Do not claim the dashboard is rebuilt until the build command succeeds.

## Response contract

End with the archive import state, activity and recording coverage, whether the Training Center was rebuilt, and the exact next missing input.
