# Training load and ride labels

Gradient Ascent keeps training load honest about its source. Imported normalized
power (NP), intensity factor, and Training Stress Score (TSS) take precedence over
locally calculated values. If TSS is missing but source NP is available, it can
estimate TSS from the ride's moving time and your currently configured FTP.

For a local recording without source NP, Gradient Ascent can estimate NP from its
recorded power stream. It builds time-weighted one-second samples, calculates
complete 30-second rolling averages, averages their fourth powers, and takes the
fourth root. The calculation requires at least ten minutes of usable power and
enough complete rolling windows. Invalid samples and gaps longer than five seconds
break the rolling window; missing time is neither filled with zero nor extended
across a pause.

Estimated TSS is `hours × (NP / FTP)² × 100`. For a power-stream estimate, the
duration is capped at the lesser of recorded power time and reported moving time.
It uses your **current configured FTP**, not an inferred historical FTP. Changing
FTP can therefore change a locally estimated score after refresh. Different device
processing, missing samples, or an older FTP can also produce a different result
from the score originally shown by your device or TrainingPeaks. See TrainingPeaks'
[NP explanation](https://help.trainingpeaks.com/hc/en-us/articles/204071804-Normalized-Power)
and [TSS definition](https://www.trainingpeaks.com/learn/articles/estimating-training-stress-score-tss/).

The Training Center marks calculated load with `~`. A **partial** label means some
power coverage or activity load is missing; totals include only supported values,
without extrapolating the missing load. If the source has no score and there is not
enough usable power, duration, or a valid FTP, the score stays missing rather than
becoming zero. These estimates are coaching context, not a substitute for how you
feel or evidence that a prescribed workout was completed.

## Ride titles

Meaningful provider titles and explicit rider-authored names are preserved. When a
title is only a generated identifier, date, filename, or generic label such as
`Private ride`, the dashboard may use a useful planned-workout name instead. This
is a display fallback: it does not rename the source recording, reveal a masked
private title, or establish that you followed the planned intervals.
