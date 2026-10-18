"""Outcome predicates and ITT arithmetic (not hardcoded sample numbers)."""

from src.goals import ActiveGoal, GoalCatalog, Snapshot, success_evidence
from src.impact import pilot_itt, proportion_diff_ci, replay_itt
from src.config import load_goals
from pathlib import Path


def _goal(**overrides):
    base = {
        "goal_id": "G01",
        "name": "first_play_24h",
        "rule_version": "2026-09-30",
        "started_at": "2026-10-01T12:00:00.000Z",
        "deadline": "2026-10-02T12:00:00.000Z",
        "success_event": "play_started",
        "success_predicate": "any title",
        "success_params": {},
        "status": "ACTIVE",
        "reason": "test",
        "confidence": 0.8,
        "evidence": [],
        "assumptions": [],
    }
    base.update(overrides)
    return ActiveGoal(**base)


def test_success_evidence_respects_window_and_predicate():
    catalogue = {
        "T1": {"title_id": "T1", "content_slug": "s1", "content_type": "show", "release_date": "2026-09-01"}
    }
    inside = {
        "event_id": "e1",
        "user_id": "U-1",
        "event": "play_started",
        "occurred_at": "2026-10-01T13:00:00.000Z",
        "sent_at": "2026-10-01T13:00:00.000Z",
        "received_at": "2026-10-01T13:00:00.000Z",
        "properties": {"title_id": "T1"},
    }
    outside = {
        **inside,
        "event_id": "e2",
        "occurred_at": "2026-10-03T13:00:00.000Z",
        "sent_at": "2026-10-03T13:00:00.000Z",
        "received_at": "2026-10-03T13:00:00.000Z",
    }
    snap = Snapshot("U-1", "2026-10-05T00:00:00.000Z", [outside], None, catalogue, {})
    assert success_evidence(_goal(), snap) == []
    snap_ok = Snapshot("U-1", "2026-10-05T00:00:00.000Z", [inside], None, catalogue, {})
    assert success_evidence(_goal(), snap_ok)


def test_g07_requires_same_content_slug():
    catalogue = {
        "T1": {"title_id": "T1", "content_slug": "serial-a", "content_type": "show", "release_date": "2026-01-01"},
        "T2": {"title_id": "T2", "content_slug": "serial-b", "content_type": "show", "release_date": "2026-01-01"},
    }
    goal = _goal(
        goal_id="G07",
        success_event="play_started",
        success_predicate="same content_slug as the unfinished series",
        success_params={"content_slug": "serial-a"},
        deadline="2026-10-04T12:00:00.000Z",
    )
    wrong = {
        "event_id": "e1",
        "user_id": "U-1",
        "event": "play_started",
        "occurred_at": "2026-10-01T13:00:00.000Z",
        "sent_at": "2026-10-01T13:00:00.000Z",
        "received_at": "2026-10-01T13:00:00.000Z",
        "properties": {"title_id": "T2"},
    }
    right = {**wrong, "event_id": "e2", "properties": {"title_id": "T1"}}
    snap = Snapshot("U-1", "2026-10-05T00:00:00.000Z", [wrong, right], None, catalogue, {"serial-a": ["e1"]})
    evidence = success_evidence(goal, snap)
    assert evidence and evidence[0]["ref"] == "event:e2"


def test_replay_itt_uses_arm_denominators_not_served_only():
    arms = {f"U-{i:02d}": ("agent" if i % 2 == 0 else "holdout") for i in range(10)}
    # Only two successes, both agent; holdout has none. Denominator remains 5/5.
    outcomes = [
        {"viewer_id": "U-00", "status": "SUCCEEDED"},
        {"viewer_id": "U-02", "status": "FAILED"},
        {"viewer_id": "U-01", "status": "FAILED"},
    ]
    decisions = [
        {"viewer_id": "U-00", "goal": {"goal_id": "G01"}, "action": "HOME_PAGE", "arm": "agent"},
        {"viewer_id": "U-01", "goal": {"goal_id": "G01"}, "action": "NO_ACTION", "arm": "holdout"},
    ]
    report = replay_itt(arms, outcomes, decisions)
    assert report["agent"]["users"] == 5
    assert report["holdout"]["users"] == 5
    assert report["agent"]["successes"] == 1
    assert report["holdout"]["successes"] == 0
    assert report["holdout"]["with_action"] == 0
    assert abs(report["absolute_uplift_pp"] - 20.0) < 1e-9


def test_pilot_itt_reproduces_selection_bias_contrast_without_hardcoding_uplift():
    assignment = [
        {"user_id": "U1", "arm": "treatment", "served": "true"},
        {"user_id": "U2", "arm": "treatment", "served": "false"},
        {"user_id": "U3", "arm": "holdout", "served": "false"},
        {"user_id": "U4", "arm": "holdout", "served": "false"},
    ]
    outcomes = [
        {"user_id": "U1", "arm": "treatment", "served": "true", "succeeded": "true"},
        {"user_id": "U2", "arm": "treatment", "served": "false", "succeeded": "false"},
        {"user_id": "U3", "arm": "holdout", "served": "false", "succeeded": "true"},
        {"user_id": "U4", "arm": "holdout", "served": "false", "succeeded": "false"},
    ]
    report = pilot_itt(assignment, outcomes)
    assert report["treatment"]["users"] == 2
    assert report["treatment"]["successes"] == 1
    assert report["holdout"]["users"] == 2
    assert report["holdout"]["successes"] == 1
    assert report["absolute_uplift_pp"] == 0.0
    biased = report["biased_served_vs_others"]
    assert biased["served_users"] == 1
    assert biased["served_rate"] == 1.0
    assert abs(biased["absolute_uplift_pp"] - (100.0 - 100 / 3)) < 1e-6


def test_proportion_ci_includes_zero_when_counts_equal():
    ci = proportion_diff_ci(10, 100, 10, 100)
    assert ci["diff"] == 0.0
    assert ci["low"] < 0 < ci["high"]
