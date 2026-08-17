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

The season chart follows the actual plan's weekly TSS target or modeled prescribed
load, preserves its range, and overlays recorded weekly TSS. The selected-week
readout and tooltips distinguish source targets, coach decisions, fully prescribed
session calculations, recorded scores, and incomplete load. TSS is training load,
not measured fitness or a quota to fill.
Weeks without supported scores remain gaps, genuine zero scores remain zero,
future recordings are not projected, and the current week is labeled as in
progress. The underlying values retain their precision. Week totals retain the
scheduled-versus-recorded comparison. A day with recorded activity shows recorded
stats in its Week card instead of repeating the original plan summary; unrecorded
days keep their scheduled stats. Meaningful missing-load warnings and full score
provenance remain available.

Daily calendar imports preserve `Planned TSS` and explicit duration columns,
including numeric `Duration (min)` values. Weekly sheets can supply `TSS Target`.
Cancelled sessions do not contribute to forecasts; an ambiguous imported daily
total is not reused after one of its sessions is cancelled.

For a week, an imported TSS target wins unless a coach budget explicitly overrides
it. Otherwise an active coach-authored budget is used, followed by a complete sum
of genuinely prescribed daily targets or fully specified structured workouts.
Rough session estimates are not promoted into that sum. An hours target remains a
separate scheduling constraint: the app does not turn it into a TSS budget, assign
missing daily load, invent workout durations, or create device workouts. Without
an explicit budget or complete prescribed load, weekly TSS stays unspecified.
The smaller cumulative TSS chart stops at an unspecified daily load instead of
silently treating it as zero; the weekly budget is never spread across that gap.

### Coach-authored budgets

A coach budget is a deliberate plan decision informed by goals, recent complete
scored training, recovery, race priorities, and intended sessions. It can be
provisional while those decisions are still being resolved. Do not invent targets
for old weeks to make a chart look complete, or add riding merely to close a TSS
shortfall.

The private `plan/tss_budgets.json` stores these decisions separately from imported
calendar files, so reimport does not erase them. Draft updates outside the workspace
and install them through the validated, locked command:

```bash
.codex/bin/gradient-ascent update-tss-budgets --file /path/to/tss-budgets-draft.json
.codex/bin/gradient-ascent tss-budget-status
```

A draft has `{"version": 1, "budgets": [...]}`. Each entry names the exact
`start_date` and `end_date` of an existing plan week and supplies `target_tss` and
a meaningful `rationale`. Optional fields are `range: {"min": ..., "max": ...}`,
`ceiling_tss`, `status` (`provisional` or `confirmed`), `conditions` (a list of
strings), and `override_source` (a boolean). Omit the range for a single target.
The default status is `provisional`, and the default override is `false`. A range
must contain its target; a ceiling must cover the entire range. Numbers must be
finite JSON numbers from 0–151,200, not booleans or numeric strings. That upper bound
is an input-validation limit, not a recommended training load. Each draft allows
at most 520 non-overlapping existing weeks, a nonblank rationale of at most 4,096
UTF-8 bytes, and up to 16 nonblank conditions of at most 1,024 bytes each.
The ceiling is a separate coaching limit, not the upper end of an automatically
generated forecast. See the complete
[synthetic budget example](../examples/calendar/sample-tss-budgets.json), which
matches the [sample weekly calendar](../examples/calendar/sample-training-calendar.csv).
Its dates and numbers demonstrate the format; they are not a personalized training
plan. Do not import it into an existing athlete plan without reviewing and replacing
the example decisions.

The normal update preserves unrelated coach budgets and rebuilds local insights
and the dashboard from the current plan. It does not run configured imports or
contact providers.
Use `--replace` only when intentionally replacing the complete coach-budget set;
`--no-rebuild` defers both local rebuilds. Imported source targets are not silently
overridden: a conflicting draft fails unless it explicitly sets
`override_source: true`. A change to relevant goals, profile, events, or prescribed
plan can mark a budget `needs_review`; it stops being active until reviewed and
reissued. A budget for a removed week becomes `orphaned`.
Use `tss-budget-status --fingerprints` to obtain the current week dates and plan
fingerprints without printing plan text. Automated drafts should copy the matching
value into `expected_plan_fingerprint`; reapproving a changed plan requires it.
The application records revisions and authorship time. Do not invent or copy those
stored metadata fields into a new draft.
Status reports aggregate counts for total, current, review-needed, orphaned,
provisional, and confirmed budgets without printing their private rationale.

### Fully prescribed sessions

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

### Separate rough session estimates

A session with an explicit total duration and supported cycling intensity can
still receive a labeled rough estimate using `hours × IF² × 100`. These estimates
do not become a weekly budget. Their ranges describe application assumptions about
the whole session, including easier riding between efforts, not statistical
confidence, device instructions, or a predicted exact device score.

The whole-session IF assumptions are:

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

An explicit rest day can be zero; a session without enough information stays
unknown. Recovery, warmup, and cooldown durations in an interval prescription are
not treated as the whole workout. A stated total duration or imported duration is
needed before estimating its full-session load.

## Ride titles

Meaningful provider titles and explicit rider-authored names are preserved. When a
title is only a generated identifier, date, filename, or generic label such as
`Private ride`, the dashboard may use a useful planned-workout name instead. This
is a display fallback: it does not rename the source recording, reveal a masked
private title, or establish that you followed the planned intervals.
