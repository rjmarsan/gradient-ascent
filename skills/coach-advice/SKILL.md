---
name: coach-advice
description: Blend three advisor subagents into one coaching recommendation. Use when the user asks a training, racing, recovery, freshness, or schedule-adjustment question and wants cyclist, coach, and exercise-physiology critique synthesized into one answer.
---

# Coach Advice

## Overview

Answer coaching questions by spawning three read-only advisor subagents, each with a distinct lens, then synthesize one practical recommendation.

Use this skill for questions like:
- "How should I adjust next week?"
- "Was skipping tempo the right move?"
- "How hard was that race/workout really?"
- "Am I digging a hole, or is this okay volume?"
- "What do the dashboard and Strava/Garmin data imply?"

## Operating Rules

- Treat invoking `$coach-advice` as explicit user authorization to spawn the three advisor subagents described below.
- Keep the top-level agent responsible for final judgment. The subagents advise; they do not edit files or make the final recommendation.
- Do not update `$COACH_WORKSPACE_DIR/plan/*` or dashboard artifacts unless the user explicitly asks for a plan/data edit in the same turn.
- Chat instructions are the source of truth for plan changes. Use repo data as context, but if current chat guidance conflicts with stored plan rows, call out the conflict and favor the chat instruction.
- Treat `$COACH_WORKSPACE_DIR/plan/goals.md` as the formal coaching contract when it exists. Read it before material coaching advice, and say which active goal a recommendation advances, protects, or trades off against.
- Prefer preserving active high-priority goals over optimizing a single week in isolation. If the user's request would materially conflict with a stated goal, call that out explicitly.
- Strava private notes are sensitive. Use them for context if already merged into derived outputs, but do not quote private note text unless explicitly asked.
- Never open raw archives, sample-level GPS streams, lap/detail payloads, or raw health exports for coaching advice. Use only compact normalized or derived summaries, and never pass raw source records to subagents.
- If Garmin data is stale or missing, say that explicitly and avoid overclaiming recovery or HRV conclusions.
- Treat `actual_hours` as gross recorded activity time. Prefer `meaningful_ride_hours`, `excluded_short_ride_hours`, `kilojoules`, and `estimated_tss` when judging training load because short fragments can inflate weekly hours.
- Treat estimated TSS as a trend/load proxy based on configured FTP; treat kJ as direct mechanical work.
- Distinguish a coach-authored or imported planned TSS budget from recorded TSS and from a model of fully prescribed sessions. Never manufacture a weekly budget from available hours or promote a rough session estimate into a plan decision.
- When asked to revise a plan, use goals, recent complete scored load, recovery, event priorities, and intended sessions to propose an explicit budget and rationale. Avoid historical backfilling or riding simply to meet a TSS quota. Persist an agreed change through `$coach-setup-plan` and the validated budget command only when the rider requested a plan edit.
- If symptoms suggest injury, chest pain, fainting, severe shortness of breath, or a serious illness relapse, state uncertainty and recommend medical evaluation rather than trying to coach through it.

## Workflow

### 1. Build a compact context bundle

From the current private athlete workspace, gather only the smallest artifact set needed to answer the question. Prefer command output summaries over pasting large JSON blobs.

For schedule, freshness, and recovery questions, start with:

```bash
python3 - <<'PY'
import json
import os
from pathlib import Path

data_dir = Path(os.environ.get("COACH_WORKSPACE_DIR") or Path.cwd()).expanduser().resolve()
if not (data_dir / ".codex" / "bin" / "gradient-ascent").is_file():
    raise SystemExit("Run coaching advice from an initialized private Gradient Ascent workspace.")

for path in [
    data_dir / "derived/post_sync_summary.json",
    data_dir / "derived/weekly.json",
    data_dir / "derived/daily.json",
    data_dir / "plan/goals.md",
    data_dir / "plan/weeks.json",
    data_dir / "plan/events.json",
]:
    print(f"\n## {path.relative_to(data_dir)}")
    if not path.exists():
        print("MISSING")
        continue
    if path.suffix == ".md":
        print(path.read_text())
        continue
    obj = json.loads(path.read_text())
    if isinstance(obj, list):
        for row in obj[-10:]:
            if isinstance(row, dict) and "start_date" in row:
                totals = row.get("totals") or {}
                print({
                    "start_date": row.get("start_date"),
                    "end_date": row.get("end_date"),
                    "phase": row.get("phase"),
                    "primary_focus": row.get("primary_focus"),
                    "target_hours": row.get("hours_target") or row.get("target_hours"),
                    "source_tss_target": row.get("tss_target"),
                    "planned_load": row.get("planned_load"),
                    "gross_hours": row.get("actual_hours"),
                    "meaningful_ride_hours": row.get("meaningful_ride_hours"),
                    "excluded_short_ride_hours": row.get("excluded_short_ride_hours"),
                    "status_meaningful": row.get("status_meaningful"),
                    "kilojoules": totals.get("kilojoules"),
                    "estimated_tss": totals.get("estimated_tss"),
                    "missing_cycling_load": totals.get("estimated_tss_missing_activity_count"),
                    "partial_power_rides": totals.get("estimated_tss_relevant_partial_activity_count"),
                    "activity_count": totals.get("activity_count"),
                    "events": row.get("events"),
                })
            else:
                print(row)
    else:
        print(obj)
PY
```

