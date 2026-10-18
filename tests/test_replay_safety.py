"""Required idempotency, fault, and control tests for the replay path."""

import json
from pathlib import Path

from src.goals import ActiveGoal, GoalCatalog, Snapshot, retarget_open_goal
from src.llm.mock import MockProvider
from src.pack_index import PackIndex
from src.replay import build_timeline, run_replay
from src.state_store import StateStore
from src.config import load_goals


def test_mock_timeout_does_not_sleep_and_forces_fallback():
    provider = MockProvider(
        {
            "trig": {"fault": "timeout", "seconds": 45},
            "bad": {"fault": "malformed_output", "model_output": '{"goal_id": "G02", "eligible": tr'},
            "claim": {
                "fault": "unsupported_claim",
                "model_output": {"goal_id": "G04", "eligible": True, "confidence": 0.97},
            },
        }
    )
    from src.llm.base import DecisionContext
    import time

    start = time.perf_counter()
    timeout = provider.propose(DecisionContext("trig", "U-1"))
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0
    assert timeout.fault == "timeout"
    assert timeout.fallback_used
    assert provider.propose(DecisionContext("bad", "U-1")).fault == "malformed_output"
    claim = provider.propose(DecisionContext("claim", "U-1"))
    assert claim.fault == "unsupported_claim"
    assert claim.claim["goal_id"] == "G04"


def test_timeline_orders_control_before_trigger_at_same_instant():
    pack = PackIndex("one_user_one_agent_pack")
    timeline = build_timeline(pack)
    kinds = [kind for _when, _rank, kind, _payload in timeline]
    assert kinds.count("control") == 4
    assert kinds.count("trigger") == len(pack.triggers)
    # First title withdrawal happens before later triggers.
    withdrawn_at = next(when for when, _r, kind, payload in timeline if kind == "control" and payload["type"] == "title_withdrawn")
    later_triggers = [
        when
        for when, _r, kind, _payload in timeline
        if kind == "trigger" and when > withdrawn_at
    ]
    assert later_triggers


def test_corrected_goal_rule_only_retargets_open_matching_goals():
    catalog = GoalCatalog(load_goals(Path("one_user_one_agent_pack")), {"app_open", "play_started"})
    open_g03 = ActiveGoal(
        goal_id="G03",
        name="second_day_return_2d",
        rule_version="2026-09-30",
        started_at="2026-10-01T02:00:00.000Z",
        deadline="2026-10-03T02:00:00.000Z",
        success_event="app_open",
        success_predicate="on a later IST calendar day than the goal start",
        success_params={},
        status="ACTIVE",
        reason="test",
        confidence=0.8,
        evidence=[],
        assumptions=[],
    )
    closed_g03 = ActiveGoal(**{**open_g03.to_dict(), "status": "SUCCEEDED"})
    other = ActiveGoal(**{**open_g03.to_dict(), "goal_id": "G01", "name": "first_play_24h", "success_event": "play_started"})
    catalog.apply_control(
        {
            "type": "goal_rule_corrected",
            "goal_id": "G03",
            "at": "2026-10-01T08:30:00.000Z",
            "new_success": {"event": "play_started", "condition": "on a later IST calendar day within the window"},
        }
    )
    snap = Snapshot("U-1", "2026-10-01T09:00:00.000Z", [], None, {}, {})
    assert retarget_open_goal(open_g03, snap, catalog).success_event == "play_started"
    assert retarget_open_goal(closed_g03, snap, catalog).success_event == "app_open"
    assert retarget_open_goal(other, snap, catalog).goal_id == "G01"
    assert retarget_open_goal(other, snap, catalog).rule_version == "2026-09-30"


def test_duplicate_trigger_publish_is_idempotent(tmp_path):
    store = StateStore(tmp_path / "state.sqlite")
    decision = {
        "decision_id": "D-1",
        "viewer_id": "U-1",
        "trigger_event_id": "same",
        "received_at": "2026-10-01T12:00:00.000Z",
        "payload": {"decision_id": "D-1"},
    }
    action = {
        "action_id": "A-1",
        "decision_id": "D-1",
        "trigger_event_id": "same",
        "user_id": "U-1",
        "type": "home_page",
        "published_at": "2026-10-01T12:00:01.000Z",
        "payload": {"action_id": "A-1", "type": "home_page"},
    }
    assert store.publish(decision, action) is True
    assert (
        store.publish(
            {**decision, "decision_id": "D-2"},
            {**action, "action_id": "A-2", "decision_id": "D-2"},
        )
        is False
    )
    assert store.decision_count() == 1
    assert store.action_count() == 1
    store.close()


def test_mini_replay_writes_decisions_and_zero_holdout_actions(tmp_path):
    # Full pack replay is covered by the CLI; this smoke-tests wiring on a tiny out dir
    # by running the real harness once and asserting invariants on a sample.
    out = tmp_path / "run"
    result = run_replay("one_user_one_agent_pack", out, model="mock", reset=True)
    assert result.decisions
    assert (out / "decisions.jsonl").exists()
    assert (out / "actions.jsonl").exists()
    assert (out / "latency_report.json").exists()
    arms = PackIndex("one_user_one_agent_pack").arms
    for action in result.actions:
        assert arms[action["user_id"]] != "holdout"
        assert action["type"] in {"home_page", "push", "whatsapp", "in_app_message"}
        assert "published_at" in action
        assert "action_id" in action
        assert "decision_id" in action
        assert "trigger_event_id" in action
    # Duplicate trigger event ids must not create a second decision.
    trigger_ids = [d["trigger"]["event_id"] for d in result.decisions]
    assert len(trigger_ids) == len(set(trigger_ids))
    # Faults from the pack should surface without a 45s sleep.
    faults = {d.get("model_fault") for d in result.decisions if d.get("model_fault")}
    assert "timeout" in faults or "malformed_output" in faults or "unsupported_claim" in faults
    latency = json.loads((out / "latency_report.json").read_text(encoding="utf-8"))
    assert latency["over_5m"] == 0
    assert latency["max"] is None or latency["max"] < 60_000
