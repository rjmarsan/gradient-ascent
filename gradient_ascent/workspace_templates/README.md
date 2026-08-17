# Gradient Ascent athlete workspace

This private directory contains one athlete's plan, local source data, notes, context, summaries, and Training Center.

## Coaching conversations

Open this existing private folder as a separate project in Codex Desktop and start
your coaching tasks here. No new Git repository is required. Do not move athlete data
into the public Gradient Ascent checkout or publish this workspace.

Use `$coach-advice` for training questions, `$gradient-ascent-goals` to refine your
priorities, and `$coach-note` when you want an approved insight saved. The Training
Center's **Ask Coach** button opens a new conversation in this same private folder.

## Layout

- `plan/`: profile, goals, events, plan, notes, equipment, and corrections.
- `connections/`: non-secret local import and enabled-sync configuration.
- `strava/`: activity index, laps, and streams.
- `recordings/`: normalized activities, laps, and streams from loose FIT, TCX, or GPX files.
- `garmin/`: daily recovery imported from Garmin Connect files.
- `apple_health/`: workouts and recovery imported from Apple Health.
- `integrations/`: private Ride with GPS imports and provider-neutral companion manifests.
- `canonical/`: source-normalized records.
- `derived/`: rebuilt summaries and Training Center assets.
- `context/`: narrative coaching context that should survive across tasks.
- `threads/`: concise summaries or links from important coaching tasks.
- `imports/`: private local source files.

## Common commands

```bash
.codex/bin/gradient-ascent onboarding-status --json
.codex/bin/gradient-ascent refresh
.codex/bin/gradient-ascent serve-training-center --port 8787
```

Normal refresh contacts Ride with GPS only if you explicitly enabled that connection.
Use `refresh --local-only` for an offline rebuild. In the Training Center's
**Connections** view, choose **Install and connect** or **Connect**, then click the
sign-in link in your chosen browser profile. The actual official `ride` CLI owns
OAuth and tokens; never put credentials in this workspace or a coaching prompt.

```bash
.codex/bin/gradient-ascent ride setup --install
.codex/bin/gradient-ascent ride status
.codex/bin/gradient-ascent ride check
.codex/bin/gradient-ascent ride sync --history
.codex/bin/gradient-ascent ride disable
```

`--install` explicitly approves installing the verified vendor CLI. `ride status` is
offline; **Check** tests the vendor session. Repeat **Import older rides** or
`ride sync --history` until the bounded scan reports completion. **Stop syncing** or
`ride disable` preserves imported rides and the vendor's independent login. Garmin
and Strava remain separate local-file/optional-companion sources.

Optional imports:

```bash
.codex/bin/gradient-ascent build-plan imports/training-plan.xlsx
.codex/bin/gradient-ascent import-strava-export imports/strava-export/export.zip
.codex/bin/gradient-ascent import-garmin-export imports/garmin-connect
.codex/bin/gradient-ascent import-apple-health-export imports/apple-health/export.xml
.codex/bin/gradient-ascent import-sync-manifest /path/to/companion-manifest.json
```

The Training Center binds to localhost. Use the URL it prints and verify `/api/health` before making edits.

Drag loose FIT, TCX, or GPX ride files anywhere on the Training Center page to import them locally.

This workspace may be versioned privately if the athlete chooses, but raw archives, GPS traces, health data, logs, and `.env` are ignored by default. Review every file before sharing or publishing any part of the workspace.
