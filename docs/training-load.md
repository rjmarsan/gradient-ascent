# Training load and ride labels

Gradient Ascent keeps training load honest about its source. Imported normalized
power (NP), intensity factor, and Training Stress Score (TSS) take precedence over
locally calculated values. A valid single FIT session supplies whole-activity
NP/TSS/IF and device timer duration when present; lap NP values are never averaged
into a substitute whole-ride NP. If TSS is missing but source NP is available,
Gradient Ascent can calculate it using the device timer duration, or moving time
when no valid timer duration is available, and the FTP configured for the activity's local date.
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
Effective-dated FTP changes apply only from their stated date onward. Before the
first dated entry, the previous configured FTP is retained as a **legacy calculation
baseline**, not presented as a recovered historical test result. Profiles without
dated history retain the original single-value behavior. Different device
processing, missing samples, or an older FTP can still produce a different result
from the score originally shown by your device or TrainingPeaks. See TrainingPeaks'
[NP explanation](https://help.trainingpeaks.com/hc/en-us/articles/204071804-Normalized-Power)
and [TSS definition](https://www.trainingpeaks.com/learn/articles/estimating-training-stress-score-tss/).

## Change FTP without rewriting earlier load

Inspect the private history, then record an explicitly accepted value and date:

```bash
.codex/bin/gradient-ascent ftp-history --date 2026-06-30
.codex/bin/gradient-ascent set-ftp --watts 250 --effective-date 2026-07-01 \
  --reason "Accepted threshold test" --change-key accepted-ftp-july
```

These numbers are a synthetic command example. `set-ftp` records the change in
official plan history and rebuilds local insights and the dashboard without a
provider fetch. Use `--expected-profile SHA256` with the fingerprint returned by
`ftp-history` to reject a stale edit. A conflicting entry on the same date requires
`--replace-date`; identical retries can reuse `--change-key`. Future effective dates
are not accepted. `--no-rebuild` saves the decision without rebuilding immediately.

The version-1 `ftp_history` inside `plan/athlete.json` contains `baseline_w` and
sorted `{effective_date,ftp_w}` entries. The baseline preserves the previous setting
and is unknown if none was valid. Do not manufacture historical measurements or
edit the current scalar independently of its history. Recorded source TSS/IF still
wins; calculated activity details identify their actual FTP basis. Power-stream NP
is cached independently of FTP, so changing FTP does not rewrite original recordings.

Explicit-watt structured-workout forecasts use the workout date's FTP. Percentage
targets remain percentages, and neither existing watt prescriptions nor weekly TSS
budgets are automatically rescaled. Budget weeks whose FTP basis changes become
`needs_review`; unaffected earlier weeks retain their fingerprints. Review the
workouts and deliberately reapprove affected budgets using the normal plan-history
workflow. Calendar re-import preserves the dated profile. These new authoring
commands require the same secure filesystem support as official plan history.

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

## Season chart: Plan or CTL · ATL

The season chart opens in **Plan**: weekly source targets, coach-authored budgets,
or complete prescribed-session totals, compared with recorded weekly TSS. Its
ranges are intentional planning ranges, not statistical confidence intervals or
measured fitness. Switch to **CTL · ATL** to see recorded daily load trends and,
when explicit future daily targets exist, a separately labeled conditional
projection. The two views use different units: weekly TSS versus TSS/day.

### Recorded CTL and ATL

The CTL · ATL view uses recorded daily TSS to model longer-term Chronic Training Load
(CTL) and shorter-term Acute Training Load (ATL). The method
`ctl_atl_daily_ewma_v1` follows the current TrainingPeaks Help Center formulas:

```text
CTL_today = CTL_yesterday + (recorded_TSS_today - CTL_yesterday) / 42
ATL_today = ATL_yesterday + (recorded_TSS_today - ATL_yesterday) / 7
```

Both values are expressed in TSS/day. They are exponentially weighted daily load
averages, not simple 42-day or seven-day means. A date's CTL and ATL include the
recorded load available for that date. They are useful training-load models, not
direct measurements of fitness, fatigue, readiness, or medical status. Different
source history, thresholds, initialization, and implementation conventions can
produce different numbers from a vendor's chart; Gradient Ascent does not promise
exact TrainingPeaks parity. See the official [CTL](https://help.trainingpeaks.com/hc/en-us/articles/204071884-Fitness-CTL-)
and [ATL](https://help.trainingpeaks.com/hc/en-us/articles/204071894-Fatigue-ATL) definitions.

The calculation starts from zero immediately before the first supported recorded
score, including a genuine zero score. This is a disclosed mathematical starting
point, not an estimate of the athlete's earlier fitness. All available earlier
history is processed before the visible season is selected; changing the displayed
date range does not reset the model. The remaining influence of the zero seed is
tracked, and limited history stays qualified. More complete historical recordings
can improve the model. [TrainingPeaks initialization guidance](https://help.trainingpeaks.com/hc/en-us/articles/230903988-Estimate-Starting-Fitness-CTL)

A day without a recording contributes zero **recorded** TSS, which is not proof
that the athlete rested. A known unscored activity keeps its score missing; a
partially scored day contributes only its available subtotal. The model preserves
a known-incomplete-history warning even after those days leave the recent 7- or
42-day window. It does not invent the missing load or alter recorded TSS.

The model's `through_date` identifies the last build date. Today's value can be a
recorded-so-far value, and an older open dashboard does not gain new observations
just because the clock advances. Refresh to rebuild. Planned sessions never become
completed training merely because they appear on the calendar.

### Conditional future projection

`ctl_atl_daily_projection_v1` uses the same 42- and seven-day recurrence, beginning
with today's valid recorded CTL/ATL state. **It starts tomorrow; the remainder of
today's plan is excluded.** A full-day target is never added to today's recorded
load. If the recorded model is stale or has no supported starting score, a
projection is unavailable until that is resolved.

Each projected day needs an explicit imported daily TSS target, an explicit rest
day, a fully specified structured-workout calculation, or a current coach-authored
daily allocation. Genuine zero is valid. Source conflicts must be resolved rather
than blended or counted twice. The projection stops at the first unspecified day;
it does not jump to a later known workout or spread a weekly budget across the gap.
The recorded model's incomplete-history and zero-seed qualifications still apply,
and a scenario that depends on a provisional target remains provisional. These
curves describe what the stated plan would imply, not what the rider has done or
a promise of future performance.

## Planned load

Weekly coaching budgets remain separate from CTL/ATL. Week totals and tooltips
distinguish source targets, coach decisions, fully prescribed session calculations,
recorded scores, and incomplete load. TSS is training load, not measured fitness
or a quota to fill. Missing scores stay missing, genuine zero scores remain zero,
and the underlying values retain their precision. Current and future weeks still
identify missing or review-needed budgets. A past week that never had a budget
instead describes its evidence: **Recorded**, **Load incomplete**, **No scored load**,
or **No recordings**. An existing stale budget still needs review; this is not
permission to invent historical targets.

A day with recorded activity shows recorded
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
strings), `override_source` (a boolean), and the daily allocation described below.
Omit the range for a single target.
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

#### Optional daily allocation

A budget may include `daily_tss`, a complete list of `{date, target_tss}` entries
for every date in its source week. Each day may also have a nonblank `rationale` of
at most 1,024 UTF-8 bytes. Daily targets are finite JSON numbers from 0–21,600;
their sum must equal the weekly **central target**, within a small numeric tolerance.
Include rest days explicitly as zero. A range or ceiling is not automatically
allocated. The [synthetic budget example](../examples/calendar/sample-tss-budgets.json)
shows the complete shape.

Daily allocations are deliberate full-day planning decisions, including any
provisional race or recovery assumptions. They are not FIT workouts, interval
durations, or device instructions. Existing explicit daily source prescriptions,
including rest, take priority unless the rider deliberately sets
`override_daily_source: true`. That permission is separate from `override_source`,
which concerns the weekly source target. A conflicting allocation is rejected
without the corresponding explicit override.

Use the existing `update-tss-budgets --file DRAFT` command to save allocations.
Omitting `daily_tss` preserves an existing allocation if the weekly central target
has not changed; use `daily_tss: null` to clear it explicitly. Changing the central
target requires a replacement allocation or an explicit clear. The allocation
shares its budget's revision, provisional/confirmed status, and changed-plan
review requirements. Stale or orphaned allocations are not used for projection.
Use `tss-budget-status --daily` to inspect current dated allocations and their
provenance; this is opt-in, while ordinary status remains aggregate-only.

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
