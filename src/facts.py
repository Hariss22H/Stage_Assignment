"""Fact provenance, source precedence, and conflict records.

The deterministic core owns these facts. A model is not allowed to create or replace them.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from src.config import VALID_DIALECTS
from src.data_loader import parse_campaign_deeplink
from src.time_utils import freshness_seconds, parse_utc

SUBSCRIPTION_EVENTS = frozenset({"purchase_completed", "subscription_renewed", "subscription_lapsed"})
CONSENT_FIELDS = {
    "push": "notifications_opt_in",
    "whatsapp": "whatsapp_opt_in",
}


@dataclass
class Fact:
    name: str
    value: object
    source: str
    source_id: str
    observed_at: str | None
    received_at: str | None
    freshness_seconds: float | None
    confidence: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Conflict:
    fact: str
    candidates: list[dict]
    chosen_value: object
    precedence: str
    resolution_reason: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ViewerFacts:
    viewer_id: str
    as_of: str
    facts: dict[str, Fact] = field(default_factory=dict)
    conflicts: list[Conflict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "viewer_id": self.viewer_id,
            "as_of": self.as_of,
            "facts": {name: fact.to_dict() for name, fact in self.facts.items()},
            "conflicts": [conflict.to_dict() for conflict in self.conflicts],
        }


def is_opted_in(fact: Fact) -> bool:
    """Unknown and false are both not consent."""
    return fact.value is True


def _as_of_dt(as_of: str):
    parsed = parse_utc(as_of)
    if parsed is None:
        raise ValueError("as_of timestamp is required")
    return parsed


def _received(event: dict):
    return parse_utc(event.get("received_at"))


def _events_until(events: list[dict], as_of: str) -> list[dict]:
    limit = _as_of_dt(as_of)
    visible = []
    for event in events:
        received = _received(event)
        if received is not None and received <= limit:
            visible.append(event)
    return sorted(visible, key=lambda event: event.get("received_at") or "")


def _bool_field(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text == "true":
        return True
    if text == "false":
        return False
    return None


def _is_trial(value: object) -> bool:
    return _bool_field(value) is True


def _profile_time(profile: dict | None) -> str | None:
    if not profile:
        return None
    return profile.get("updated_at") or profile.get("snapshot_at") or None


def _status_from_event(event: dict) -> str | None:
    name = event.get("event")
    props = event.get("properties") or {}
    if name == "purchase_completed":
        return "trial_active" if _is_trial(props.get("is_trial")) else "active"
    if name == "subscription_renewed":
        return "active"
    if name == "subscription_lapsed":
        return "lapsed"
    return None


def _fact(
    name: str,
    value: object,
    source: str,
    source_id: str,
    observed_at: str | None,
    received_at: str | None,
    as_of: str,
    confidence: float,
) -> Fact:
    freshness = None
    stamp = parse_utc(received_at) or parse_utc(observed_at)
    as_of_dt = parse_utc(as_of)
    if stamp is not None and as_of_dt is not None:
        freshness = round(freshness_seconds(stamp, as_of_dt), 3)
    return Fact(name, value, source, source_id, observed_at, received_at, freshness, confidence)


def resolve_subscription(events: list[dict], profile: dict | None, as_of: str) -> tuple[Fact, list[Conflict], bool]:
    """Latest trustworthy subscription observation wins. Returns (fact, conflicts, ever_paid)."""
    visible = _events_until(events, as_of)
    subscription_events = [event for event in visible if event.get("event") in SUBSCRIPTION_EVENTS]
    latest_event = subscription_events[-1] if subscription_events else None
    event_status = _status_from_event(latest_event) if latest_event else None
    event_time = latest_event.get("received_at") if latest_event else None
    profile_status = (profile or {}).get("subscription_status") or None
    profile_time = _profile_time(profile)
    ever_paid = any(
        (
            event.get("event") == "purchase_completed" and not _is_trial((event.get("properties") or {}).get("is_trial"))
        )
        or event.get("event") == "subscription_renewed"
        for event in subscription_events
    )

    chosen_value: object = "unknown"
    source = "none"
    source_id = "none"
    observed = None
    received = None
    confidence = 0.2
    conflicts: list[Conflict] = []

    event_is_newer = bool(event_time and (not profile_time or event_time >= profile_time))
    if latest_event is not None and event_is_newer:
        chosen_value = event_status
        source = f"event.{latest_event.get('event')}"
        source_id = f"event:{latest_event.get('event_id')}"
        observed = latest_event.get("occurred_at")
        received = event_time
        confidence = 1.0
        if profile_status and profile_status != event_status:
            conflicts.append(
                Conflict(
                    fact="subscription_status",
                    candidates=[
                        {"value": event_status, "source": source, "source_id": source_id, "at": event_time},
                        {
                            "value": profile_status,
                            "source": "profile.subscription_status",
                            "source_id": f"profile:{(profile or {}).get('user_id')}",
                            "at": profile_time,
                        },
                    ],
                    chosen_value=event_status,
                    precedence="latest received_at event beats an older profile snapshot",
                    resolution_reason="A subscription event received after the profile update is the current status.",
                )
            )
    elif profile_status:
        chosen_value = profile_status
        source = "profile.subscription_status"
        source_id = f"profile:{(profile or {}).get('user_id')}"
        observed = profile_time
        received = profile_time
        confidence = 0.7
        if event_status and event_status != profile_status:
            conflicts.append(
                Conflict(
                    fact="subscription_status",
                    candidates=[
                        {
                            "value": profile_status,
                            "source": source,
                            "source_id": source_id,
                            "at": profile_time,
                        },
                        {
                            "value": event_status,
                            "source": f"event.{latest_event.get('event')}",
                            "source_id": f"event:{latest_event.get('event_id')}",
                            "at": event_time,
                        },
                    ],
                    chosen_value=profile_status,
                    precedence="profile updated_at is later than the last subscription event",
                    resolution_reason="The profile update is newer than the last subscription event, so the snapshot wins. The older event stays in the conflict.",
                )
            )
    fact = _fact(
        "subscription_status",
        chosen_value,
        source,
        source_id,
        observed,
        received,
        as_of,
        confidence,
    )
    return fact, conflicts, ever_paid


def resolve_consent(channel: str, events: list[dict], profile: dict | None, as_of: str) -> tuple[Fact, list[Conflict]]:
    """Opt-in comes only from the profile. A later opt-out for that channel overrides it."""
    if channel not in CONSENT_FIELDS:
        raise ValueError(f"unknown consent channel: {channel}")
    field_name = CONSENT_FIELDS[channel]
    fact_name = field_name
    profile_value = _bool_field((profile or {}).get(field_name))
    profile_time = _profile_time(profile)
    visible = _events_until(events, as_of)
    opt_outs = [
        event
        for event in visible
        if event.get("event") == "notification_opt_out"
        and (event.get("properties") or {}).get("channel") == channel
    ]
    latest_opt_out = opt_outs[-1] if opt_outs else None
    conflicts: list[Conflict] = []

    if latest_opt_out is not None and (not profile_time or latest_opt_out.get("received_at") >= profile_time):
        if profile_value is True:
            conflicts.append(
                Conflict(
                    fact=fact_name,
                    candidates=[
                        {
                            "value": False,
                            "source": "event.notification_opt_out",
                            "source_id": f"event:{latest_opt_out.get('event_id')}",
                        },
                        {
                            "value": True,
                            "source": f"profile.{field_name}",
                            "source_id": f"profile:{(profile or {}).get('user_id')}",
                        },
                    ],
                    chosen_value=False,
                    precedence="later notification_opt_out overrides the snapshot",
                    resolution_reason=f"A {channel} opt-out received after the profile update removes consent.",
                )
            )
        fact = _fact(
            fact_name,
            False,
            "event.notification_opt_out",
            f"event:{latest_opt_out.get('event_id')}",
            latest_opt_out.get("occurred_at"),
            latest_opt_out.get("received_at"),
            as_of,
            1.0,
        )
        return fact, conflicts

    if profile_value is None:
        fact = _fact(fact_name, None, "none", "none", None, None, as_of, 0.0)
        return fact, conflicts

    if latest_opt_out is not None and profile_value is True:
        conflicts.append(
            Conflict(
                fact=fact_name,
                candidates=[
                    {"value": True, "source": f"profile.{field_name}", "source_id": f"profile:{(profile or {}).get('user_id')}"},
                    {
                        "value": False,
                        "source": "event.notification_opt_out",
                        "source_id": f"event:{latest_opt_out.get('event_id')}",
                    },
                ],
                chosen_value=True,
                precedence="profile updated_at is later than the opt-out",
                resolution_reason="The profile was updated after the opt-out and still records opt-in. The older opt-out remains visible.",
            )
        )
    fact = _fact(
        fact_name,
        profile_value,
        f"profile.{field_name}",
        f"profile:{(profile or {}).get('user_id')}",
        profile_time,
        profile_time,
        as_of,
        0.8,
    )
    return fact, conflicts


def resolve_signup(events: list[dict], profile: dict | None, as_of: str) -> tuple[Fact, list[Conflict]]:
    visible = _events_until(events, as_of)
    signups = [event for event in visible if event.get("event") == "signup_completed"]
    conflicts: list[Conflict] = []
    if signups:
        earliest = min(signups, key=lambda event: event.get("received_at") or "")
        profile_signup = (profile or {}).get("signup_at") or None
        if profile_signup and profile_signup != earliest.get("received_at"):
            conflicts.append(
                Conflict(
                    fact="signup_at",
                    candidates=[
                        {
                            "value": earliest.get("received_at"),
                            "source": "event.signup_completed",
                            "source_id": f"event:{earliest.get('event_id')}",
                        },
                        {
                            "value": profile_signup,
                            "source": "profile.signup_at",
                            "source_id": f"profile:{(profile or {}).get('user_id')}",
                        },
                    ],
                    chosen_value=earliest.get("received_at"),
                    precedence="earliest signup_completed received_at, else profile.signup_at",
                    resolution_reason="The earliest server receipt of signup_completed is the signup time. occurred_at is not used because the device clock can be skewed.",
                )
            )
        fact = _fact(
            "signup_at",
            earliest.get("received_at"),
            "event.signup_completed",
            f"event:{earliest.get('event_id')}",
            earliest.get("occurred_at"),
            earliest.get("received_at"),
            as_of,
            1.0,
        )
        return fact, conflicts
    if profile and profile.get("signup_at"):
        fact = _fact(
            "signup_at",
            profile.get("signup_at"),
            "profile.signup_at",
            f"profile:{profile.get('user_id')}",
            profile.get("signup_at"),
            _profile_time(profile),
            as_of,
            0.6,
        )
        return fact, []
    return _fact("signup_at", None, "none", "none", None, None, as_of, 0.0), []


def resolve_request_dialect(trigger: dict, profile: dict | None, as_of: str) -> tuple[Fact, list[Conflict]]:
    """The triggering event's app dialect wins over profile and campaign deeplink."""
    used = trigger.get("app_dialect")
    if used is None or str(used).strip() == "":
        used = None
    candidates = [
        {
            "value": trigger.get("app_dialect"),
            "source": "trigger.app_dialect",
            "source_id": f"event:{trigger.get('event_id')}",
        }
    ]
    preferred = ((profile or {}).get("preferred_dialect") or "").strip()
    if preferred and preferred != (used or ""):
        candidates.append(
            {
                "value": preferred,
                "source": "profile.preferred_dialect",
                "source_id": f"profile:{(profile or {}).get('user_id')}",
            }
        )
    deeplink = parse_campaign_deeplink((trigger.get("properties") or {}).get("deeplink"))
    if deeplink and deeplink["dialect"] != (used or ""):
        candidates.append(
            {
                "value": deeplink["dialect"],
                "source": "trigger.deeplink",
                "source_id": f"event:{trigger.get('event_id')}",
                "title_id": deeplink["title_id"],
            }
        )
    conflicts: list[Conflict] = []
    if len(candidates) > 1:
        conflicts.append(
            Conflict(
                fact="app_dialect",
                candidates=candidates,
                chosen_value=used,
                precedence="trigger.app_dialect",
                resolution_reason=(
                    "The app setting determines the dialect requested by the client. "
                    "A campaign deeplink never overrides the current app setting."
                ),
            )
        )
    valid = used in VALID_DIALECTS
    fact = _fact(
        "app_dialect",
        used,
        "trigger.app_dialect",
        f"event:{trigger.get('event_id')}",
        trigger.get("occurred_at"),
        trigger.get("received_at") or trigger.get("deliver_at"),
        as_of,
        1.0 if valid else 0.2,
    )
    return fact, conflicts


