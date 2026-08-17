---
name: gradient-ascent
description: Run or resume prompt-driven setup for a private Gradient Ascent workspace.
---

# Prompt-Driven Setup

## Start Or Resume

1. Confirm the intended private workspace. If the current directory is the plugin checkout, use the user's requested workspace path or default to `~/code/gradient-ascent-workspace`.
2. Resolve the CLI, then initialize without overwriting files:

```bash
COACH_CLI="$WORKSPACE_DIR/.codex/bin/gradient-ascent"
if [ ! -x "$COACH_CLI" ]; then
  COACH_CLI="$(command -v gradient-ascent || true)"
fi
if [ -z "$COACH_CLI" ]; then
  PLUGIN_DIR="${GRADIENT_ASCENT_PLUGIN_DIR:-$PWD}"
  COACH_CLI="$PLUGIN_DIR/.venv/bin/gradient-ascent"
fi
test -x "$COACH_CLI" || { echo "Run the editable local install first." >&2; exit 1; }
"$COACH_CLI" init-workspace "$WORKSPACE_DIR"
```

The initializer creates `.codex/bin/gradient-ascent` with the supported Python. Use it thereafter so fresh threads do not depend on virtual-environment activation.

3. Run the compact status command from that workspace:

```bash
export COACH_WORKSPACE_DIR="$WORKSPACE_DIR"
"$WORKSPACE_DIR/.codex/bin/gradient-ascent" onboarding-status --json
```

4. Advance only the reported `current_step`. After completing it, rerun `onboarding-status --json`. This makes interrupted setup resumable without asking the rider to repeat earlier answers.

## Prompt Steps

### Profile

Ask one grouped question for optional display name, timezone, units, disciplines, experience, realistic weekly availability, recurring constraints, and sensors (`none` is valid).

Validate and merge the answers with the deterministic profile command. Repeat `--discipline`, `--constraint`, and `--sensor` as needed; `--sensor none` records an empty sensor list.

```bash
"$WORKSPACE_DIR/.codex/bin/gradient-ascent" onboarding-profile \
  --timezone "$TIMEZONE" \
  --unit-system "$UNIT_SYSTEM" \
  --discipline "$DISCIPLINE" \
  --experience-level "$EXPERIENCE_LEVEL" \
  --weekly-availability "$WEEKLY_AVAILABILITY"
```

Preserve existing fields and never invent FTP, recovery, racing category, or sensor data.

### Goals And Events

Ask one grouped question for the rider's north star, primary decision-driving goal, why it matters, what success means, the coaching implication, and the evidence that should count. Record exactly what the rider said:

```bash
"$WORKSPACE_DIR/.codex/bin/gradient-ascent" onboarding-goals \
  --north-star "$NORTH_STAR" \
  --goal "$PRIMARY_GOAL" \
  --why "$WHY" \
  --success "$SUCCESS" \
  --coaching-implication "$COACHING_IMPLICATION" \
  --evidence "$EVIDENCE"
```

Then ask for priority events. Add each known event with an ISO date and A/B/C priority:

```bash
"$WORKSPACE_DIR/.codex/bin/gradient-ascent" onboarding-event \
  --name "$EVENT_NAME" \
  --date "$EVENT_DATE" \
  --discipline "$DISCIPLINE" \
  --priority "$PRIORITY"
```

Record an explicit source when no manual event was added. Run only the matching command:

```bash
"$WORKSPACE_DIR/.codex/bin/gradient-ascent" onboarding-choice events none       # no target event
"$WORKSPACE_DIR/.codex/bin/gradient-ascent" onboarding-choice events plan_file  # events are in the plan
```

Use `$gradient-ascent-goals` later to revise or reconcile the initial contract.

### Existing Plan

Ask whether the rider has a CSV/XLSX plan to import or wants to continue without a current plan.

For a file:

```bash
"$WORKSPACE_DIR/.codex/bin/gradient-ascent" build-plan path/to/calendar.csv
```

For no current plan:

```bash
"$WORKSPACE_DIR/.codex/bin/gradient-ascent" onboarding-choice plan none
```

Never silently replace a completed profile, goals, or local plan decisions.

