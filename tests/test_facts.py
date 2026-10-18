"""Source precedence: dialect, subscription, consent, missing profile."""

from src.facts import build_viewer_facts, is_opted_in, resolve_request_dialect

AS_OF = "2026-10-01T12:00:00.000Z"
PROFILE = {
    "user_id": "U-1",
    "signup_at": "2026-09-20T00:00:00.000Z",
    "subscription_status": "never",
    "preferred_dialect": "d1",
    "notifications_opt_in": "true",
    "whatsapp_opt_in": "false",
    "updated_at": "2026-09-29T00:00:00.000Z",
    "snapshot_at": "2026-09-30T00:00:00.000Z",
}


def _event(event_id, name, received, properties=None, dialect="d3"):
    return {
        "event_id": event_id,
        "user_id": "U-1",
        "event": name,
        "occurred_at": received,
        "sent_at": received,
        "received_at": received,
        "app_dialect": dialect,
        "properties": properties or {},
    }


def test_app_dialect_beats_profile_and_deeplink_and_keeps_the_conflict():
    trigger = _event(
        "trig-1",
        "app_open",
        "2026-10-01T12:00:00.000Z",
        {"deeplink": "ott://c/cmp036/d5/title/T0909"},
        dialect="d3",
    )
    fact, conflicts = resolve_request_dialect(trigger, PROFILE, AS_OF)
    assert fact.value == "d3"
    assert fact.source == "trigger.app_dialect"
    assert fact.confidence == 1.0
    assert len(conflicts) == 1
    sources = {item["source"] for item in conflicts[0].candidates}
    assert sources == {"trigger.app_dialect", "profile.preferred_dialect", "trigger.deeplink"}
    assert conflicts[0].chosen_value == "d3"
    assert "deeplink never overrides" in conflicts[0].resolution_reason


def test_invalid_dialect_is_not_treated_as_a_catalogue_dialect():
    trigger = _event("trig-2", "app_open", AS_OF, dialect="d7")
    fact, _conflicts = resolve_request_dialect(trigger, None, AS_OF)
    assert fact.value == "d7"
    assert fact.confidence < 1.0
    missing = _event("trig-3", "app_open", AS_OF, dialect=None)
    missing["app_dialect"] = None
    fact_missing, _ = resolve_request_dialect(missing, None, AS_OF)
    assert fact_missing.value is None


def test_newer_purchase_beats_stale_profile():
    events = [
        _event(
            "buy-1",
            "purchase_completed",
            "2026-09-30T10:00:00.000Z",
            {"plan_id": "P_MONTH", "price_inr": 149, "is_trial": False},
        )
    ]
    facts = build_viewer_facts("U-1", events, PROFILE, AS_OF)
    assert facts.facts["subscription_status"].value == "active"
    assert facts.facts["ever_paid"].value is True
    assert facts.conflicts[0].fact == "subscription_status"
    assert facts.conflicts[0].chosen_value == "active"


def test_later_opt_out_overrides_profile_consent():
    events = [_event("opt-1", "notification_opt_out", "2026-09-30T01:00:00.000Z", {"channel": "push"})]
    facts = build_viewer_facts("U-1", events, PROFILE, AS_OF)
    push = facts.facts["notifications_opt_in"]
    assert push.value is False
    assert is_opted_in(push) is False
    assert facts.facts["whatsapp_opt_in"].value is False
    assert any(conflict.fact == "notifications_opt_in" for conflict in facts.conflicts)


def test_missing_profile_is_unknown_consent_not_consent_and_still_builds_state():
    events = [_event("open-1", "app_open", "2026-10-01T11:00:00.000Z")]
    facts = build_viewer_facts("U-new", events, None, AS_OF)
    assert facts.facts["has_profile"].value is False
    assert facts.facts["subscription_status"].value == "unknown"
    assert facts.facts["notifications_opt_in"].value is None
    assert is_opted_in(facts.facts["notifications_opt_in"]) is False
    assert is_opted_in(facts.facts["whatsapp_opt_in"]) is False
    assert facts.facts["has_observed_play_started"].value is False


def test_no_events_and_no_profile_does_not_claim_the_user_never_played():
    facts = build_viewer_facts("U-empty", [], None, AS_OF)
    assert facts.facts["has_observed_play_started"].value == "unknown"
    assert facts.facts["signup_at"].value is None


def test_events_after_as_of_are_ignored():
    events = [
        _event(
            "buy-future",
            "purchase_completed",
            "2026-10-02T00:00:00.000Z",
            {"is_trial": False, "plan_id": "P_MONTH", "price_inr": 149},
        )
    ]
    facts = build_viewer_facts("U-1", events, PROFILE, AS_OF)
    assert facts.facts["subscription_status"].value == "never"
    assert facts.facts["ever_paid"].value is False
