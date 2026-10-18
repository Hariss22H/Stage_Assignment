"""Goal eligibility, ranking, cancellation, and the G03 rule correction."""

from datetime import timedelta
from pathlib import Path

from src.config import load_goals
from src.goals import (
    PRIORITY,
    ActiveGoal,
    GoalCatalog,
    Snapshot,
    evaluate_goal,
    resolve_goal,
    retarget_open_goal,
)
from src.state_store import StateStore
from src.time_utils import add_window, format_utc, parse_utc

AS_OF = "2026-10-01T12:00:00.000Z"
OBSERVED = {
    "app_open",
    "play_started",
    "episode_completed",
    "purchase_completed",
    "signup_completed",
    "subscription_lapsed",
    "subscription_renewed",
    "login_success",
}


def _profile(**overrides):
    base = {
        "user_id": "U-1",
        "signup_at": "2026-09-01T00:00:00.000Z",
        "subscription_status": "never",
        "preferred_dialect": "d1",
        "notifications_opt_in": "true",
        "whatsapp_opt_in": "false",
        "updated_at": "2026-09-29T00:00:00.000Z",
        "snapshot_at": "2026-09-30T00:00:00.000Z",
    }
    base.update(overrides)
    return base


def _event(event_id, name, received, properties=None, occurred=None):
    return {
        "event_id": event_id,
        "user_id": "U-1",
        "event": name,
        "occurred_at": occurred or received,
        "sent_at": occurred or received,
        "received_at": received,
        "app_dialect": "d1",
        "properties": properties or {},
    }


def _catalogue():
    return {
        "T-SHOW": {
            "title_id": "T-SHOW",
            "content_slug": "serial-d1",
            "content_type": "show",
            "dialect": "d1",
            "release_date": "2026-09-20",
            "available_from": "2026-09-20T00:00:00.000Z",
            "available_until": "",
        },
        "T-NEW": {
            "title_id": "T-NEW",
            "content_slug": "new-film-d1",
            "content_type": "film",
            "dialect": "d1",
            "release_date": "2026-09-15",
            "available_from": "2026-09-15T00:00:00.000Z",
            "available_until": "",
        },
        "T-FILM": {
            "title_id": "T-FILM",
            "content_slug": "old-film-d1",
            "content_type": "film",
            "dialect": "d1",
            "release_date": "2024-01-01",
            "available_from": "2024-01-01T00:00:00.000Z",
            "available_until": "",
        },
    }


def _episodes():
    return {"serial-d1": ["serial-d1-s1e1", "serial-d1-s1e2"]}


def _snapshot(events, profile, as_of=AS_OF, trigger_event_id=None):
    return Snapshot(
        viewer_id=profile["user_id"] if profile else "U-1",
        as_of=as_of,
        events=events,
        profile=profile,
        catalogue=_catalogue(),
        episodes=_episodes(),
        trigger_event_id=trigger_event_id,
    )


def _catalog(observed=None):
    document = load_goals(Path("one_user_one_agent_pack"))
    return GoalCatalog(document, set(observed or OBSERVED))


def test_g10_and_g11_are_excluded_from_the_real_config():
    catalog = _catalog()
    assert "success event is null" in catalog.excluded_reasons["G10"]
    assert "tv_app_open" in catalog.excluded_reasons["G11"]
    snap = _snapshot([], _profile(subscription_status="active"))
    resolution = resolve_goal(catalog, snap, None)
    chosen = None if resolution.goal is None else resolution.goal.goal_id
    assert chosen != "G10"
    assert chosen != "G11"
    assert all(not item["eligible"] for item in resolution.considered if item["goal_id"] in {"G10", "G11"})


def test_missing_success_event_excludes_any_goal_not_only_g11():
    catalog = _catalog(observed={"app_open"})
    assert "G01" in catalog.excluded_reasons
    assert "play_started" in catalog.excluded_reasons["G01"]


