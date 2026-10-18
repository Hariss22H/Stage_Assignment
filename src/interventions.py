"""Deterministic intervention planner.

Home page is preferred for a fresh in-app trigger. Push, WhatsApp, and in-app
messages are fallbacks. The planner never reads catalogue descriptions, search
queries, or deeplink text as instructions. The verifier accepts or rejects the result.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from src.config import VALID_DIALECTS
from src.data_loader import title_available
from src.facts import is_opted_in
from src.time_utils import earliest_allowed_send, in_quiet_hours, ist_date, parse_utc

DECISION_NAMES = {
    "home_page": "HOME_PAGE",
    "push": "PUSH",
    "whatsapp": "WHATSAPP",
    "in_app_message": "IN_APP_MESSAGE",
    "NO_ACTION": "NO_ACTION",
}


@dataclass
class Plan:
    action: str
    reason: str
    no_action_reason: str | None = None
    payload: dict | None = None

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "reason": self.reason,
            "no_action_reason": self.no_action_reason,
            "payload": self.payload,
        }


def plan_action(
    *,
    trigger: dict,
    snapshot,
    goal,
    arm: str,
    catalogue: dict[str, dict],
    interventions: dict,
    allowed_prices: dict[str, int],
    prior_actions: list[dict],
    at: datetime,
    stale: bool,
) -> Plan:
    if arm == "holdout":
        return Plan("NO_ACTION", "Viewer is in the holdout arm.", "holdout arm receives no action")
    if stale:
        return Plan("NO_ACTION", "Trigger is offline-buffered or device-skewed.", "stale trigger")
    if goal is None or goal.status != "ACTIVE":
        return Plan("NO_ACTION", "No measurable eligible goal is active.", "no measurable eligible goal")
    dialect = trigger.get("app_dialect")
    page = None
    if dialect in VALID_DIALECTS:
        page = _home_page(dialect, catalogue, goal, at)
    if page is not None:
        return Plan("HOME_PAGE", "A fresh visit can show a dialect-matching home page.", payload=page)
    message = _message(
        trigger=trigger,
        snapshot=snapshot,
        goal=goal,
        catalogue=catalogue,
        interventions=interventions,
        allowed_prices=allowed_prices,
        prior_actions=prior_actions,
        at=at,
        dialect=dialect,
    )
    if message is not None:
        return message
    if dialect not in VALID_DIALECTS:
        return Plan("NO_ACTION", "The app dialect has no catalogue page.", "app dialect is not a catalogue dialect")
    return Plan("NO_ACTION", "No safe proportionate action is available.", "no safe action")


def _home_page(dialect: str, catalogue: dict[str, dict], goal, at: datetime) -> dict | None:
    pool = [
        row
        for _title_id, row in sorted(catalogue.items())
        if row.get("dialect") == dialect and title_available(row, at)
    ]
    if not pool:
        return None
    hero = _hero(pool, goal)
    used = set()
    hero_id = None
    if hero is not None:
        hero_id = hero["title_id"]
        used.add(hero.get("content_slug"))
    rows = []
    popular = _take(pool, used, 6, lambda _row: True)
    if popular:
        rows.append({"row_type": "popular_in_dialect", "title_ids": popular})
    releases = _take(
        pool,
        used,
        6,
        lambda row: _recent(row, at),
    )
    if releases:
        rows.append({"row_type": "new_releases", "title_ids": releases})
    if hero_id is None and not rows:
        return None
    return {"type": "home_page", "dialect": dialect, "hero_title_id": hero_id, "rows": rows}


def _hero(pool: list[dict], goal):
    slug = None if goal is None else (goal.success_params or {}).get("content_slug")
    if slug:
        for row in pool:
            if row.get("content_slug") == slug:
                return row
    ranked = sorted(pool, key=lambda row: (row.get("release_date") or "", row.get("title_id") or ""), reverse=True)
    return ranked[0] if ranked else None


def _take(pool, used: set, limit: int, predicate) -> list[str]:
    chosen = []
    slugs = []
    for row in pool:
        slug = row.get("content_slug")
        if not slug or slug in used or slug in slugs or not predicate(row):
            continue
        chosen.append(row["title_id"])
        slugs.append(slug)
        if len(chosen) == limit:
            break
    if len(chosen) < 3:
        return []
    used.update(slugs)
    return chosen


def _recent(row: dict, at: datetime) -> bool:
    raw = (row.get("release_date") or "")[:10]
    if len(raw) != 10:
        return False
    released = datetime.strptime(raw, "%Y-%m-%d").date()
    return at.date() - timedelta(days=30) <= released <= at.date()


def _message(*, trigger, snapshot, goal, catalogue, interventions, allowed_prices, prior_actions, at, dialect) -> Plan | None:
    if dialect not in VALID_DIALECTS:
        return None
    title = _hero(
        [row for row in catalogue.values() if row.get("dialect") == dialect and title_available(row, at)],
        goal,
    )
    if title is None:
        return None
    push_ok = is_opted_in(snapshot.facts.facts["notifications_opt_in"])
    whatsapp_ok = is_opted_in(snapshot.facts.facts["whatsapp_opt_in"]) and trigger.get("app_language") == "hin"
    if push_ok and _outbound_count(prior_actions, at) == 0:
        send_at = at if not in_quiet_hours(at) else earliest_allowed_send(at)
        if _within_deadline(goal, send_at) and title_available(title, send_at):
            return Plan("PUSH", "Push is the smallest consented channel that can name one title.", payload=_push(title, send_at))
    if whatsapp_ok and _outbound_count(prior_actions, at if not in_quiet_hours(at) else earliest_allowed_send(at)) == 0:
        send_at = at if not in_quiet_hours(at) else earliest_allowed_send(at)
        if _within_deadline(goal, send_at) and title_available(title, send_at):
            payload = _whatsapp(title, dialect, goal, interventions, allowed_prices, send_at)
            if payload is not None:
                return Plan("WHATSAPP", "WhatsApp uses an approved Hindi template.", payload=payload)
    if _in_app_count(prior_actions, at) < int(interventions["in_app_message"]["max_per_day"]):
        return Plan(
            "IN_APP_MESSAGE",
            "In-app message does not require messaging consent.",
            payload={
                "type": "in_app_message",
                "title_id": title["title_id"],
                "send_at": _iso(at),
                "text": _copy(title["title"]),
            },
        )
    return None


def _within_deadline(goal, send_at: datetime) -> bool:
    deadline = parse_utc(goal.deadline)
    return deadline is not None and send_at <= deadline


def _outbound_count(prior_actions, moment: datetime) -> int:
    day = ist_date(moment)
    count = 0
    for action in prior_actions:
        if action.get("type") not in {"push", "whatsapp"}:
            continue
        stamp = parse_utc(action.get("send_at") or action.get("published_at"))
        if stamp is not None and ist_date(stamp) == day:
            count += 1
    return count


def _in_app_count(prior_actions, moment: datetime) -> int:
    day = ist_date(moment)
    count = 0
    for action in prior_actions:
        if action.get("type") != "in_app_message":
            continue
        stamp = parse_utc(action.get("send_at") or action.get("published_at"))
        if stamp is not None and ist_date(stamp) == day:
            count += 1
    return count


def _copy(title_name: str) -> dict:
    name = " ".join(str(title_name).split())
    heading = name[:40].strip() or "A title for you"
    body = f"Watch {name}."
    if len(body) > 120:
        body = body[:117].rstrip() + "."
    return {"title": heading, "body": body}


def _iso(moment: datetime) -> str:
    from src.time_utils import format_utc

    return format_utc(moment)


def _push(title: dict, send_at: datetime) -> dict:
    return {
        "type": "push",
        "send_at": _iso(send_at),
        "title_id": title["title_id"],
        "text": _copy(title.get("title") or "A title for you"),
        "deeplink": f"ott://title/{title['title_id']}",
    }


def _whatsapp(title, dialect, goal, interventions, allowed_prices, send_at) -> dict | None:
    templates = {item["template_id"]: item for item in interventions["whatsapp"]["approved_templates"]}
    template_id = "wa_recommend_title_v1"
    template = templates.get(template_id)
    if template is None:
        return None
    variables = {"title_name": str(title.get("title") or "").strip(), "dialect_name": dialect}
    if goal is not None and goal.goal_id == "G04" and "wa_trial_ending_v1" in templates:
        price = allowed_prices.get("P_MONTH")
        if price is not None:
            template_id = "wa_trial_ending_v1"
            variables = {"plan_price": f"₹{price}"}
    if not all(variables.values()):
        return None
    return {
        "type": "whatsapp",
        "send_at": _iso(send_at),
        "template_id": template_id,
        "variables": variables,
        "title_id": title["title_id"],
    }
