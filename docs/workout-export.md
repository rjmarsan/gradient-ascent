# Export a planned schedule or workout

Gradient Ascent can create local, user-owned downloads from your private athlete
workspace. Exporting does not connect a provider account, upload a plan, publish a
website, or change your device's calendar.

## Choose the right export

| Format | What it contains | What it does not do |
| --- | --- | --- |
| ZIP | A readable `index.html`, `schedule.ics`, `schedule.csv`, `manifest.json`, `README.txt`, and a FIT file for each explicitly structured workout | Upload or schedule anything automatically |
| ICS | Calendar entries, races, and events for the selected dates | Control a trainer or run intervals on a cycling computer |
| CSV | A readable, spreadsheet-safe schedule including event status | Define executable intervals from prose |
| FIT | One selected cycling workout's steps, durations, and targets | Represent a completed ride, navigation route, or device-calendar appointment |

In the Training Center, choose **More > Export planned schedule**, select the date
range, and choose ZIP, ICS, or CSV. The equivalent CLI commands are:

```bash
.codex/bin/gradient-ascent export-plan --format zip --start 2026-09-01 --end 2026-09-07
.codex/bin/gradient-ascent export-plan --format ics --start 2026-09-01 --end 2026-09-07
.codex/bin/gradient-ascent export-plan --format csv --start 2026-09-01 --end 2026-09-07
.codex/bin/gradient-ascent export-plan --format fit --workout example-tempo
```

The default format is ZIP, written under the private workspace's `exports/planned/`
directory. Use `--out PATH` to choose a destination. Existing files are preserved
unless you explicitly pass `--overwrite`. A ZIP's workout files are named
`workouts/<date>-<id>.fit`. Inspect the manifest and readable schedule before sharing
or transferring the bundle.

The schedule combines independent entries from `plan/weeks.json`, `plan/events.json`,
and `plan/workouts.json`. Races and other events remain calendar-only, with their
confirmed, tentative, or cancelled status, location, and priority preserved. A
skipped event stays cancelled; an undecided event is not silently promoted into a
commitment. Same-date entries from different sources remain separate.

## Author explicit workout steps

The existing weekly plan can contain useful human instructions, but a phrase such as
“tempo if fresh” is not an exact device prescription. Weekly prose remains
calendar-only. Gradient Ascent does not invent interval durations, infer FTP or watts,
or silently replace a weekly entry with a structured workout.

Store independently authored structured workouts in `plan/workouts.json` inside the
private athlete workspace. See the complete, synthetic
[structured workout example](../examples/workouts/structured-workouts.json). Review
its date and targets before use; it is a format example, not a personalized plan.
If the workspace already has this file, merge the intended entries instead of
replacing existing work.

The file has a `version` of `1` and a `workouts` array. Each workout needs a unique
`id`, ISO `date`, `name`, `sport: "cycling"`, and explicit `steps`. IDs use 1–80
lowercase letters, digits, or hyphens, starting with a letter or digit. `description`
is optional calendar text of at most 64 KiB; `device_description` is an optional,
shorter device cue.
A simple step contains:

```json
{
  "name": "Steady",
  "duration_s": 300,
  "intensity": "active",
  "target": {
    "type": "power",
    "unit": "percent_ftp",
    "low": 85,
    "high": 95
  }
}
```

Use `{"type": "open"}` when there is deliberately no power target. Power ranges use
integer `percent_ftp` values from 0–300 or integer `watts` values from 1–3000, with
`low <= high`. Zero-watt FIT targets are ambiguous; use an open target or explicit
zero-percent target instead. Supported intensities are `warmup`, `active`,
`recovery`, `cooldown`, and `rest`.

Repeats use `{"repeat": 3, "steps": [...]}` with simple steps inside and an integer
repeat count from 1–50. Nested repeats are unsupported. After expansion, a workout
must contain 1–50 steps, each lasting
1–86,400 seconds, with a total duration of at most 24 hours. Workout names, step
names, and `device_description` must fit within 254 UTF-8 bytes; unsupported values
are rejected rather than silently truncated. Some devices display shorter names.
Garmin's consumer workout builder also limits workouts to 50 steps, and device
storage/display limits vary. [Garmin workout guidance](https://support.garmin.com/en-IN/?faq=wZ52AaLbLG2GC1Lxu2l4k7&tab=topics)

## Transfer to a calendar or device

### Calendar

Open or import `schedule.ics` in your calendar application. For example, Apple
Calendar supports importing `.ics` files. This is a static snapshot, not ongoing
calendar synchronization; repeated imports may behave differently across calendar
apps. Check the imported event statuses and review duplicates before importing again.
The calendar service you choose may store the imported schedule on its servers;
choose the destination deliberately.
[Apple Calendar import instructions](https://support.apple.com/guide/calendar/icl1023/mac)

### Garmin Edge

For a compatible Edge, connect with a USB data cable, copy the exported workout FIT
file into `Garmin/NewFiles`, and safely disconnect. Check that the device recognizes
the workout and its targets before riding. Compatibility and USB/MTP support depend
on the model. A FIT Workout contains executable instructions; copying it does not
promise that it will appear on a particular Garmin Connect calendar date.
[Garmin manual file transfer](https://support.garmin.com/en-MY/?faq=rzvP53Si4O3barYoXzw5L7),
[Garmin FIT Workout format](https://developer.garmin.com/fit/file-types/workout/)

### Wahoo ACE, BOLT 3, and ROAM 3

Wahoo documents importing planned-workout FIT files into the device's `plans`
folder over USB/MTP. Enable USB access in the Wahoo app's device settings and use a
data-capable cable; macOS needs an MTP client. A manually imported workout is
available on the device, but Wahoo does not manage or back it up through its app.
Verify the workout on the device before using it.
[Wahoo manual transfer instructions](https://support.wahoofitness.com/hc/en-us/articles/28544587410450-Connect-an-ACE-BOLT-3-and-ROAM-3-to-a-desktop-laptop-computer)

For older ELEMNT, BOLT 1/2, or ROAM 1/2 models, use the supported planned-workout
provider workflow or consult that model's documentation. This export does not claim
universal FIT, ERG, or MRC sideload compatibility for older devices.
[Wahoo planned-workout guidance](https://support.wahoofitness.com/hc/en-us/articles/115001223770-Planned-Workouts-ELEMNT)

## Cloud sync and optional hosting

Automatic device-calendar delivery is a separate feature. Garmin's Training API
and Wahoo's Cloud API require their own developer access and user authorization;
this local export does not provide either integration. The existing Ride with GPS
connection remains read-only. [Garmin Training API](https://developer.garmin.com/gc-developer-program/training-api/),
[Wahoo Cloud API access](https://developers.wahooligan.com/cloud)

You can open a ZIP's `index.html` locally. If you later want a hosted page or Site,
first review the exported plan, choose its audience and privacy controls, and
explicitly approve that upload. There is no automatic hosting or publishing. Treat
the bundle as private athlete data: it can reveal future plans, dates, and training
targets even though it contains no provider credentials.
