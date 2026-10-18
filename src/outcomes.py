"""Judge goal outcomes from simulator events and active-goal records."""

from __future__ import annotations

import json
from pathlib import Path

from src.data_loader import iter_jsonl
from src.goals import ActiveGoal, GoalCatalog, Snapshot, success_evidence
from src.time_utils import format_utc, parse_utc


def judge_outcomes(
    *,
    decisions: list[dict],
    outcome_events_path: Path,
    catalogue: dict[str, dict],
    episodes: dict[str, list[str]],
    catalog: GoalCatalog,
    judged_at: str,
) -> list[dict]:
    events_by_user: dict[str, list[dict]] = {}
    for _line, event in iter_jsonl(outcome_events_path):
        events_by_user.setdefault(event["user_id"], []).append(event)

    outcomes = []
    seen = set()
    for decision in decisions:
        goal = decision.get("goal")
        if not goal or goal.get("status") != "ACTIVE":
            continue
        key = (decision["viewer_id"], goal["goal_id"], goal["started_at"])
        if key in seen:
            continue
        seen.add(key)
        viewer_id = decision["viewer_id"]
        active = ActiveGoal(**{**goal, "status": "ACTIVE"})
        # Use the corrected catalog success rule when the goal carries a later version.
        if goal.get("rule_version") != catalog.rule_version(goal["goal_id"]):
            # Prefer the rule version recorded on the goal itself.
            active.success_event = goal.get("success_event")
            active.success_predicate = goal.get("success_predicate") or ""
        snapshot = Snapshot(
            viewer_id=viewer_id,
            as_of=judged_at,
            events=events_by_user.get(viewer_id, []),
            profile=None,
            catalogue=catalogue,
            episodes=episodes,
        )
        evidence = success_evidence(active, snapshot)
        deadline = parse_utc(goal["deadline"])
        as_of_dt = parse_utc(judged_at)
        if evidence:
            status = "SUCCEEDED"
        elif deadline is not None and as_of_dt is not None and as_of_dt > deadline:
            status = "FAILED"
        else:
            status = "INCONCLUSIVE"
        outcomes.append(
            {
                "outcome_id": f"O-{len(outcomes)+1:06d}",
                "viewer_id": viewer_id,
                "goal_id": goal["goal_id"],
                "decision_id": decision.get("decision_id"),
                "status": status,
                "judged_at": judged_at,
                "started_at": goal["started_at"],
                "deadline": goal["deadline"],
                "rule_version": goal.get("rule_version"),
                "success_event": goal.get("success_event"),
                "success_predicate": goal.get("success_predicate"),
                "evidence": evidence,
                "arm": decision.get("arm"),
            }
        )
    return outcomes


def write_outcomes(path: Path, outcomes: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in outcomes:
            handle.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