def test_g01_requires_recent_signup_and_no_play():
    catalog = _catalog()
    recent = _profile(signup_at="2026-09-28T00:00:00.000Z", subscription_status="lapsed")
    snap = _snapshot([], recent)
    assert evaluate_goal(catalog, "G01", snap).eligible
    played = _snapshot([_event("p1", "play_started", "2026-09-29T00:00:00.000Z", {"title_id": "T-FILM"})], recent)
    assert not evaluate_goal(catalog, "G01", played).eligible
    old = _snapshot([], _profile(signup_at="2026-01-01T00:00:00.000Z"))
    assert not evaluate_goal(catalog, "G01", old).eligible


def test_ranking_prefers_specific_goals_over_broad_g02():
    catalog = _catalog()
    # Never-completed viewer with an unfinished series: G07, not G02 or G05.
    events = [
        _event("p1", "play_started", "2026-09-20T00:00:00.000Z", {"title_id": "T-SHOW", "episode_id": "serial-d1-s1e1"}),
    ]
    snap = _snapshot(events, _profile(subscription_status="never", signup_at="2026-01-01T00:00:00.000Z"))
    resolution = resolve_goal(catalog, snap, None)
    assert resolution.goal is not None
    assert resolution.goal.goal_id == "G07"
    assert resolution.goal.success_params["content_slug"] == "serial-d1"
    assert resolution.goal.success_event == "play_started"
    started = parse_utc(resolution.goal.started_at)
    assert resolution.goal.deadline == format_utc(add_window(started, 72))
    assert resolution.goal.evidence
    assert resolution.goal.rule_version == "2026-09-30"

    # New viewer who never played is G01 even though G02 and G03 also match.
    new_snap = _snapshot([], _profile(signup_at="2026-09-30T12:00:00.000Z", subscription_status="never"))
    new_goal = resolve_goal(catalog, new_snap, None).goal
    assert new_goal is not None
    assert new_goal.goal_id == "G01"
    assert PRIORITY.index("G01") < PRIORITY.index("G02")
    assert PRIORITY.index("G09") < PRIORITY.index("G08")


def test_g09_uses_distinct_ist_days_and_outranks_generic_g08():
    catalog = _catalog()
    profile = _profile(subscription_status="active", signup_at="2025-01-01T00:00:00.000Z")
    one_day = [
        _event("a", "play_started", "2026-09-28T04:00:00.000Z", {"title_id": "T-FILM"}),
        _event("b", "play_started", "2026-09-28T05:00:00.000Z", {"title_id": "T-FILM"}),
        _event("o", "app_open", "2026-09-28T03:30:00.000Z"),
    ]
    # 04:00Z is 09:30 IST and 05:00Z is 10:30 IST, same IST day.
    snap = _snapshot(one_day, profile)
    assert evaluate_goal(catalog, "G09", snap).eligible
    assert resolve_goal(catalog, snap, None).goal.goal_id == "G09"
    two_days = one_day + [_event("c", "play_started", "2026-09-29T04:00:00.000Z", {"title_id": "T-FILM"})]
    assert not evaluate_goal(catalog, "G09", _snapshot(two_days, profile)).eligible
    assert resolve_goal(catalog, _snapshot(two_days, profile), None).goal.goal_id == "G08"


def test_g12_ignores_the_trigger_and_needs_an_older_anchor():
    catalog = _catalog()
    profile = _profile(signup_at="2026-01-01T00:00:00.000Z", subscription_status="active")
    trigger = _event("trig", "app_open", AS_OF)
    snap = _snapshot([trigger], profile, trigger_event_id="trig")
    # Active subscribers match G09/G08 before G12. Force the absence check directly.
    assert evaluate_goal(catalog, "G12", snap).eligible
    recent = [trigger, _event("old-open", "app_open", "2026-09-25T00:00:00.000Z")]
    assert not evaluate_goal(catalog, "G12", _snapshot(recent, profile, trigger_event_id="trig")).eligible
    empty = _snapshot([trigger], None, trigger_event_id="trig")
    empty.viewer_id = "U-new"
    assert not evaluate_goal(catalog, "G12", empty).eligible