def build_viewer_facts(
    viewer_id: str,
    events: list[dict],
    profile: dict | None,
    as_of: str,
) -> ViewerFacts:
    """Resolve durable viewer facts as of a replay timestamp. Does not use wall-clock time."""
    bundle = ViewerFacts(viewer_id=viewer_id, as_of=as_of)
    subscription, subscription_conflicts, ever_paid = resolve_subscription(events, profile, as_of)
    bundle.facts["subscription_status"] = subscription
    bundle.conflicts.extend(subscription_conflicts)
    bundle.facts["ever_paid"] = _fact(
        "ever_paid",
        ever_paid,
        "events.purchase_or_renewal" if ever_paid else "no_paid_event_observed",
        f"viewer:{viewer_id}",
        None,
        as_of,
        as_of,
        1.0 if events or profile else 0.3,
    )
    for channel in ("push", "whatsapp"):
        fact, conflicts = resolve_consent(channel, events, profile, as_of)
        bundle.facts[fact.name] = fact
        bundle.conflicts.extend(conflicts)
    signup, signup_conflicts = resolve_signup(events, profile, as_of)
    bundle.facts["signup_at"] = signup
    bundle.conflicts.extend(signup_conflicts)
    visible = _events_until(events, as_of)
    play_events = [event for event in visible if event.get("event") == "play_started"]
    if not visible and profile is None:
        play_value: object = "unknown"
        play_confidence = 0.0
        play_source = "none"
    elif play_events:
        play_value = True
        play_confidence = 1.0
        play_source = "event.play_started"
    else:
        play_value = False
        play_confidence = 0.9
        play_source = "no_play_started_observed"
    bundle.facts["has_observed_play_started"] = _fact(
        "has_observed_play_started",
        play_value,
        play_source,
        f"event:{play_events[-1]['event_id']}" if play_events else f"viewer:{viewer_id}",
        play_events[-1].get("occurred_at") if play_events else None,
        play_events[-1].get("received_at") if play_events else None,
        as_of,
        play_confidence,
    )
    bundle.facts["has_profile"] = _fact(
        "has_profile",
        profile is not None,
        "profile_snapshot" if profile else "none",
        f"profile:{viewer_id}" if profile else "none",
        _profile_time(profile),
        _profile_time(profile),
        as_of,
        1.0,
    )
    return bundle
