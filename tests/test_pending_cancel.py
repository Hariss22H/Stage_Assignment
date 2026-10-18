"""Pending message cancellation when premise becomes false."""

from src.state_store import StateStore


def _scheduled_push(action_id="A-1", title_id="T1", send_at="2026-10-01T10:00:00.000Z"):
    return {
        "action_id": action_id,
        "decision_id": "D-1",
        "trigger_event_id": f"trig-{action_id}",
        "user_id": "U-1",
        "type": "push",
        "published_at": "2026-10-01T06:00:00.000Z",
        "payload": {
            "action_id": action_id,
            "decision_id": "D-1",
            "user_id": "U-1",
            "trigger_event_id": f"trig-{action_id}",
            "type": "push",
            "published_at": "2026-10-01T06:00:00.000Z",
            "send_at": send_at,
            "title_id": title_id,
            "text": {"title": "Hello", "body": "Watch this."},
            "deeplink": f"ott://title/{title_id}",
        },
    }


def test_purchase_cancels_future_push_but_not_already_due(tmp_path):
    store = StateStore(tmp_path / "state.sqlite")
    future = _scheduled_push("A-1", send_at="2026-10-01T10:00:00.000Z")
    past = _scheduled_push("A-2", send_at="2026-10-01T05:00:00.000Z")
    store.publish(
        {
            "decision_id": "D-1",
            "viewer_id": "U-1",
            "trigger_event_id": "trig-A-1",
            "payload": {},
        },
        future,
    )
    store.publish(
        {
            "decision_id": "D-2",
            "viewer_id": "U-1",
            "trigger_event_id": "trig-A-2",
            "payload": {},
        },
        past,
    )
    cancelled = store.cancel_pending_actions(
        "U-1",
        as_of="2026-10-01T06:30:00.000Z",
        reason="purchase_completed",
    )
    assert cancelled == ["A-1"]
    exported = store.export_actions()
    assert [a["action_id"] for a in exported] == ["A-2"]
    remaining = store.actions_for_user("U-1", include_cancelled=True)
    assert any(a["action_id"] == "A-1" and a.get("cancelled") for a in remaining)
    store.close()


def test_opt_out_cancels_only_matching_channel(tmp_path):
    store = StateStore(tmp_path / "state.sqlite")
    push = _scheduled_push("A-push")
    wa = _scheduled_push("A-wa")
    wa["type"] = "whatsapp"
    wa["payload"]["type"] = "whatsapp"
    wa["payload"].pop("deeplink", None)
    wa["payload"]["template_id"] = "wa_recommend_title_v1"
    store.publish(
        {"decision_id": "D-1", "viewer_id": "U-1", "trigger_event_id": "t1", "payload": {}},
        push,
    )
    store.publish(
        {"decision_id": "D-2", "viewer_id": "U-1", "trigger_event_id": "t2", "payload": {}},
        wa,
    )
    cancelled = store.cancel_pending_actions(
        "U-1",
        as_of="2026-10-01T06:30:00.000Z",
        reason="opt_out",
        channel="push",
    )
    assert cancelled == ["A-push"]
    assert [a["action_id"] for a in store.export_actions()] == ["A-wa"]
    store.close()


def test_title_withdrawal_cancels_pending_for_that_title(tmp_path):
    store = StateStore(tmp_path / "state.sqlite")
    store.publish(
        {"decision_id": "D-1", "viewer_id": "U-1", "trigger_event_id": "t1", "payload": {}},
        _scheduled_push("A-1", title_id="T0001"),
    )
    store.publish(
        {"decision_id": "D-2", "viewer_id": "U-2", "trigger_event_id": "t2", "payload": {}},
        _scheduled_push("A-2", title_id="T0002"),
    )
    cancelled = store.cancel_pending_with_title(
        "T0001",
        as_of="2026-10-01T07:00:00.000Z",
        reason="title_withdrawn",
    )
    assert cancelled == ["A-1"]
    assert [a["action_id"] for a in store.export_actions()] == ["A-2"]
    store.close()
