# Replace with the athlete's north star

This file is the canonical coaching contract for the athlete. Its heading hierarchy is the goal hierarchy: this top-level heading is the north star, the next level holds the main goals, and lower levels hold the supporting structures.

Keep it useful in real coaching conversations:
- Describe what the athlete is actually trying to accomplish.
- Make the hierarchy obvious from the Markdown headings.
- Keep only a small number of active main goals.
- Say what each goal should change in coaching decisions.
- Define success clearly enough that progress can be evaluated honestly.
- Do not force every meaningful goal into a fake precision metric.

The coach should read this file before giving material training, racing, recovery, or schedule advice.

## Main Goals

Use one `###` section per real decision-driving goal. These are the goals that should break ties when tradeoffs appear.

### Example: Finish my first century and feel good

- **id:** `first-century-good`
- **type:** `outcome`
- **priority:** `primary`
- **target date / horizon:** `2026 season`
- **why this matters:** I want long events to feel achievable and enjoyable, not like survival.
- **success means:** Complete 100 miles and finish feeling in control rather than merely hanging on.
- **coaching implication:** Durability, fueling, and pacing matter more here than squeezing out a small short-term FTP gain.

#### Supporting Structures

##### Build long-ride durability

- **id:** `long-ride-durability`
- **type:** `capability`
- **supports:** `first-century-good`
- **success means:** Long rides become routine enough that the final hour remains controlled.

##### Practice long-ride fueling

- **id:** `long-ride-fueling`
- **type:** `process`
- **supports:** `first-century-good`
- **success means:** Fueling is rehearsed often enough that the event plan is not theoretical.

### Example: Raise FTP

- **id:** `ftp-improvement`
- **type:** `capability`
- **priority:** `secondary`
- **target date / horizon:** `next build block`
- **why this matters:** Higher aerobic power expands what race pace feels sustainable.
- **success means:** A verified FTP improvement under an agreed protocol, not just a noisy modeled estimate.
- **coaching implication:** Build toward threshold development when it does not conflict with higher-priority event or durability goals.

## Measurement Plan

Use this section to describe how progress should be judged. Keep it as prescriptive or as open-ended as the coach thinks is useful.

The executable implementation belongs in `goal_measurement.py`.

For each important goal, record:
- what evidence directly tests the goal,
- what evidence only supports or weakens the case,
- whether rider input or coach judgment is required,
- what should count as achieved, on track, at risk, or insufficient evidence,
- what the dashboard should say when the data is incomplete or uncertain.

### `first-century-good`

- **assessment mode:** `hybrid`
- **direct evidence:** completed ride of at least 100 miles; rider reports finishing in control
- **supporting evidence:** longest recent ride, repeated long-ride exposure, late-ride fade, fueling adherence
- **manual evidence:** post-ride rider check-in about how the finish felt
- **dashboard expectation:** do not mark complete from distance alone; say when subjective confirmation is still missing

### `long-ride-durability`

- **assessment mode:** `hybrid`
- **direct evidence:** coach-selected durability tests or comparable long rides
- **supporting evidence:** long-ride frequency, longest recent ride, late-ride HR/power drift, back-to-back completion
- **dashboard expectation:** prefer `evidence strengthening`, `uncertain`, or `needs re-test` over fake exact percentages when the data is thin

### `ftp-improvement`

- **assessment mode:** `hybrid`
- **direct evidence:** agreed FTP test or coach-accepted threshold estimate
- **supporting evidence:** threshold workout completion, long-interval repeatability, modeled power trends
- **dashboard expectation:** distinguish measured improvement from inferred improvement

## Review Notes

Use this section for durable contract-level notes:
- known conflicts between goals,
- reasons one goal outranks another,
- review cadence,
- what would cause the contract to be revised.

Example:

- In the final taper before an A race, freshness outranks ride-frequency consistency.
- If the athlete gets sick, process goals yield to recovery until training is appropriate again.
