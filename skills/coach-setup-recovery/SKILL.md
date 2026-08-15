---
name: coach-setup-recovery
description: Import athlete-provided Apple Health or Garmin Connect exports into Gradient Ascent and verify normalized recovery coverage.
---

# Coach recovery setup

## Purpose

Recovery data is optional. This skill imports local Apple Health and Garmin Connect files without provider login.

## Workflow

Resolve the workspace-local launcher:

```bash
WORKSPACE_DIR="${COACH_WORKSPACE_DIR:-${COACH_DATA_DIR:-$PWD}}"
COACH_CLI="$WORKSPACE_DIR/.codex/bin/gradient-ascent"
test -x "$COACH_CLI" || COACH_CLI="$(command -v gradient-ascent)"
```

1. Inspect current local source state:

```bash
export COACH_WORKSPACE_DIR="${COACH_WORKSPACE_DIR:-$PWD}"
"$COACH_CLI" connections-status --json --summary
```

2. For Apple Health, accept an export directory or `export.xml`:

```bash
"$COACH_CLI" import-apple-health-export /path/to/export.xml
"$COACH_CLI" connections-set apple_health --field export_path=/path/to/export.xml
```

3. For Garmin Connect, accept an official account export directory containing `DI_CONNECT`:

```bash
"$COACH_CLI" import-garmin-export /path/to/garmin-export
"$COACH_CLI" connections-set garmin --field export_path=/path/to/garmin-export
```

Saving the path lets the Training Center refresh button re-import the current local files.

4. Rebuild:

```bash
"$COACH_CLI" refresh
```

## Rules

- Never ask for provider credentials.
- Keep raw health exports and provider files local and out of model context.
- Missing signals must remain missing; do not infer HRV, sleep, or readiness values.
- Recovery setup does not block the rest of onboarding.

## Response contract

End with each local export's import state, recovery date coverage, the exact missing path or format problem, and whether the Training Center was rebuilt.