def test_trial_purchase_cancels_g05_and_a_paid_purchase_succeeds_it():
    catalog = _catalog()
    profile = _profile(subscription_status="never", signup_at="2026-01-01T00:00:00.000Z")
    # Present never-paid viewer with recent opens and a completed episode: G05, not G02/G12/G07.
    events = [
        _event("open", "app_open", "2026-09-30T00:00:00.000Z"),
        _event("done", "episode_completed", "2026-09-20T00:00:00.000Z", {"title_id": "T-FILM", "episode_id": "x"}),
    ]
    first = resolve_goal(catalog, _snapshot(events, profile), None)
    assert first.goal is not None
    assert first.goal.goal_id == "G05"
    paid = events + [
        _event(
            "buy",
            "purchase_completed",
            "2026-10-01T13:00:00.000Z",
            {"plan_id": "P_MONTH", "price_inr": 149, "is_trial": False},
        )
    ]
    succeeded = resolve_goal(catalog, _snapshot(paid, profile, as_of="2026-10-01T13:00:00.000Z"), first.goal)
    assert succeeded.closed is not None
    assert succeeded.closed.status == "SUCCEEDED"
    assert succeeded.closed.evidence[-1]["ref"] == "event:buy"
    trial = events + [
        _event(
            "trial",
            "purchase_completed",
            "2026-10-01T13:00:00.000Z",
            {"plan_id": "P_TRIAL", "price_inr": 1, "is_trial": True},
        )
    ]
    cancelled = resolve_goal(catalog, _snapshot(trial, profile, as_of="2026-10-01T13:00:00.000Z"), first.goal)
    assert cancelled.closed is not None
    assert cancelled.closed.status == "CANCELLED"


def test_active_goal_is_not_replaced_by_a_higher_ranked_goal():
    catalog = _catalog()
    # No profile and no narrow subscription state, so the residual goal is G02.
    events = [_event("open", "app_open", "2026-09-30T00:00:00.000Z")]
    first = resolve_goal(catalog, _snapshot(events, None), None)
    assert first.goal.goal_id == "G02"
    # A series play that would make G07 eligible must not replace the open G02.
    later_events = events + [
        _event("p1", "play_started", "2026-09-20T00:00:00.000Z", {"title_id": "T-SHOW", "episode_id": "serial-d1-s1e1"}),
    ]
    # The play is before as_of, so it was "missed" then arrives as a late event. Premise of G02
    # (no completion) still holds, so G02 stays even though G07 is now eligible.
    second = resolve_goal(catalog, _snapshot(later_events, None, as_of="2026-10-01T15:00:00.000Z"), first.goal)
    assert second.goal.goal_id == "G02"
    assert second.goal.started_at == first.goal.started_at
    assert second.closed is None


