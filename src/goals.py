"""Deterministic goal eligibility, ranking, and the active-goal lifecycle.

The model does not choose goals. Every configured goal is considered. A goal is
selectable only when its success event is present in the observed event stream
and its eligibility predicate is true on pack evidence.

Ranking policy (lower rank wins). This is not shortest-window order:

1. G04 trial_to_paid. Trial is a rare, explicit, expiring state.
2. G01 first_play. 24h window, and it does not overlap a series the user already started.
3. G03 second_day_return. 48h window for a viewer who signed up within 2 days.
4. G07 resume_series. A specific unfinished slug and a 72h window.
5. G06 reactivate_lapsed. Lapsed is a positive subscription fact. It beats a generic
   absence goal so a lapsed viewer is not only asked to play.
6. G12 return_after_absence. A evidenced 14-day gap. For a never-paid dormant viewer
   this is a nearer step than a purchase goal.
7. G05 first_paid. Never-paid viewers who are present and have nothing to resume.
8. G09 weekly_habit. Low-habit active subscribers. This is ahead of G08 on purpose.
9. G08 try_new_release. Every active subscriber is eligible whenever a recent title
   exists, which is true across this catalogue. Ranking G08 first would make G09 unreachable.
10. G02 first_full_episode. "Never completed an episode" is true for a large share of
    viewers, so it is only the residual goal.

An active goal stays active until it succeeds, its premise becomes false, or its
deadline passes. A higher-ranked goal that becomes eligible later does not replace it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta

from src.data_loader import title_available
from src.facts import build_viewer_facts
from src.time_utils import add_window, business_time, format_utc, freshness_seconds, ist_date, parse_utc

PRIORITY = ("G04", "G01", "G03", "G07", "G06", "G12", "G05", "G09", "G08", "G02")
OPEN_STATUS = "ACTIVE"
TERMINAL_STATUSES = frozenset({"SUCCEEDED", "CANCELLED", "EXPIRED"})
SERIES_TYPES = frozenset({"show", "micro_drama"})
BUSINESS_TIME_ASSUMPTION = (
    "Windows and distinct IST days use business time: occurred_at when it is within "
    "600 seconds of received_at, otherwise received_at."
)


@dataclass
class Eligibility:
    goal_id: str
    eligible: bool
    measurable: bool
    reason: str
    confidence: float
    evidence: list[dict] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    success_params: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ActiveGoal:
    goal_id: str
    name: str
    rule_version: str
    started_at: str
    deadline: str
    success_event: str | None
    success_predicate: str
    success_params: dict
    status: str
    reason: str
    confidence: float
    evidence: list[dict]
    assumptions: list[str]
    closed_reason: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class GoalResolution:
    goal: ActiveGoal | None
    closed: ActiveGoal | None
    abstain_reason: str | None
    considered: list[dict]

    def to_dict(self) -> dict:
        return {
            "goal": None if self.goal is None else self.goal.to_dict(),
            "closed": None if self.closed is None else self.closed.to_dict(),
            "abstain_reason": self.abstain_reason,
            "considered": self.considered,
        }


@dataclass
class Snapshot:
    viewer_id: str
    as_of: str
    events: list[dict]
    profile: dict | None
    catalogue: dict[str, dict]
    episodes: dict[str, list[str]]
    trigger_event_id: str | None = None
    event_source: str = "data/events.jsonl"

    def __post_init__(self) -> None:
        self.as_of_dt = parse_utc(self.as_of)
        if self.as_of_dt is None:
            raise ValueError("snapshot as_of is required")
        facts = build_viewer_facts(self.viewer_id, self.events, self.profile, self.as_of)
        self.facts = facts
        self.subscription_status = facts.facts["subscription_status"].value
        self.ever_paid = bool(facts.facts["ever_paid"].value)
        signup = facts.facts["signup_at"].value
        self.signup_at = parse_utc(signup) if isinstance(signup, str) else None
        self.has_observed_history = self.profile is not None or any(
            event.get("event_id") != self.trigger_event_id for event in self.events
        )


class GoalCatalog:
    """Configured goals plus mid-replay success-rule corrections."""

    def __init__(self, document: dict, observed_event_types: set[str]):
        self.base_version = str(document.get("version") or "unversioned")
        goals = document.get("goals") or []
        self.goals = {goal["goal_id"]: goal for goal in goals}
        self.observed = set(observed_event_types)
        self.overrides: dict[str, dict] = {}
        self.excluded_reasons: dict[str, str] = {}
        for goal_id in self.goals:
            self._refresh_measurability(goal_id)

    def _refresh_measurability(self, goal_id: str) -> None:
        event, _condition = self.success_spec(goal_id)
        if not event:
            self.excluded_reasons[goal_id] = "success event is null; the goal is not measurable"
        elif event not in self.observed:
            self.excluded_reasons[goal_id] = (
                f"success event {event} is absent from the observed event stream"
            )
        else:
            self.excluded_reasons.pop(goal_id, None)

    def rule_version(self, goal_id: str) -> str:
        override = self.overrides.get(goal_id)
        if override:
            return override["rule_version"]
        return self.base_version

    def success_spec(self, goal_id: str) -> tuple[str | None, str]:
        override = self.overrides.get(goal_id)
        if override:
            return override["event"], override["condition"]
        success = self.goals[goal_id].get("success") or {}
        return success.get("event"), str(success.get("condition") or "")

    def window_hours(self, goal_id: str) -> float:
        return float(self.goals[goal_id]["success"]["window_hours"])

    def apply_control(self, control: dict) -> str | None:
        """Apply a goal_rule_corrected control. Returns the affected goal id."""
        if control.get("type") != "goal_rule_corrected":
            return None
        goal_id = control.get("goal_id")
        if goal_id not in self.goals:
            return None
        new_success = control.get("new_success") or {}
        self.overrides[goal_id] = {
            "event": new_success.get("event"),
            "condition": str(new_success.get("condition") or ""),
            "rule_version": f"{self.base_version}+corrected:{control.get('at')}",
        }
        self._refresh_measurability(goal_id)
        return goal_id


def _event_time(event: dict) -> datetime | None:
    occurred = parse_utc(event.get("occurred_at"))
    received = parse_utc(event.get("received_at"))
    if occurred is None or received is None:
        return received or occurred
    return business_time(occurred, received)


def _received(event: dict) -> datetime | None:
    return parse_utc(event.get("received_at"))


def _is_trial(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() == "true"


def _evidence(fact: str, event: dict, as_of: datetime, source: str) -> dict:
    received = _received(event)
    freshness = None
    if received is not None:
        freshness = round(freshness_seconds(received, as_of), 3)
    return {
        "ref": f"event:{event.get('event_id')}",
        "fact": fact,
        "source": source,
        "observed_at": event.get("occurred_at"),
        "received_at": event.get("received_at"),
        "freshness_seconds": freshness,
    }


def _absence(fact: str, viewer_id: str, as_of: str, source: str) -> dict:
    return {
        "ref": f"viewer:{viewer_id}",
        "fact": fact,
        "source": source,
        "observed_at": as_of,
        "received_at": as_of,
        "freshness_seconds": 0,
    }


def _profile_evidence(fact: str, profile: dict, as_of: datetime) -> dict:
    stamp = parse_utc(profile.get("updated_at") or profile.get("snapshot_at"))
    freshness = round(freshness_seconds(stamp, as_of), 3) if stamp is not None else None
    return {
        "ref": f"profile:{profile.get('user_id')}",
        "fact": fact,
        "source": "data/profiles_snapshot.csv",
        "observed_at": profile.get("updated_at") or profile.get("snapshot_at"),
        "received_at": profile.get("snapshot_at"),
        "freshness_seconds": freshness,
    }


def _prior(snapshot: Snapshot) -> list[dict]:
    return [
        event
        for event in snapshot.events
        if event.get("event_id") != snapshot.trigger_event_id
        and (_received(event) is None or _received(event) <= snapshot.as_of_dt)
    ]


def _slug(title_id: str | None, catalogue: dict[str, dict]) -> str | None:
    if not title_id:
        return None
    row = catalogue.get(title_id)
    if not row:
        return None
    return row.get("content_slug")


def _release_date(row: dict):
    raw = (row.get("release_date") or "").strip()
    if not raw:
        return None
    return datetime.strptime(raw[:10], "%Y-%m-%d").date()


def _recent_release(row: dict, start: datetime) -> bool:
    released = _release_date(row)
    if released is None:
        return False
    end = ist_date(start)
    window_start = end - timedelta(days=30)
    return window_start <= released <= end


def evaluate_goal(catalog: GoalCatalog, goal_id: str, snapshot: Snapshot) -> Eligibility:
    goal = catalog.goals[goal_id]
    if goal_id in catalog.excluded_reasons:
        return Eligibility(
            goal_id=goal_id,
            eligible=False,
            measurable=False,
            reason=catalog.excluded_reasons[goal_id],
            confidence=0.0,
        )
    if goal_id == "G07":
        return _eval_g07(goal, snapshot)
    if goal_id == "G08":
        return _eval_g08(goal, snapshot)
    if goal_id == "G09":
        return _eval_g09(goal, snapshot)
    if goal_id == "G12":
        return _eval_g12(goal, snapshot)
    return _eval_generic(goal, snapshot)


def _eval_generic(goal: dict, snapshot: Snapshot) -> Eligibility:
    eligibility = goal.get("eligibility") or {}
    structured = [key for key in ("signed_up_within_days", "requires_no_prior_event", "subscription_status") if key in eligibility]
    if not structured:
        return Eligibility(
            goal_id=goal["goal_id"],
            eligible=False,
            measurable=True,
            reason="eligibility text has no deterministic predicate",
            confidence=0.0,
        )
    evidence: list[dict] = []
    assumptions: list[str] = [BUSINESS_TIME_ASSUMPTION]
    reasons: list[str] = []
    confidence = 0.8

    if "signed_up_within_days" in eligibility:
        days = float(eligibility["signed_up_within_days"])
        if snapshot.signup_at is None:
            return Eligibility(goal["goal_id"], False, True, "signup time is unknown", 0.0)
        age = snapshot.as_of_dt - snapshot.signup_at
        if age < timedelta(0) or age > timedelta(days=days):
            return Eligibility(
                goal["goal_id"],
                False,
                True,
                f"signup is not within {days:g} days",
                0.8,
            )
        reasons.append(f"signed up within {days:g} days")

    if "requires_no_prior_event" in eligibility:
        event_name = eligibility["requires_no_prior_event"]
        if not snapshot.has_observed_history:
            return Eligibility(
                goal["goal_id"],
                False,
                True,
                f"no history from which to conclude {event_name} never occurred",
                0.0,
            )
        prior = [event for event in _prior(snapshot) if event.get("event") == event_name]
        if prior:
            return Eligibility(
                goal["goal_id"],
                False,
                True,
                f"prior {event_name} exists",
                0.9,
                evidence=[_evidence(f"prior_{event_name}", prior[-1], snapshot.as_of_dt, snapshot.event_source)],
            )
        evidence.append(_absence(f"no_prior_{event_name}", snapshot.viewer_id, snapshot.as_of, snapshot.event_source))
        reasons.append(f"no observed {event_name}")
        if snapshot.profile is not None and not any(event.get("event_id") != snapshot.trigger_event_id for event in snapshot.events):
            assumptions.append("Absence is limited to the supplied pack; this viewer has a profile and no events.")
            confidence = 0.55

    if "subscription_status" in eligibility:
        allowed = set(eligibility["subscription_status"])
        status = snapshot.subscription_status
        if status not in allowed:
            return Eligibility(
                goal["goal_id"],
                False,
                True,
                f"subscription status {status} is not in {sorted(allowed)}",
                0.8,
            )
        fact = snapshot.facts.facts["subscription_status"]
        if fact.source.startswith("profile") and snapshot.profile is not None:
            evidence.append(_profile_evidence("subscription_status", snapshot.profile, snapshot.as_of_dt))
            confidence = min(confidence, fact.confidence)
        elif fact.source_id.startswith("event:"):
            matched = [event for event in snapshot.events if f"event:{event.get('event_id')}" == fact.source_id]
            if matched:
                evidence.append(_evidence("subscription_status", matched[-1], snapshot.as_of_dt, snapshot.event_source))
        reasons.append(f"subscription status is {status}")

    if goal["goal_id"] == "G05" and snapshot.ever_paid:
        return Eligibility(
            goal["goal_id"],
            False,
            True,
            "a paid purchase or renewal is already on record",
            0.9,
            assumptions=["G05 requires both subscription status never and no paid event."],
        )
    if goal["goal_id"] == "G06":
        assumptions.append(
            "goals.json says 'recently lapsed' but gives no day count; any current lapsed status qualifies."
        )

    return Eligibility(
        goal_id=goal["goal_id"],
        eligible=True,
        measurable=True,
        reason="; ".join(reasons),
        confidence=confidence,
        evidence=evidence,
        assumptions=assumptions,
    )


def _eval_g07(goal: dict, snapshot: Snapshot) -> Eligibility:
    candidates = []
    for event in _prior(snapshot):
        if event.get("event") != "play_started":
            continue
        title = snapshot.catalogue.get((event.get("properties") or {}).get("title_id"))
        if not title or title.get("content_type") not in SERIES_TYPES:
            continue
        slug = title.get("content_slug")
        when = _event_time(event)
        if not slug or when is None:
            continue
        candidates.append((slug, when, event))
    if not candidates:
        return Eligibility(goal["goal_id"], False, True, "no started show or micro-drama", 0.6)
    by_slug: dict[str, tuple[datetime, dict]] = {}
    for slug, when, event in candidates:
        current = by_slug.get(slug)
        if current is None or when > current[0]:
            by_slug[slug] = (when, event)
    qualified = []
    for slug, (when, event) in by_slug.items():
        if snapshot.as_of_dt - when < timedelta(days=3):
            continue
        known = snapshot.episodes.get(slug) or []
        if not known:
            continue
        completed = set()
        for item in _prior(snapshot):
            if item.get("event") != "episode_completed":
                continue
            if _slug((item.get("properties") or {}).get("title_id"), snapshot.catalogue) == slug:
                episode_id = (item.get("properties") or {}).get("episode_id")
                if episode_id:
                    completed.add(episode_id)
        remaining = [episode_id for episode_id in known if episode_id not in completed]
        if not remaining:
            continue
        qualified.append((when, slug, event, remaining))
    if not qualified:
        return Eligibility(
            goal["goal_id"],
            False,
            True,
            "no series with a play gap of at least 3 days and unwatched episodes",
            0.7,
        )
    qualified.sort(key=lambda item: (-item[0].timestamp(), item[1]))
    when, slug, event, remaining = qualified[0]
    return Eligibility(
        goal_id=goal["goal_id"],
        eligible=True,
        measurable=True,
        reason=f"unfinished series {slug} has not been played for at least 3 days; {len(remaining)} episodes remain",
        confidence=0.9,
        evidence=[_evidence("last_play_started", event, snapshot.as_of_dt, snapshot.event_source)],
        assumptions=[
            BUSINESS_TIME_ASSUMPTION,
            "G07 matches the stored content_slug. In this pack a slug does not identify another dialect.",
        ],
        success_params={"content_slug": slug, "remaining_episodes": len(remaining)},
    )


def _eval_g08(goal: dict, snapshot: Snapshot) -> Eligibility:
    if snapshot.subscription_status != "active":
        return Eligibility(goal["goal_id"], False, True, "subscription status is not active", 0.8)
    available = [
        row
        for row in snapshot.catalogue.values()
        if _recent_release(row, snapshot.as_of_dt) and title_available(row, snapshot.as_of_dt)
    ]
    if not available:
        return Eligibility(
            goal["goal_id"],
            False,
            True,
            "no available title was released in the 30 days before goal start",
            0.5,
        )
    evidence = []
    if snapshot.profile is not None:
        evidence.append(_profile_evidence("subscription_status", snapshot.profile, snapshot.as_of_dt))
    return Eligibility(
        goal_id=goal["goal_id"],
        eligible=True,
        measurable=True,
        reason="active subscriber and at least one available title was released in the prior 30 IST days",
        confidence=0.75,
        evidence=evidence,
        assumptions=[
            BUSINESS_TIME_ASSUMPTION,
            "A release counts when its release_date is inside the inclusive IST-date window ending on the goal start date.",
        ],
    )


def _play_days(snapshot: Snapshot, since: datetime) -> set:
    days = set()
    for event in _prior(snapshot):
        if event.get("event") != "play_started":
            continue
        when = _event_time(event)
        if when is not None and since <= when <= snapshot.as_of_dt:
            days.add(ist_date(when))
    return days


def _eval_g09(goal: dict, snapshot: Snapshot) -> Eligibility:
    if snapshot.subscription_status != "active":
        return Eligibility(goal["goal_id"], False, True, "subscription status is not active", 0.8)
    days = _play_days(snapshot, snapshot.as_of_dt - timedelta(days=14))
    if len(days) > 1:
        return Eligibility(
            goal["goal_id"],
            False,
            True,
            f"plays on {len(days)} distinct IST days in the last 14 days",
            0.85,
        )
    return Eligibility(
        goal_id=goal["goal_id"],
        eligible=True,
        measurable=True,
        reason=f"active subscriber with plays on {len(days)} IST day(s) in the last 14 days",
        confidence=0.8,
        assumptions=[BUSINESS_TIME_ASSUMPTION, "Distinct days are IST dates of business time, not raw event counts."],
    )


def _eval_g12(goal: dict, snapshot: Snapshot) -> Eligibility:
    window_start = snapshot.as_of_dt - timedelta(days=14)
    recent_opens = []
    older_anchor = snapshot.signup_at is not None and snapshot.signup_at <= window_start
    for event in _prior(snapshot):
        received = _received(event)
        if received is None:
            continue
        if received <= window_start:
            older_anchor = True
        if event.get("event") == "app_open" and window_start < received <= snapshot.as_of_dt:
            recent_opens.append(event)
    if recent_opens:
        return Eligibility(
            goal["goal_id"],
            False,
            True,
            "an app_open was received in the 14 days before the trigger",
            0.9,
            evidence=[_evidence("recent_app_open", recent_opens[-1], snapshot.as_of_dt, snapshot.event_source)],
        )
    if not older_anchor:
        return Eligibility(
            goal["goal_id"],
            False,
            True,
            "no event or signup at least 14 days old, so absence is not evidenced",
            0.2,
        )
    return Eligibility(
        goal_id=goal["goal_id"],
        eligible=True,
        measurable=True,
        reason="no app_open in the 14 days before the trigger, with older history present",
        confidence=0.8,
        evidence=[_absence("no_app_open_14d", snapshot.viewer_id, snapshot.as_of, snapshot.event_source)],
        assumptions=[BUSINESS_TIME_ASSUMPTION, "The triggering event itself is not counted as a prior app_open."],
    )


def _rank(goal_id: str) -> tuple[int, str]:
    try:
        return (PRIORITY.index(goal_id), goal_id)
    except ValueError:
        return (len(PRIORITY), goal_id)


def select_eligible(catalog: GoalCatalog, snapshot: Snapshot) -> tuple[Eligibility | None, list[dict]]:
    considered = []
    winners = []
    for goal_id in sorted(catalog.goals, key=_rank):
        result = evaluate_goal(catalog, goal_id, snapshot)
        considered.append(
            {
                "goal_id": goal_id,
                "eligible": result.eligible,
                "measurable": result.measurable,
                "reason": result.reason,
            }
        )
        if result.eligible:
            winners.append(result)
    if not winners:
        return None, considered
    winners.sort(key=lambda item: _rank(item.goal_id))
    return winners[0], considered


def commit_goal(catalog: GoalCatalog, chosen: Eligibility, snapshot: Snapshot) -> ActiveGoal:
    goal = catalog.goals[chosen.goal_id]
    event, predicate = catalog.success_spec(chosen.goal_id)
    started = snapshot.as_of_dt
    deadline = add_window(started, catalog.window_hours(chosen.goal_id))
    return ActiveGoal(
        goal_id=chosen.goal_id,
        name=goal["name"],
        rule_version=catalog.rule_version(chosen.goal_id),
        started_at=format_utc(started),
        deadline=format_utc(deadline),
        success_event=event,
        success_predicate=predicate,
        success_params=dict(chosen.success_params),
        status=OPEN_STATUS,
        reason=chosen.reason,
        confidence=chosen.confidence,
        evidence=list(chosen.evidence),
        assumptions=list(chosen.assumptions),
    )


def _matches_event(goal: ActiveGoal, event: dict, catalogue: dict[str, dict]) -> bool:
    if event.get("event") != goal.success_event:
        return False
    when = _event_time(event)
    started = parse_utc(goal.started_at)
    deadline = parse_utc(goal.deadline)
    if when is None or started is None or deadline is None:
        return False
    if when < started or when > deadline:
        return False
    predicate = (goal.success_predicate or "").lower()
    props = event.get("properties") or {}
    if "is_trial" in predicate or "paid plan" in predicate:
        if _is_trial(props.get("is_trial")):
            return False
    if "later ist" in predicate and ist_date(when) <= ist_date(started):
        return False
    slug = goal.success_params.get("content_slug")
    if slug and _slug(props.get("title_id"), catalogue) != slug:
        return False
    if "30 days" in predicate:
        title = catalogue.get(props.get("title_id") or "")
        if title is None or not _recent_release(title, started):
            return False
    return True


def success_evidence(goal: ActiveGoal, snapshot: Snapshot) -> list[dict]:
    """Events that satisfy the stored predicate inside the goal window."""
    if goal.goal_id == "G09" or "distinct ist" in (goal.success_predicate or "").lower():
        days: dict = {}
        for event in snapshot.events:
            if not _matches_event(replace(goal, success_predicate="any title"), event, snapshot.catalogue):
                continue
            when = _event_time(event)
            if when is None:
                continue
            days.setdefault(ist_date(when), event)
        if len(days) >= 3:
            return [
                _evidence("play_started_ist_day", event, snapshot.as_of_dt, snapshot.event_source)
                for _day, event in sorted(days.items(), key=lambda item: item[0])[:3]
            ]
        return []
    matched = [event for event in snapshot.events if _matches_event(goal, event, snapshot.catalogue)]
    if not matched:
        return []
    return [_evidence(goal.success_event or "success", matched[0], snapshot.as_of_dt, snapshot.event_source)]


def premise_holds(goal: ActiveGoal, snapshot: Snapshot) -> tuple[bool, str]:
    status = snapshot.subscription_status
    if goal.goal_id == "G04":
        return status == "trial_active", "subscription is no longer trial_active"
    if goal.goal_id == "G05":
        ok = status == "never" and not snapshot.ever_paid
        return ok, "viewer is no longer never-paid"
    if goal.goal_id == "G06":
        return status == "lapsed", "viewer is no longer lapsed"
    if goal.goal_id == "G07":
        slug = goal.success_params.get("content_slug")
        known = snapshot.episodes.get(slug) or []
        completed = set()
        for event in snapshot.events:
            if event.get("event") != "episode_completed":
                continue
            if _slug((event.get("properties") or {}).get("title_id"), snapshot.catalogue) != slug:
                continue
            episode_id = (event.get("properties") or {}).get("episode_id")
            if episode_id:
                completed.add(episode_id)
        remaining = [episode_id for episode_id in known if episode_id not in completed]
        return bool(remaining), "no unwatched episodes remain on the chosen series"
    if goal.goal_id in {"G08", "G09"}:
        return status == "active", "viewer is no longer an active subscriber"
    return True, ""


def refresh_goal(goal: ActiveGoal, snapshot: Snapshot, catalog: GoalCatalog) -> ActiveGoal:
    """Apply new events to one open goal. Closed goals are returned unchanged."""
    if goal.status != OPEN_STATUS:
        return goal
    if goal.goal_id in catalog.excluded_reasons:
        return replace(
            goal,
            status="CANCELLED",
            closed_reason=catalog.excluded_reasons[goal.goal_id],
        )
    evidence = success_evidence(goal, snapshot)
    if evidence:
        return replace(goal, status="SUCCEEDED", evidence=list(goal.evidence) + evidence, closed_reason=None)
    deadline = parse_utc(goal.deadline)
    if deadline is not None and snapshot.as_of_dt > deadline:
        return replace(goal, status="EXPIRED", closed_reason="deadline passed without a success event")
    holds, reason = premise_holds(goal, snapshot)
    if not holds:
        return replace(goal, status="CANCELLED", closed_reason=reason)
    return goal


def retarget_open_goal(goal: ActiveGoal, snapshot: Snapshot, catalog: GoalCatalog) -> ActiveGoal:
    """Re-judge one goal after a rule correction. Unrelated and closed goals stay as they are."""
    if goal.status != OPEN_STATUS:
        return goal
    event, predicate = catalog.success_spec(goal.goal_id)
    version = catalog.rule_version(goal.goal_id)
    revised = goal
    if event != goal.success_event or predicate != goal.success_predicate or version != goal.rule_version:
        revised = replace(
            goal,
            success_event=event,
            success_predicate=predicate,
            rule_version=version,
        )
    return refresh_goal(revised, snapshot, catalog)


def resolve_goal(catalog: GoalCatalog, snapshot: Snapshot, active: ActiveGoal | None) -> GoalResolution:
    """Keep the open goal, or close it and select at most one replacement."""
    closed = None
    current = active
    if current is not None and current.status == OPEN_STATUS:
        current = refresh_goal(current, snapshot, catalog)
        if current.status == OPEN_STATUS:
            return GoalResolution(goal=current, closed=None, abstain_reason=None, considered=[])
        closed = current
    chosen, considered = select_eligible(catalog, snapshot)
    if chosen is None:
        reason = "no measurable eligible goal"
        return GoalResolution(goal=None, closed=closed, abstain_reason=reason, considered=considered)
    return GoalResolution(
        goal=commit_goal(catalog, chosen, snapshot),
        closed=closed,
        abstain_reason=None,
        considered=considered,
    )
