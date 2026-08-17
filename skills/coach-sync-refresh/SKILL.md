---
name: coach-sync-refresh
description: Sync explicitly enabled Ride with GPS, refresh local imports, and rebuild coaching summaries and the Training Center.
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

Normal refresh first fetches recent changes through Ride with GPS's actual official
`ride` CLI if the rider explicitly enabled it. It then re-imports configured Apple
Health and Garmin local paths and rebuilds canonical records, daily and weekly
insights, progress output, and the Training Center once. Use `refresh --local-only`
when the rider asks for an offline rebuild. Loading the dashboard and `ride status`
are offline; `ride check` explicitly contacts the vendor to verify the session.

Do not enable or install a provider during refresh without a separate rider choice.
Use `$coach-setup-activities` for a new Ride with GPS connection. If the rider asks
for older history, `ride sync --history` advances a bounded, resumable scan; report
remaining work and repeat until complete only within that request. `ride disable`
stops automatic requests without deleting history or changing vendor credentials.

## Validation

Inspect `derived/post_sync_summary.json` and report:

- each local import status
- Ride with GPS listed/imported/existing/updated counts and any sync failure, when enabled
- first and last source dates
- canonical activity and recovery counts
- recent weekly gross hours, meaningful ride hours, excluded short-fragment hours, kJ, and estimated TSS
- whether `derived/training_center.html` was regenerated
- any missing or invalid configured local path

Do not call the dashboard refreshed if the refresh command failed.

## Rules

- Use local athlete-provided files or explicitly enabled official Ride with GPS sync.
- Never ask for provider credentials.
- The vendor CLI owns Ride with GPS OAuth and tokens; do not read its configuration.
- Ride with GPS consent does not authorize live Strava or Garmin access.
- Keep raw archives, health files, and sync manifests out of model context.
- Preserve current data when an optional source is absent.
- Missing recovery data must limit the confidence of recovery advice.

## Response contract

Keep the report short: source cutoff dates, import errors, rebuilt artifacts, current weekly load, and the next concrete action.