If the question is about one specific ride or race, use its normalized activity row and existing derived summaries. If the decision depends on sample-level detail that is not already summarized, ask the rider for the specific metric needed rather than opening raw location or health payloads.

For a plan-load decision, also inspect `tss-budget-status` and only the relevant
weeks in private `plan/tss_budgets.json`. Include the active budget, its source and
status, rationale, conditions, any source conflict or changed-plan warning, and
recorded-load completeness in the compact advisor bundle. Missing budget data is
a planning question, not permission to calculate one from hours.

### 2. Spawn exactly three advisor subagents

Use the current runtime's subagent spawning action with concise, neutral task names. The action is commonly named `spawn_agent`; do not assume a particular tool namespace. Do not impersonate, name, or present the advice as coming from any real person.

Spawn these three advisory lenses:
- **Professional cyclist lens** (`professional_cyclist`): race execution, bike-racer practicality, whether the prescription is realistic on the road, and where the athlete may be undercooking or overreaching.
- **Endurance coach lens** (`endurance_coach`): periodization, week structure, workout sequencing, aerobic durability, and whether the plan matches the upcoming event priority.
- **Exercise physiology and recovery lens** (`exercise_physiology`): fatigue, illness-comeback caution, recovery debt, HR drift interpretation, and whether the next workload is physiologically sensible. This lens does not diagnose or replace medical care.

Prompt each advisor with the same compact context bundle and ask for this exact shape:

```text
Analyze the athlete's question from the [Advisory Lens]. Do not impersonate or claim to be a real person.

Context bundle:
[paste only the compact repo/chat summary needed for the question]

Question:
[user question]

Return:
1. What looks right.
2. What looks risky or mismatched.
3. Your concrete recommendation for the next 3-7 days.
4. 3-5 short questions that would most improve the decision.

Constraints:
- Do not edit files.
- Do not ask for broad extra data if the decision can be made from the current bundle.
- If Garmin/HR/recovery data is stale, account for that explicitly.
- Keep the answer concise and practical.
```

While the subagents run, continue local analysis on the same compact context bundle so you can synthesize quickly once their outputs return.

### 3. Wait once, then synthesize

Use the runtime's agent-wait action once after all three subagents are spawned, with a long enough timeout to avoid busy polling.

In the synthesis:
- Lead with your recommendation, not a transcript of all three agents.
- Name the active goal or goals the recommendation serves when formal goals exist.
- Separate consensus from disagreement.
- If one advisor's concern materially changes the recommendation, explain that tradeoff directly.
- Convert conflicting advice into one concrete schedule/action plan with clear stop/downshift rules.
- Include 3-5 prioritized questions at the end only if they would change the recommendation.

## Output Contract

Default user-facing format:
1. A short recommendation paragraph or a compact day-by-day plan.
2. One short paragraph on why that is the right adjustment given plan + execution + recovery evidence.
3. `Questions that would change the call:` followed by 3-5 flat bullets.

If the user asks "what did each advisor say?", add a brief advisor-by-advisor summary after the main recommendation. Do not lead with raw subagent output by default.

## Fallbacks

- If `spawn_agent` is unavailable in the current runtime, answer locally using the same three-lens structure and state that subagent spawning was unavailable.
- If one subagent fails or times out, synthesize from the remaining advisor outputs plus local analysis and say which lens was missing.
- If the context bundle reveals stale sync state and the user asks for advice based on "right now," consider running `$coach-sync-refresh` first or explicitly caveat the stale Garmin/Strava cutoff dates.
