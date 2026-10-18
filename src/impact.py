"""Intention-to-treat impact for the replay and the earlier pilot."""

from __future__ import annotations

import math
from collections import defaultdict


def proportion_diff_ci(success_a: int, n_a: int, success_b: int, n_b: int, z: float = 1.96) -> dict:
    """Normal-approximation 95% CI for the difference in proportions (a - b)."""
    if n_a == 0 or n_b == 0:
        return {"low": None, "high": None, "method": "undefined"}
    p_a = success_a / n_a
    p_b = success_b / n_b
    se = math.sqrt(p_a * (1 - p_a) / n_a + p_b * (1 - p_b) / n_b)
    diff = p_a - p_b
    return {
        "diff": diff,
        "low": diff - z * se,
        "high": diff + z * se,
        "method": "normal approximation for difference in independent proportions",
    }


def replay_itt(arms: dict[str, str], outcomes: list[dict], decisions: list[dict]) -> dict:
    """ITT over every randomized user. Success = any SUCCEEDED outcome for that user."""
    succeeded = set()
    for outcome in outcomes:
        if outcome.get("status") == "SUCCEEDED":
            succeeded.add(outcome["viewer_id"])
    by_arm = defaultdict(lambda: {"users": 0, "successes": 0, "with_goal": 0, "with_action": 0})
    for user_id, arm in arms.items():
        by_arm[arm]["users"] += 1
        if user_id in succeeded:
            by_arm[arm]["successes"] += 1
    users_with_goal = {d["viewer_id"] for d in decisions if d.get("goal")}
    users_with_action = {d["viewer_id"] for d in decisions if d.get("action") not in (None, "NO_ACTION")}
    for user_id, arm in arms.items():
        if user_id in users_with_goal:
            by_arm[arm]["with_goal"] += 1
        if user_id in users_with_action:
            by_arm[arm]["with_action"] += 1
    agent = by_arm["agent"]
    holdout = by_arm["holdout"]
    agent_rate = agent["successes"] / agent["users"] if agent["users"] else 0.0
    holdout_rate = holdout["successes"] / holdout["users"] if holdout["users"] else 0.0
    ci = proportion_diff_ci(agent["successes"], agent["users"], holdout["successes"], holdout["users"])
    return {
        "analysis": "intention_to_treat",
        "denominator": "all users in replay/arms.csv",
        "agent": {
            "users": agent["users"],
            "successes": agent["successes"],
            "rate": agent_rate,
            "with_goal": agent["with_goal"],
            "with_action": agent["with_action"],
        },
        "holdout": {
            "users": holdout["users"],
            "successes": holdout["successes"],
            "rate": holdout_rate,
            "with_goal": holdout["with_goal"],
            "with_action": holdout["with_action"],
        },
        "absolute_uplift_pp": (agent_rate - holdout_rate) * 100,
        "ci95_diff": ci,
        "notes": [
            "Users remain in their assigned arm whether or not they triggered, were eligible, or were served.",
            "Success is any judged SUCCEEDED goal outcome for that user. Users with no goal count as non-success.",
        ],
    }


def pilot_itt(assignment: list[dict], outcomes: list[dict]) -> dict:
    """Recompute the pilot using randomized arms, not served-versus-others."""
    by_arm = defaultdict(lambda: {"users": 0, "successes": 0, "served": 0, "served_successes": 0})
    for row in outcomes:
        arm = row["arm"]
        by_arm[arm]["users"] += 1
        success = str(row.get("succeeded")).lower() == "true"
        served = str(row.get("served")).lower() == "true"
        if success:
            by_arm[arm]["successes"] += 1
        if served:
            by_arm[arm]["served"] += 1
            if success:
                by_arm[arm]["served_successes"] += 1
    treatment = by_arm["treatment"]
    holdout = by_arm["holdout"]
    t_rate = treatment["successes"] / treatment["users"] if treatment["users"] else 0.0
    h_rate = holdout["successes"] / holdout["users"] if holdout["users"] else 0.0
    served = treatment["served"]
    served_rate = treatment["served_successes"] / served if served else 0.0
    others = sum(arm["users"] for arm in by_arm.values()) - served
    others_success = (
        sum(arm["successes"] for arm in by_arm.values()) - treatment["served_successes"]
    )
    others_rate = others_success / others if others else 0.0
    ci = proportion_diff_ci(treatment["successes"], treatment["users"], holdout["successes"], holdout["users"])
    return {
        "analysis": "pilot_randomized_arm_itt",
        "treatment": {"users": treatment["users"], "successes": treatment["successes"], "rate": t_rate},
        "holdout": {"users": holdout["users"], "successes": holdout["successes"], "rate": h_rate},
        "absolute_uplift_pp": (t_rate - h_rate) * 100,
        "ci95_diff": ci,
        "biased_served_vs_others": {
            "served_users": served,
            "served_rate": served_rate,
            "all_other_users": others,
            "all_other_rate": others_rate,
            "absolute_uplift_pp": (served_rate - others_rate) * 100,
        },
        "selection_bias_explanation": (
            "The internal headline compared users the agent chose to serve with everyone else. "
            "Served users were selected by the agent and were already more likely to succeed. "
            "The randomized comparison keeps every treatment-arm user in the treatment denominator, "
            "including those never served, and compares that group with the holdout arm."
        ),
        "assignment_rows": len(assignment),
        "outcome_rows": len(outcomes),
    }


def render_impact_markdown(replay: dict, pilot: dict) -> str:
    lines = [
        "# Impact report",
        "",
        "## Replay intention-to-treat",
        "",
        f"- Denominator: {replay['denominator']}",
        f"- Agent: {replay['agent']['successes']}/{replay['agent']['users']} "
        f"({100 * replay['agent']['rate']:.2f}%)",
        f"- Holdout: {replay['holdout']['successes']}/{replay['holdout']['users']} "
        f"({100 * replay['holdout']['rate']:.2f}%)",
        f"- Absolute uplift: {replay['absolute_uplift_pp']:.2f} percentage points",
        (
            f"- 95% CI for (agent - holdout): "
            f"{replay['ci95_diff']['low'] * 100:.2f} to {replay['ci95_diff']['high'] * 100:.2f} pp"
            if replay["ci95_diff"]["low"] is not None
            else "- 95% CI: undefined"
        ),
        f"- Agent users with a goal: {replay['agent']['with_goal']}; with an action: {replay['agent']['with_action']}",
        f"- Holdout users with an action (must be 0): {replay['holdout']['with_action']}",
        "",
        "## Pilot randomized-arm re-analysis",
        "",
        f"- Treatment: {pilot['treatment']['successes']}/{pilot['treatment']['users']} "
        f"({100 * pilot['treatment']['rate']:.2f}%)",
        f"- Holdout: {pilot['holdout']['successes']}/{pilot['holdout']['users']} "
        f"({100 * pilot['holdout']['rate']:.2f}%)",
        f"- Absolute uplift: {pilot['absolute_uplift_pp']:.2f} percentage points",
        "",
        "### Why the internal headline differs",
        "",
        pilot["selection_bias_explanation"],
        "",
        (
            f"Biased served-vs-others contrast on the same rows: "
            f"{100 * pilot['biased_served_vs_others']['served_rate']:.1f}% vs "
            f"{100 * pilot['biased_served_vs_others']['all_other_rate']:.1f}% "
            f"(+{pilot['biased_served_vs_others']['absolute_uplift_pp']:.1f} pp)."
        ),
        "",
    ]
    return "\n".join(lines)
