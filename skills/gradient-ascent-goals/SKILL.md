---
name: gradient-ascent-goals
description: Define, revise, and operationalize a cyclist's formal coaching goals in plan/goals.md. Use when the user wants to set goals, make goals more realistic, resolve overlap or conflict between goals, turn vague ambitions into a coaching contract, or decide how progress should be judged in the dashboard.
---

# Gradient Ascent Goals

Use this skill to turn a pile of ambitions into a usable coaching contract.

## Core Contract

- Treat `$COACH_WORKSPACE_DIR/plan/goals.md` as canonical when it exists.
- Keep the Markdown hierarchy meaningful:
  - top-level heading: the north star for the current season or training arc,
  - `## Main Goals`: the few goals that should drive tradeoffs,
  - nested supporting structures: capabilities or habits that make the main goals more likely.
- Keep the number of active main goals small enough that they can actually break ties.
- Prefer a goal that changes coaching decisions over a vanity metric that never changes behavior.
- Do not force every goal into fake precision. Mechanical, hybrid, and judgment-based goals are all valid.

## Workflow

When a CLI command is needed, resolve the workspace-local launcher first:

```bash
WORKSPACE_DIR="${COACH_WORKSPACE_DIR:-${COACH_DATA_DIR:-$PWD}}"
COACH_CLI="$WORKSPACE_DIR/.codex/bin/gradient-ascent"
test -x "$COACH_CLI" || COACH_CLI="$(command -v gradient-ascent)"
```

1. Read `plan/goals.md`, `plan/goals_template.md`, `plan/goal_measurement.py`, and the current plan context before proposing changes. If the goal files are missing in an existing workspace, rerun `"$COACH_CLI" init-workspace .` to backfill the starter files without replacing existing plan data.
2. Ask what the athlete is really trying to make true, then separate:
   - outcomes: race result, finish, qualification, podium,
   - capabilities: FTP, repeatability, durability, descending, pack handling,
   - processes: ride frequency, fueling consistency, sleep, strength work.
3. Collapse duplicates and expose overlap:
   - if two goals succeed or fail on the same evidence, merge them or make one supporting structure,
   - if two goals compete for time, freshness, or adaptation, say which one wins and why.
4. Stress-test realism against the athlete's horizon, history, constraints, and calendar.
5. Draft the revised goals in a temporary UTF-8 Markdown file outside the workspace. Do not edit `plan/goals.md` directly; the locked install command below prevents a refresh or import from publishing a mixed snapshot. Each active main goal should state:
   - why it matters,
   - what success means,
   - what coaching decisions it should influence.
6. Fill the Measurement Plan section with the evidence that should matter for each goal:
   - direct evidence,
   - supporting evidence,
   - manual or coach-judgment evidence,
   - what incomplete evidence should mean.
7. When the user wants dashboard behavior, draft a temporary Python file for the Progress tab. The coach owns both the evaluation logic and the presentation.
8. Install the drafts through the workspace-local command. It validates the Python draft, updates the canonical files under the workspace lock, and rebuilds the Training Center:

```bash
"$COACH_CLI" update-goal-files \
  --goals-file /path/to/goals-draft.md \
  --measurement-file /path/to/goal-measurement-draft.py
```

Omit `--measurement-file` when only the coaching contract changed. Never copy either draft directly into `plan/`.

## Judgment Rules

- A-race goals usually outrank generic capability chasing near the target event.
- Capability goals are often better supporting structures unless they are the actual point of the season.
- Process goals are useful when they are leading indicators or guardrails, not when they become chores detached from the north star.
- If the athlete says "feel good," define what that means behaviorally before pretending it is measurable.
- If a proposed goal cannot be evaluated at all, keep it as a value or preference, not a tracked goal.
- If a measurement would create perverse behavior, do not use it just because it graphs cleanly.
- TSS is a training-load measure, not a goal by itself. Translate the goal hierarchy,
  event priorities, recent scored training, recovery, and constraints into explicit
  weekly budget decisions only when the rider requests a plan change. Keep those
  decisions in the separate validated `plan/tss_budgets.json` workflow described by
  `$coach-setup-plan`; do not hide an invented hours-based budget in goal-measurement
  code or turn a load target into a quota. Preserve missing or review-needed budgets
  as unresolved coaching decisions.

## Useful Examples

- `Finish my first century and feel good` can be a main outcome goal, supported by long-ride durability, fueling practice, and pacing discipline.
- `Ride 5 days a week` is usually a process goal unless consistency itself is the season's main problem.
- `Raise FTP` may be a supporting capability under a race goal, or a main goal in an off-season development block.
- `A / B / C races` belong in the contract only when the tiering changes planning decisions.

## Output

Leave the user with:
- the revised coaching contract in `plan/goals.md`,
- an explicit statement of any merged, demoted, or rejected goals,
- the tradeoffs that should govern future coaching decisions,
- and, when requested, a coach-authored Progress tab implementation in `plan/goal_measurement.py`.