### Activity History

Ask whether the rider wants optional Ride with GPS sync, has an official Strava account archive, standalone FIT/TCX/GPX ride files, or a local sync manifest from an optional companion installed separately. Keep raw activity rows, streams, and manifest payloads out of model context.

For Ride with GPS, inspect offline status first:

```bash
"$WORKSPACE_DIR/.codex/bin/gradient-ascent" ride status
```

Only after the rider chooses to connect, run `ride setup`. Add `--install` only after
they approve downloading the pinned official vendor CLI. Give the rider the printed
sign-in URL to click in their chosen browser profile; never open a profile for them
or ask for credentials. The vendor CLI owns OAuth and its token store. Then run:

```bash
"$WORKSPACE_DIR/.codex/bin/gradient-ascent" ride check
"$WORKSPACE_DIR/.codex/bin/gradient-ascent" ride sync
"$WORKSPACE_DIR/.codex/bin/gradient-ascent" onboarding-choice activities ridewithgps
```

If the rider asks for older history, run `ride sync --history` and report its bounded,
resumable progress; repeat only as needed to finish the requested import. Do not
claim complete history while more pages remain. `ride disable` stops future automatic
sync without deleting imported rides or the vendor's login. Connecting this source
does not authorize live Strava or Garmin access.

For Strava:

```bash
"$WORKSPACE_DIR/.codex/bin/gradient-ascent" import-strava-export path/to/strava-export.zip
```

For each standalone recording:

```bash
"$WORKSPACE_DIR/.codex/bin/gradient-ascent" import-activity-recording path/to/ride.fit
"$WORKSPACE_DIR/.codex/bin/gradient-ascent" onboarding-choice activities local_recordings
```

For an optional companion's versioned local sync manifest:

```bash
"$WORKSPACE_DIR/.codex/bin/gradient-ascent" import-sync-manifest path/to/manifest.json
"$WORKSPACE_DIR/.codex/bin/gradient-ascent" onboarding-choice activities external_sync
"$WORKSPACE_DIR/.codex/bin/gradient-ascent" onboarding-status --json
```

The companion owns any provider authentication and network requests. Gradient Ascent only imports the local file; never ask for provider credentials or require a companion when the rider can use an archive, standalone recordings, or no history.

If the rider wants to continue without activity history:

```bash
"$WORKSPACE_DIR/.codex/bin/gradient-ascent" onboarding-choice activities none
```

Do not ask for provider credentials. Local-file setup remains available without any
provider connection or optional companion.

### Dashboard

Build the available local view even when optional data is absent:

```bash
"$WORKSPACE_DIR/.codex/bin/gradient-ascent" refresh --local-only
"$WORKSPACE_DIR/.codex/bin/gradient-ascent" onboarding-status --json
```

Serve it when useful and report the verified localhost URL:

```bash
"$WORKSPACE_DIR/.codex/bin/gradient-ascent" serve-training-center --port 8787
```

Use the actual printed port. Fetch `/api/health` there and match its `workspace_id` to this process before reporting the URL.

Use normal `refresh` instead when the rider wants an explicitly enabled Ride with GPS
connection to fetch recent changes before the local rebuild. Recovery, health sources,
Ride with GPS, and companion sync are optional; they do not block core onboarding.

## Prompt Efficiency

- Ask for one coherent group of rider decisions at a time.
- Load only the setup skill needed for the current step.
- Prefer compact status/count output over printing source JSON.
- Never paste raw exports, streams, health payloads, sync manifests, or the full install manual into context.
- Do not repeat completed questions when setup resumes.
- Stop only for a missing user-owned file, consent-sensitive information, or a decision the rider must make.

## Safety

- Never store real athlete data in the plugin checkout.
- Never print credentials or private activity descriptions.
- Do not overwrite existing workspace files without explicit confirmation.
- Keep raw activity, health, and generated athlete files private.
- Missing data must stay missing; do not substitute plausible-looking defaults.

## Response Contract

End with:

- workspace path
- completed setup steps
- current or next step
- activity and plan coverage counts
- dashboard URL if served
- verified dashboard Workspace ID if served
- exact user input needed next, if any