def test_g03_correction_retargets_only_open_g03_goals():
    catalog = _catalog()
    profile = _profile(signup_at="2026-09-30T18:00:00.000Z", subscription_status="lapsed")
    started = resolve_goal(catalog, _snapshot([], profile, as_of="2026-10-01T00:00:00.000Z"), None)
    # Lapsed outranks G03. Build the G03 goal directly so the correction test is about retargeting.
    g03 = ActiveGoal(
        goal_id="G03",
        name="second_day_return_2d",
        rule_version=catalog.rule_version("G03"),
        started_at="2026-10-01T02:00:00.000Z",
        deadline="2026-10-03T02:00:00.000Z",
        success_event="app_open",
        success_predicate="on a later IST calendar day than the goal start",
        success_params={},
        status="ACTIVE",
        reason="signed up within 2 days",
        confidence=0.8,
        evidence=[],
        assumptions=[],
    )
    g01 = ActiveGoal(
        goal_id="G01",
        name="first_play_24h",
        rule_version="2026-09-30",
        started_at="2026-10-01T02:00:00.000Z",
        deadline="2026-10-02T02:00:00.000Z",
        success_event="play_started",
        success_predicate="any title",
        success_params={},
        status="ACTIVE",
        reason="new",
        confidence=0.8,
        evidence=[],
        assumptions=[],
    )
    closed = ActiveGoal(**{**g03.to_dict(), "status": "SUCCEEDED", "closed_reason": None})
    control = {
        "type": "goal_rule_corrected",
        "goal_id": "G03",
        "at": "2026-10-01T08:30:00.000Z",
        "new_success": {"event": "play_started", "condition": "on a later IST calendar day within the window"},
    }
    assert catalog.apply_control(control) == "G03"
    snap = _snapshot([], profile, as_of="2026-10-01T09:00:00.000Z")
    revised = retarget_open_goal(g03, snap, catalog)
    untouched = retarget_open_goal(g01, snap, catalog)
    still_closed = retarget_open_goal(closed, snap, catalog)
    assert revised.success_event == "play_started"
    assert revised.rule_version != "2026-09-30"
    assert revised.status == "ACTIVE"
    assert untouched.success_event == "play_started"
    assert untouched.rule_version == "2026-09-30"
    assert still_closed.status == "SUCCEEDED"
    assert still_closed.rule_version == "2026-09-30"
    # Same IST day does not succeed. The next IST day does.
    same_day = _snapshot(
        [_event("play-same", "play_started", "2026-10-01T03:00:00.000Z", {"title_id": "T-FILM"})],
        profile,
        as_of="2026-10-01T09:00:00.000Z",
    )
    assert retarget_open_goal(g03, same_day, catalog).status == "ACTIVE"
    next_day = _snapshot(
        [_event("play-next", "play_started", "2026-10-01T20:00:00.000Z", {"title_id": "T-FILM"})],
        profile,
        as_of="2026-10-01T21:00:00.000Z",
    )
    succeeded = retarget_open_goal(g03, next_day, catalog)
    assert succeeded.status == "SUCCEEDED"
    assert started.goal is None or started.goal.goal_id != "G11"


def test_deadline_expiry_allows_one_new_goal():
    catalog = _catalog()
    events = [_event("open", "app_open", "2026-09-30T00:00:00.000Z")]
    first = resolve_goal(catalog, _snapshot(events, None, as_of="2026-10-01T00:00:00.000Z"), None)
    assert first.goal.goal_id == "G02"
    later = resolve_goal(catalog, _snapshot(events, None, as_of="2026-10-05T00:00:00.000Z"), first.goal)
    assert later.closed.status == "EXPIRED"
    assert later.goal is not None
    assert later.goal.status == "ACTIVE"
    assert later.goal.started_at != first.goal.started_at


def test_goal_record_round_trips_through_sqlite(tmp_path):
    catalog = _catalog()
    profile = _profile(subscription_status="trial_active")
    resolution = resolve_goal(catalog, _snapshot([], profile), None)
    assert resolution.goal.goal_id == "G04"
    store = StateStore(tmp_path / "state.sqlite")
    store.upsert_goal(profile["user_id"], resolution.goal.to_dict())
    store.append_goal_history("h1", profile["user_id"], resolution.goal.to_dict())
    store.conn.commit()
    loaded = store.get_goal(profile["user_id"])
    assert loaded["deadline"] == resolution.goal.deadline
    assert loaded["success_predicate"]
    assert loaded["evidence"] == [] or isinstance(loaded["evidence"], list)
    assert store.goal_history(profile["user_id"])[0]["goal_id"] == "G04"
    store.close()


def test_film_play_does_not_qualify_for_resume():
    catalog = _catalog()
    events = [_event("p", "play_started", "2026-09-01T00:00:00.000Z", {"title_id": "T-FILM", "episode_id": "film-e1"})]
    snap = _snapshot(events, _profile())
    assert not evaluate_goal(catalog, "G07", snap).eligible
