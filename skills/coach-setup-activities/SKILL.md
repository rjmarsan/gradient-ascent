---
name: coach-setup-activities
description: Set up optional Ride with GPS sync, import a local activity archive or recording, or continue without activity history.
---

# Coach activity setup

## Purpose

Use this skill after the base plugin and workspace install is healthy. Activity setup
is optional: the rider can connect Ride with GPS through its official CLI, import
local files, or continue without history.

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

2. Ask which source the rider wants. For Ride with GPS, run offline `ride status`
first. Run `ride setup` only with their approval; add `--install` only when they
explicitly approve installing the official pinned vendor CLI. Give them the printed
sign-in link to click in the correct browser profile. The vendor owns OAuth and its
token store; never request an API key, password, or token. After setup:

```bash
"$COACH_CLI" ride check
"$COACH_CLI" ride sync
"$COACH_CLI" onboarding-choice activities ridewithgps
```

Use `ride sync --history` when older history is requested. Repeat the bounded,
resumable command until its progress reports completion, or report what remains.
`ride setup --reauth` allows an explicit corrected sign-in; a workspace must not mix
different rider accounts. `ride disable` stops future automatic sync without deleting
imported rides. This does not enable live Strava or Garmin access.

3. For a Strava archive, send the rider to Strava's official [account download page](https://www.strava.com/athlete/download_my_account). Ask for the downloaded ZIP, extracted directory, or `activities.csv` path only when it is not already known.

4. Import locally and rebuild without an additional provider request:

```bash
"$COACH_CLI" import-strava-export /path/to/strava-export.zip
"$COACH_CLI" refresh --local-only
```

The Training Center Connections view can also upload the ZIP or CSV into ignored workspace storage.

5. If the rider wants to continue without activity history, record the explicit choice:

```bash
"$COACH_CLI" onboarding-choice activities none
```

## Validation

For a local archive, check file presence and compact coverage counts without printing
raw contents:

- `strava/activities.json`
- `strava/state.json`
- `strava/streams/<activity_id>.json` for parsed FIT, TCX, or GPX files
- `strava/laps/<activity_id>.json` when device laps were present
- `derived/training_center.html`

For Ride with GPS, use its aggregate sync result and `ride status`; do not print raw
API responses or recording samples. For archive imports, report parsed, missing,
unsupported, corrupt, and preserved-existing recording counts accurately. Original
recording files do not reconstruct Strava-specific segment efforts.

## Rules

- Use athlete-provided local files or explicitly enabled official Ride with GPS sync.
- Never ask for provider credentials.
- Keep raw archives and samples out of model context.
- Do not stage or publish raw activity files.
- Do not claim the dashboard is rebuilt until the build command succeeds.

## Response contract

End with the selected source's import state, activity and recording coverage, any
remaining history pages, whether the Training Center was rebuilt, and the exact next
missing input.
