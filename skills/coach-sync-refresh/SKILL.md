---
name: coach-sync-refresh
description: Refresh configured local Gradient Ascent imports and rebuild canonical data, coaching summaries, and the Training Center.
---

# Coach sync refresh

## Workflow

Run from the private athlete workspace.

Resolve the workspace-local launcher:

```bash
WORKSPACE_DIR="${COACH_WORKSPACE_DIR:-${COACH_DATA_DIR:-$PWD}}"
COACH_CLI="$WORKSPACE_DIR/.codex/bin/gradient-ascent"
test -x "$COACH_CLI" || COACH_CLI="$(command -v gradient-ascent)"
```

1. If the rider supplied a newer Strava account archive, import it first:

```bash
"$COACH_CLI" import-strava-export /path/to/strava-export.zip
```

Without a newer archive, preserve the current Strava history and report its cutoff date.

2. If the training-plan file changed, reimport it:

```bash
"$COACH_CLI" build-plan /path/to/training-plan.xlsx
```

3. If the rider has an optional, separately installed companion and supplied a newer versioned local sync manifest, import it without printing the manifest:

```bash
"$COACH_CLI" import-sync-manifest /path/to/manifest.json
```

The companion owns its provider authentication and network access. Gradient Ascent only reads the local file; do not request credentials, install a companion implicitly, or treat an absent companion as an error.

4. Run the standard refresh:

```bash
"$COACH_CLI" refresh
```

The refresh re-imports configured Apple Health and Garmin local paths, then rebuilds canonical records, daily and weekly insights, progress output, and the Training Center.

## Validation

Inspect `derived/post_sync_summary.json` and report:

- each local import status
- first and last source dates
- canonical activity and recovery counts
- recent weekly gross hours, meaningful ride hours, excluded short-fragment hours, kJ, and estimated TSS
- whether `derived/training_center.html` was regenerated
- any missing or invalid configured local path

Do not call the dashboard refreshed if the refresh command failed.

## Rules

- Use only local athlete-provided files.
- Never ask for provider credentials.
- Keep raw archives, health files, and sync manifests out of model context.
- Preserve current data when an optional source is absent.
- Missing recovery data must limit the confidence of recovery advice.

## Response contract

Keep the report short: source cutoff dates, import errors, rebuilt artifacts, current weekly load, and the next concrete action.
