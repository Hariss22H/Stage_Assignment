"""Idempotent SQLite publishing."""

from src.state_store import StateStore


def _decision(decision_id: str, trigger: str) -> dict:
    return {
        "decision_id": decision_id,
        "viewer_id": "U-1",
        "trigger_event_id": trigger,
        "received_at": "2026-10-01T12:00:00.000Z",
        "payload": {"decision_id": decision_id, "action": "NO_ACTION"},
    }


def _action(action_id: str, decision_id: str, trigger: str) -> dict:
    return {
        "action_id": action_id,
        "decision_id": decision_id,
        "trigger_event_id": trigger,
        "user_id": "U-1",
        "type": "home_page",
        "published_at": "2026-10-01T12:00:01.000Z",
        "payload": {"action_id": action_id, "type": "home_page"},
    }


def test_duplicate_trigger_publishes_one_decision_and_no_second_action(tmp_path):
    store = StateStore(tmp_path / "state.sqlite")
    first = store.publish(_decision("D-1", "evt-1"), _action("A-1", "D-1", "evt-1"))
    second = store.publish(_decision("D-2", "evt-1"), _action("A-2", "D-2", "evt-1"))
    assert first is True
    assert second is False
    assert store.decision_count() == 1
    assert store.action_count() == 1
    assert store.actions_for_user("U-1")[0]["action_id"] == "A-1"
    assert store.event_processed("evt-1")
    store.close()


def test_viewer_state_round_trip_and_reset(tmp_path):
    store = StateStore(tmp_path / "state.sqlite")
    store.save_viewer_state("U-9", {"facts": {"subscription_status": "never"}}, "2026-10-01T00:00:00.000Z")
    store.conn.commit()
    assert store.load_viewer_state("U-9")["facts"]["subscription_status"] == "never"
    store.upsert_goal(
        "U-9",
        {
            "goal_id": "G01",
            "started_at": "2026-10-01T00:00:00.000Z",
            "deadline": "2026-10-02T00:00:00.000Z",
            "rule_version": "2026-09-30",
            "status": "ACTIVE",
        },
    )
    store.conn.commit()
    assert store.get_goal("U-9")["goal_id"] == "G01"
    store.reset()
    assert store.load_viewer_state("U-9") is None
    assert store.get_goal("U-9") is None
    store.close()
