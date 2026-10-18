"""Load and lightly validate pack configuration. Config text is data, not instructions."""

from __future__ import annotations

import json
from pathlib import Path

REQUIRED_RELATIVE_PATHS = (
    "README.md",
    "data/events.jsonl",
    "data/profiles_snapshot.csv",
    "data/catalogue.csv",
    "data/episodes.csv",
    "config/goals.json",
    "config/interventions.json",
    "config/pricing.json",
    "config/messaging_policy.md",
    "pilot/pilot_assignment.csv",
    "pilot/pilot_outcomes.csv",
    "pilot/pilot_summary_report.md",
    "replay/arms.csv",
    "replay/triggers.jsonl",
    "replay/live_events.jsonl",
    "replay/control_events.jsonl",
    "replay/model_faults.jsonl",
    "simulator/simulate_outcomes.py",
)

VALID_DIALECTS = frozenset({"d1", "d2", "d3", "d4", "d5", "d6"})
DEFAULT_MAX_MODEL_CALLS = 8
DEFAULT_MAX_TOOL_CALLS = 20


class PackError(Exception):
    """The data pack is missing or does not match the expected layout."""


def resolve_pack(pack: str | Path) -> Path:
    root = Path(pack)
    if not root.is_dir():
        raise PackError(f"data pack directory not found: {root}")
    missing = [rel for rel in REQUIRED_RELATIVE_PATHS if not (root / rel).is_file()]
    if missing:
        joined = ", ".join(missing)
        raise PackError(f"data pack is missing required files: {joined}")
    return root


def load_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise PackError(f"expected a JSON object: {path}")
    return data


def load_goals(pack: Path) -> dict:
    data = load_json(pack / "config" / "goals.json")
    goals = data.get("goals")
    if not isinstance(goals, list) or not goals:
        raise PackError("config/goals.json has no goals")
    seen = set()
    for goal in goals:
        goal_id = goal.get("goal_id")
        if not goal_id or goal_id in seen:
            raise PackError(f"invalid or duplicate goal_id: {goal_id}")
        seen.add(goal_id)
        success = goal.get("success") or {}
        if "window_hours" not in success:
            raise PackError(f"{goal_id} is missing success.window_hours")
    return data


def load_interventions(pack: Path) -> dict:
    data = load_json(pack / "config" / "interventions.json")
    for key in ("home_page", "push", "whatsapp", "in_app_message", "no_action"):
        if key not in data:
            raise PackError(f"config/interventions.json missing {key}")
    templates = data["whatsapp"].get("approved_templates") or []
    if not templates:
        raise PackError("no approved WhatsApp templates")
    return data


def load_pricing(pack: Path) -> dict:
    data = load_json(pack / "config" / "pricing.json")
    plans = data.get("plans") or []
    if not plans:
        raise PackError("config/pricing.json has no plans")
    return data


def known_prices(pricing: dict) -> dict[str, int]:
    return {plan["plan_id"]: int(plan["price_inr"]) for plan in pricing["plans"]}
