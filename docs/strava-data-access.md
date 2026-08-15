# Strava data access

Gradient Ascent imports Strava data from the athlete's official account archive.

Request the archive from [Strava's account download page](https://www.strava.com/athlete/download_my_account). When it is ready, provide one of:

- the downloaded ZIP
- the extracted export directory
- `activities.csv`

Use the Training Center Connections view or run:

```bash
gradient-ascent import-strava-export /path/to/export.zip
```

## What is imported

The activity index is normalized into `strava/activities.json`. Private activity descriptions are intentionally not copied into the normalized index.

When `activities.csv` references a FIT, TCX, or GPX recording, Gradient Ascent parses it locally. Supported recording output includes timestamps, GPS coordinates, altitude, distance, heart rate, cadence, temperature, power, and device laps when present.

Normalized streams are written under `strava/streams/`; laps are written under `strava/laps/`. Existing richer files are preserved instead of overwritten.

The importer reports missing, unsupported, and corrupt recordings without discarding the activity summary history.

## Refreshing history

Import a newer official archive to advance local Strava history. The importer merges by activity ID and preserves richer existing fields.

## Security boundary

Gradient Ascent does not implement a Strava developer application, OAuth callback, API-token store, or background Strava API sync. The archive and parsed files remain in the athlete workspace and are ignored by its generated `.gitignore`.
