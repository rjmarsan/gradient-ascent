# Data model

Gradient Ascent separates source files, normalized records, coaching configuration, and generated views.

## Source-normalized records

`canonical/activities.json` contains activities normalized from Strava, standalone activity recordings, Apple Health, and optional provider-neutral companion manifests. Common fields include:

- stable provider-qualified ID
- local and UTC start timestamps
- sport type
- moving and elapsed time
- distance and elevation gain
- heart rate, power, work, and estimated training load when available
- source provider and confidence

`canonical/resolved_activities.json` applies duplicate resolution across providers. Strava wins a close duplicate, followed by a standalone recording, Apple Health, and an optional companion source, because reviewed built-in sources usually provide more predictable cycling detail.

`canonical/recovery.json` contains daily recovery observations from Garmin Connect, Apple Health, and optional companion manifests with a shared shape:

- date
- resting heart rate
- HRV
- sleep duration and score
- readiness or recovery score
- average stress
- source provider

Missing signals remain `null`; they are not estimated.

`canonical/activity_links.json` records duplicate candidates and the selected primary record.

## Coaching configuration

`plan/athlete.json` stores the rider profile, units, time zone, experience, sensors, constraints, availability, and optional physiological values such as FTP.

`plan/goals.md` stores decision-driving goals in a readable contract. `plan/events.json` stores structured priority events. `plan/weeks.json` and `plan/phases.json` store an imported or authored training plan.

`plan/daily_notes.json`, `plan/coach_notes.json`, and `plan/dashboard_labels.json` contain rider and coach annotations.

## Source storage

`strava/activities.json` is the local activity index. `strava/streams/` and `strava/laps/` contain normalized recording details parsed from FIT, TCX, or GPX files.

`recordings/activities.json`, `recordings/streams/`, and `recordings/laps/` contain normalized rides imported by dropping standalone FIT, TCX, or GPX files onto the Training Center.

`garmin/YYYY-MM-DD.json` contains a normalized day produced from a local Garmin Connect export.

`apple_health/workouts.json` and `apple_health/recovery.json` contain normalized Apple Health records.

`integrations/<provider-id>/manifest.json` contains a validated, versioned local activity/recovery snapshot imported from a separate companion application. Provider IDs and record IDs are validated, provenance is preserved, and the entire directory is Git-ignored. Gradient Ascent does not run companions, contact providers, or read provider credentials.

`connections/config.json` stores non-secret local import paths. It never stores credentials.

## Generated outputs

`derived/activities.json`, `derived/daily.json`, and `derived/weekly.json` power coaching summaries and the Training Center.

`derived/post_sync_summary.json` reports import outcomes, source date coverage, canonical counts, and generated artifacts.

`derived/training_center.html` and `derived/training_center_data.js` are rebuilt local assets. They may contain athlete information and should remain inside the private workspace.
