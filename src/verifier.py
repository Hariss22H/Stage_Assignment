"""Independent safety gate.

Checks are recomputed from the trigger, viewer facts, catalogue, and config.
Planner flags and model claims are not evidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from src.config import VALID_DIALECTS
from src.data_loader import parse_campaign_deeplink, title_available
from src.facts import is_opted_in
from src.goals import evaluate_goal
from src.time_utils import in_quiet_hours, ist_date, parse_utc

PRICE_PATTERN = re.compile(r"(?:₹|rs\.?\s*|inr\s*)(\d+)", re.IGNORECASE)
INSTRUCTION_MARKERS = ("ignore your previous", "system note", "opted out")
TITLE_DEEPLINK = re.compile(r"^ott://title/([^/]+)$")


@dataclass
class Verification:
    passed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    # Unsupported model claims are rejected here but do not void an independently
    # verified deterministic proposal (spec: reject the claim, keep safe planning).
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed

    def to_dict(self) -> dict:
        return {
            "passed": list(self.passed),
            "failed": list(self.failed),
            "warnings": list(self.warnings),
        }


def _fail(result: Verification, name: str) -> None:
    result.failed.append(name)


def _ok(result: Verification, name: str) -> None:
    result.passed.append(name)


def verify_proposal(
    proposal: dict | None,
    *,
    trigger: dict,
    arm: str,
    snapshot,
    catalog,
    goal,
    catalogue: dict[str, dict],
    interventions: dict,
    allowed_prices: set[int],
    prior_actions: list[dict],
    published_at: datetime,
    stale: bool,
    model_calls: int = 0,
    tool_calls: int = 0,
    max_model_calls: int = 8,
    max_tool_calls: int = 20,
    model_goal_id: str | None = None,
) -> Verification:
    """Return passed and failed check names. `proposal` None means the planner abstained."""
    result = Verification()
    if arm == "holdout":
        if proposal is None:
            _ok(result, "holdout")
        else:
            _fail(result, "holdout")
        return result
    _ok(result, "holdout")

    if stale:
        if proposal is None:
            _ok(result, "freshness")
        else:
            _fail(result, "freshness")
        return result
    _ok(result, "freshness")

    if model_calls > max_model_calls or tool_calls > max_tool_calls:
        _fail(result, "budget")
    else:
        _ok(result, "budget")

    if model_goal_id and (goal is None or model_goal_id != goal.goal_id):
        result.warnings.append("model_goal_claim")
    elif model_goal_id:
        _ok(result, "model_goal_claim")

    if proposal is None:
        if goal is None:
            _ok(result, "goal")
        else:
            independent = evaluate_goal(catalog, goal.goal_id, snapshot)
            if independent.measurable and goal.success_event and goal.deadline:
                _ok(result, "goal")
            else:
                _fail(result, "goal")
        _ok(result, "abstention")
        return result

    kind = proposal.get("type")
    if kind not in {"home_page", "push", "whatsapp", "in_app_message"}:
        _fail(result, "action_type")
        return result
    _ok(result, "action_type")

    if goal is None or goal.goal_id not in catalog.goals or goal.goal_id in catalog.excluded_reasons:
        _fail(result, "goal")
    else:
        independent = evaluate_goal(catalog, goal.goal_id, snapshot)
        if goal.status != "ACTIVE" or not goal.success_event or not goal.deadline:
            _fail(result, "goal")
        elif not independent.measurable:
            _fail(result, "goal")
        else:
            _ok(result, "goal")

    dialect = trigger.get("app_dialect")
    if kind == "home_page":
        _verify_home_page(result, proposal, catalogue, dialect, published_at)
    elif kind == "push":
        _verify_push(result, proposal, snapshot, catalogue, interventions, allowed_prices, dialect, published_at, prior_actions)
    elif kind == "whatsapp":
        _verify_whatsapp(
            result, proposal, snapshot, catalogue, interventions, allowed_prices, dialect, trigger, published_at, prior_actions
        )
    else:
        _verify_in_app(result, proposal, catalogue, interventions, dialect, published_at, prior_actions)
    _verify_injection(result, proposal, catalogue)
    return result


def _verify_home_page(result, proposal, catalogue, dialect, published_at) -> None:
    if dialect not in VALID_DIALECTS or proposal.get("dialect") != dialect:
        _fail(result, "dialect")
    else:
        _ok(result, "dialect")
    rows = proposal.get("rows") or []
    allowed_types = {"next_episode", "because_you_watched", "popular_in_dialect", "new_releases", "genre"}
    shape_ok = len(rows) <= 6 and (bool(proposal.get("hero_title_id")) or bool(rows))
    catalogue_ok = True
    available_ok = True
    seen: set[str] = set()
    ids = []
    if proposal.get("hero_title_id"):
        ids.append(proposal["hero_title_id"])
    for row in rows:
        if row.get("row_type") not in allowed_types or not 3 <= len(row.get("title_ids") or []) <= 10:
            shape_ok = False
        ids.extend(row.get("title_ids") or [])
    for title_id in ids:
        title = catalogue.get(title_id)
        if title is None:
            catalogue_ok = False
            shape_ok = False
            continue
        if title.get("dialect") != dialect or not title_available(title, published_at):
            available_ok = False
            shape_ok = False
        slug = title.get("content_slug")
        if slug in seen:
            shape_ok = False
        if slug:
            seen.add(slug)
    (_ok if shape_ok else _fail)(result, "home_page_shape")
    (_ok if catalogue_ok else _fail)(result, "catalogue")
    (_ok if available_ok else _fail)(result, "availability")


def _send_at(proposal, published_at: datetime) -> datetime:
    return parse_utc(proposal.get("send_at")) or published_at


def _same_day_count(prior_actions, kinds: set[str], moment: datetime) -> int:
    day = ist_date(moment)
    count = 0
    for action in prior_actions:
        if action.get("type") not in kinds:
            continue
        stamp = parse_utc(action.get("send_at") or action.get("published_at"))
        if stamp is not None and ist_date(stamp) == day:
            count += 1
    return count


def _title_ok(result, title_id, catalogue, dialect, at) -> dict | None:
    title = catalogue.get(title_id)
    if title is None:
        _fail(result, "catalogue")
        return None
    if title.get("dialect") != dialect or dialect not in VALID_DIALECTS:
        _fail(result, "dialect")
    else:
        _ok(result, "dialect")
    if not title_available(title, at):
        _fail(result, "availability")
    else:
        _ok(result, "availability")
    _ok(result, "catalogue") if title is not None and "catalogue" not in result.failed else None
    return title


def _verify_push(result, proposal, snapshot, catalogue, interventions, allowed_prices, dialect, published_at, prior_actions) -> None:
    if not is_opted_in(snapshot.facts.facts["notifications_opt_in"]):
        _fail(result, "consent")
    else:
        _ok(result, "consent")
    send_at = _send_at(proposal, published_at)
    if in_quiet_hours(send_at):
        _fail(result, "quiet_hours")
    else:
        _ok(result, "quiet_hours")
    if _same_day_count(prior_actions, {"push", "whatsapp"}, send_at) >= 1:
        _fail(result, "frequency_cap")
    else:
        _ok(result, "frequency_cap")
    title = _title_ok(result, proposal.get("title_id"), catalogue, dialect, send_at)
    text = proposal.get("text") or {}
    heading = str(text.get("title") or "")
    body = str(text.get("body") or "")
    limits = interventions["push"]["copy"]
    forbidden = [phrase.lower() for phrase in limits["forbidden_phrases"]]
    combined = f"{heading} {body}".lower()
    deeplink = proposal.get("deeplink") or ""
    match = TITLE_DEEPLINK.match(deeplink)
    copy_ok = (
        0 < len(heading) <= int(limits["title_max_chars"])
        and 0 < len(body) <= int(limits["body_max_chars"])
        and not any(phrase in combined for phrase in forbidden)
        and match is not None
        and match.group(1) == proposal.get("title_id")
        and _prices_allowed(combined, allowed_prices)
    )
    description = str((title or {}).get("description") or "")
    copied_description = bool(description) and description in body
    if copy_ok and title is not None and not copied_description:
        _ok(result, "copy")
    else:
        _fail(result, "copy")


def _verify_whatsapp(result, proposal, snapshot, catalogue, interventions, allowed_prices, dialect, trigger, published_at, prior_actions) -> None:
    if not is_opted_in(snapshot.facts.facts["whatsapp_opt_in"]):
        _fail(result, "consent")
    else:
        _ok(result, "consent")
    send_at = _send_at(proposal, published_at)
    if in_quiet_hours(send_at):
        _fail(result, "quiet_hours")
    else:
        _ok(result, "quiet_hours")
    if _same_day_count(prior_actions, {"push", "whatsapp"}, send_at) >= 1:
        _fail(result, "frequency_cap")
    else:
        _ok(result, "frequency_cap")
    _title_ok(result, proposal.get("title_id"), catalogue, dialect, send_at)
    templates = {item["template_id"]: item for item in interventions["whatsapp"]["approved_templates"]}
    template = templates.get(proposal.get("template_id"))
    variables = proposal.get("variables") or {}
    language_ok = trigger.get("app_language") == "hin"
    template_ok = (
        template is not None
        and language_ok
        and template.get("language") == "hin"
        and set(variables) == set(template.get("variables") or [])
        and all(str(value).strip() for value in variables.values())
        and _prices_allowed(" ".join(str(value) for value in variables.values()), allowed_prices)
    )
    if template_ok:
        _ok(result, "template")
    else:
        _fail(result, "template")


def _verify_in_app(result, proposal, catalogue, interventions, dialect, published_at, prior_actions) -> None:
    send_at = _send_at(proposal, published_at)
    cap = int(interventions["in_app_message"]["max_per_day"])
    if _same_day_count(prior_actions, {"in_app_message"}, send_at) >= cap:
        _fail(result, "frequency_cap")
    else:
        _ok(result, "frequency_cap")
    _title_ok(result, proposal.get("title_id"), catalogue, dialect, send_at)
    _ok(result, "consent")


def _verify_injection(result, proposal, catalogue) -> None:
    text = proposal.get("text") or {}
    variables = proposal.get("variables") or {}
    parts = []
    if isinstance(text, dict):
        parts.extend(str(value) for value in text.values())
    if isinstance(variables, dict):
        parts.extend(str(value) for value in variables.values())
    parts.append(str(proposal.get("deeplink") or ""))
    action_text = " ".join(parts).lower()
    if any(marker in action_text for marker in INSTRUCTION_MARKERS):
        _fail(result, "prompt_injection")
        return
    for title_id in [proposal.get("title_id"), proposal.get("hero_title_id")]:
        title = catalogue.get(title_id) if title_id else None
        description = str((title or {}).get("description") or "").strip()
        if description and description.lower() in action_text:
            _fail(result, "prompt_injection")
            return
    deeplink = proposal.get("deeplink") or ""
    if deeplink and parse_campaign_deeplink(deeplink):
        _fail(result, "prompt_injection")
        return
    _ok(result, "prompt_injection")


def _prices_allowed(text: str, allowed_prices: set[int]) -> bool:
    for match in PRICE_PATTERN.findall(text):
        if int(match) not in allowed_prices:
            return False
    return True
