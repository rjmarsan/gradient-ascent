# Training load and ride labels

Gradient Ascent keeps training load honest about its source. Imported normalized
power (NP), intensity factor, and Training Stress Score (TSS) take precedence over
locally calculated values. A valid single FIT session supplies whole-activity
NP/TSS/IF and device timer duration when present; lap NP values are never averaged
into a substitute whole-ride NP. If TSS is missing but source NP is available,
Gradient Ascent can calculate it using the device timer duration, or moving time
when no valid timer duration is available, and your currently configured FTP.
[Garmin's FIT activity format](https://developer.garmin.com/fit/file-types/activity/)

For a local recording without source NP, Gradient Ascent can estimate NP from its
recorded power stream. It builds time-weighted one-second samples, calculates
complete 30-second rolling averages, averages their fourth powers, and takes the
fourth root. The calculation requires at least ten minutes of usable power and
enough complete rolling windows. Missing heart-rate samples do not discard valid
power samples. Real zero watts and coasting remain in the calculation; only
explicit FIT timer events identify paused samples. Invalid samples and gaps longer
than five seconds break the rolling window; missing time is neither filled with
zero nor extended across a pause. The recorded-power method is
`power_stream_30s_v3`.

Estimated TSS is `hours × (NP / FTP)² × 100`. For a power-stream estimate, the
duration is capped at the lesser of usable recorded power time and the reported
load duration: device timer time when available, otherwise moving time. Moving
time still determines the dashboard's riding-hours totals.
It uses your **current configured FTP**, not an inferred historical FTP. Changing
FTP can therefore change a locally estimated score after refresh. Different device
processing, missing samples, or an older FTP can also produce a different result
from the score originally shown by your device or TrainingPeaks. See TrainingPeaks'
[NP explanation](https://help.trainingpeaks.com/hc/en-us/articles/204071804-Normalized-Power)
and [TSS definition](https://www.trainingpeaks.com/learn/articles/estimating-training-stress-score-tss/).

Ride with GPS imports use the provider's separate moving and total durations;
stopped time is not counted as moving just because the next track point is in
motion. Original timestamps and recording files stay intact. Older imports gain
these source durations on their next authorized sync, and subsequent local-only
refreshes reuse them. See [Ride with GPS's duration definitions](https://support.ridewithgps.com/hc/en-us/articles/4419571047835-Activity-Metrics).

The Training Center rounds displayed TSS to whole numbers and identifies its origin
as **Source** or **Calculated**, instead of putting a `~` before the number. Coverage
and missing load are separate: a note such as **97% power coverage** or **1 ride
without load** explains what is incomplete. Totals include only supported values,
without extrapolating the missing load. Missing-load counts cover cycling activities
with positive moving or elapsed time, not unscored walks or zero-duration entries.
Any valid source-reported score still contributes to the total. Power coverage is
weighted by the reported load durations of locally power-scored rides. A
source-reported score alone does not establish complete telemetry, and this ratio
does not prove that every timer-active or moving second was captured. If the source
has no score and there is not enough usable power, duration, or a valid FTP, the
score stays missing rather than becoming zero. These estimates are coaching
context, not a substitute for how you feel or evidence that a prescribed workout
was completed.

### Repairing older local recordings

Local refresh can rebuild older normalized recordings from retained, digest-verified
original files. It preserves source identity and does not contact a provider. The
refresh result's `imports.recording_repair` contains aggregate `repaired`, `current`,
`unavailable`, `errors`, and `unsupported` counts. Missing or oversized
originals stay unrepaired; the app does not invent the lost samples. Optional
original-file retention is limited to 64 MiB per file and does not reduce the
existing upload limit. Use `gradient-ascent refresh --local-only` for an explicitly
offline rebuild.

## Planned load

The season chart follows the central planned TSS estimate, shades its range, and
overlays recorded weekly TSS. Source-target and forecast counts explain how much
of the trajectory is explicitly prescribed versus modeled. The selected-week
readout and tooltips identify source targets, forecasts, calculated scores, and
incomplete load. TSS is training load, not measured fitness.
Weeks without supported scores remain gaps, genuine zero scores remain zero,
future recordings are not projected, and the current week is labeled as in
progress. The underlying values retain their precision. Scheduled and recorded
hours and TSS remain visible together in the week and day views.

Daily calendar imports preserve `Planned TSS` and explicit duration columns,
including numeric `Duration (min)` values. Weekly sheets can supply `TSS Target`.
Cancelled sessions do not contribute to forecasts; an ambiguous imported daily
total is not reused after one of its sessions is cancelled.

An explicit source TSS target or range takes priority. Otherwise, a session with an
explicit total duration and a supported cycling intensity can receive a
**calculated forecast**, using `hours × IF² × 100`. Its low/high range describes
uncertainty about the whole session, including easier riding between efforts; it
is not an interval prescription or a prediction of the exact device score.

The current whole-session IF assumptions are:

| Session | IF range |
| --- | --- |
| Recovery | 0.45–0.60 |
| Endurance | 0.60–0.75 |
| Openers | 0.55–0.75 |
| Tempo | 0.70–0.85 |
| Sweet spot | 0.75–0.90 |
| Threshold or VO2 | 0.75–0.95 |
| Criterium | 0.75–1.00 |
| Road race | 0.70–0.95 |
| Dirt race | 0.65–0.90 |

These are application assumptions, not athlete-specific measurements. An explicit
rest day can be zero; a session without enough information stays unknown.
For an interval prescription, recovery, warmup, and cooldown durations are not
treated as the whole workout. A stated total duration or imported duration is
needed before forecasting its full-session load.

For a week, an explicit TSS target wins, followed by a complete sum of daily
targets and forecasts. If daily load is incomplete but the source gives a weekly
hours budget, Gradient Ascent can show a broader forecast using whole-week IF
0.55–0.85. It does not distribute that budget among missing days, invent workout
durations, or create device workouts. Without a usable source budget, an incomplete
weekly total stays unknown.
The smaller cumulative TSS chart stops at an unspecified daily load instead of
silently treating it as zero; the weekly budget is never spread across that gap.

Explicit workouts in `plan/workouts.json` remain independent of calendar prose.
Their step durations give an exact planned duration; fully specified power targets
can support a separate, labeled power-model forecast. The
`structured_power_30s_v2` method applies complete 30-second rolling averages to the
prescribed low, midpoint, and high power targets across step boundaries, then uses
the fourth-power calculation and full prescribed duration. It does not pad the
first window. Workouts shorter than 30 seconds, open targets, or missing FTP for
watt-based targets leave TSS unknown while preserving the exact duration. This is
a forecast, not recorded telemetry. It does not silently replace or add to a prose
workout, prove completion, or change exported device instructions. See
[planned schedule export](workout-export.md).

## Ride titles

Meaningful provider titles and explicit rider-authored names are preserved. When a
title is only a generated identifier, date, filename, or generic label such as
`Private ride`, the dashboard may use a useful planned-workout name instead. This
is a display fallback: it does not rename the source recording, reveal a masked
private title, or establish that you followed the planned intervals.
