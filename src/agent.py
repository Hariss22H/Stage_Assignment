"""Per-viewer decide → verify → publish orchestration."""

from __future__ import annotations

from datetime import datetime, timedelta

from src.facts import resolve_request_dialect
from src.goals import Snapshot, resolve_goal
from src.interventions import plan_action
from src.llm.base import DecisionContext, ModelProvider
from src.time_utils import format_utc, is_offline_buffered, parse_utc
from src.verifier import verify_proposal

LATENCY_FAIL_SECONDS = 300


class ViewerAgent:
    def __init__(
        self,
        *,
        pack,
        catalog,
        store,
        provider: ModelProvider,
        decision_seq: list[int],
        action_seq: list[int],
    ):
        self.pack = pack
        self.catalog = catalog
        self.store = store
        self.provider = provider
        self.decision_seq = decision_seq
        self.action_seq = action_seq

    def handle_trigger(self, trigger: dict, *, wall_start: float, wall_now) -> dict:
        viewer_id = trigger["user_id"]
        deliver_at = parse_utc(trigger.get("deliver_at") or trigger.get("received_at"))
        assert deliver_at is not None
        occurred = parse_utc(trigger.get("occurred_at"))
        received = parse_utc(trigger.get("received_at"))
        stale = False
        stale_reason = None
        if occurred is not None and received is not None:
            stale = is_offline_buffered(occurred, received)
            if stale:
                stale_reason = "received_at - occurred_at exceeds offline-buffer threshold"
        as_of = format_utc(deliver_at)
        events = self._events(viewer_id)
        # Include the trigger itself for dialect and as history after the decision.
        if not any(event.get("event_id") == trigger.get("event_id") for event in events):
            events = events + [trigger]
        snapshot = Snapshot(
            viewer_id=viewer_id,
            as_of=as_of,
            events=events,
            profile=self.pack.profiles.get(viewer_id),
            catalogue=self.pack.catalogue,
            episodes=self.pack.episodes,
            trigger_event_id=trigger.get("event_id"),
        )
        active = self.store.get_goal(viewer_id)
        from src.goals import ActiveGoal

        active_goal = ActiveGoal(**active) if active else None
        resolution = resolve_goal(self.catalog, snapshot, active_goal)
        if resolution.closed is not None:
            self.store.append_goal_history(
                f"{viewer_id}:{resolution.closed.goal_id}:{resolution.closed.started_at}:closed",
                viewer_id,
                resolution.closed.to_dict(),
            )
        if resolution.goal is not None:
            self.store.upsert_goal(viewer_id, resolution.goal.to_dict())
        else:
            self.store.delete_goal(viewer_id)
        self.store.conn.commit()

        eligible_ids = [item["goal_id"] for item in resolution.considered if item.get("eligible")]
        model = self.provider.propose(
            DecisionContext(
                trigger_event_id=trigger["event_id"],
                viewer_id=viewer_id,
                eligible_goal_ids=eligible_ids,
            )
        )
        model_calls = 1
        model_goal_id = None
        if model.claim and isinstance(model.claim, dict):
            model_goal_id = model.claim.get("goal_id")

        arm = self.pack.arms.get(viewer_id, "agent")
        prior_actions = self.store.actions_for_user(viewer_id)
        plan = plan_action(
            trigger=trigger,
            snapshot=snapshot,
            goal=resolution.goal,
            arm=arm,
            catalogue=self.pack.catalogue,
            interventions=self.pack.interventions,
            allowed_prices=self.pack.allowed_prices,
            prior_actions=prior_actions,
            at=deliver_at,
            stale=stale,
        )
        decided_ms = max(0, int((wall_now() - wall_start) * 1000))
        published_at = deliver_at + timedelta(milliseconds=decided_ms)
        proposal = None if plan.action == "NO_ACTION" else plan.payload
        checks = verify_proposal(
            proposal,
            trigger=trigger,
            arm=arm,
            snapshot=snapshot,
            catalog=self.catalog,
            goal=resolution.goal,
            catalogue=self.pack.catalogue,
            interventions=self.pack.interventions,
            allowed_prices=set(self.pack.allowed_prices.values()),
            prior_actions=prior_actions,
            published_at=published_at,
            stale=stale,
            model_calls=model_calls,
            model_goal_id=model_goal_id,
        )
        # Reject unsupported model claims without voiding a verified deterministic plan.
        if (
            model.fault == "unsupported_claim"
            and model_goal_id
            and (resolution.goal is None or model_goal_id != resolution.goal.goal_id)
            and "model_goal_claim" not in checks.warnings
        ):
            checks.warnings.append("model_goal_claim")
        publish_action = proposal
        action_name = plan.action
        no_action_reason = plan.no_action_reason
        status = "PUBLISHED"
        fallback_used = bool(model.fallback_used) or bool(checks.warnings)
        if not checks.ok or publish_action is None:
            publish_action = None
            action_name = "NO_ACTION"
            if not checks.ok:
                status = "REJECTED"
                no_action_reason = no_action_reason or f"verifier failed: {', '.join(checks.failed)}"
                fallback_used = True
            else:
                status = "NO_ACTION"
                if no_action_reason is None:
                    no_action_reason = plan.reason

        dialect_fact, dialect_conflicts = resolve_request_dialect(trigger, snapshot.profile, as_of)
        self.decision_seq[0] += 1
        decision_id = f"D-{self.decision_seq[0]:06d}"
        payload = {
            "decision_id": decision_id,
            "viewer_id": viewer_id,
            "trigger": {
                "event": trigger.get("event"),
                "event_id": trigger.get("event_id"),
                "received_at": trigger.get("received_at"),
                "deliver_at": trigger.get("deliver_at"),
                "event_age_s": round((received - occurred).total_seconds(), 3)
                if occurred is not None and received is not None
                else None,
                "stale": stale,
                "stale_reason": stale_reason,
            },
            "goal": None if resolution.goal is None else resolution.goal.to_dict(),
            "action": action_name,
            "no_action_reason": no_action_reason,
            "dialect": {
                "used": dialect_fact.value,
                "source": dialect_fact.source,
                "conflicts": [item.to_dict() for item in dialect_conflicts],
                "resolution_reason": dialect_conflicts[0].resolution_reason
                if dialect_conflicts
                else "Trigger app dialect is the current app setting.",
            },
            "content": _content_refs(publish_action),
            "confidence": 0.0 if resolution.goal is None else resolution.goal.confidence,
            "confidence_reason": plan.reason
            if resolution.goal is None
            else resolution.goal.reason,
            "evidence": [] if resolution.goal is None else resolution.goal.evidence,
            "assumptions": [] if resolution.goal is None else resolution.goal.assumptions,
            "conflicts": [item.to_dict() for item in dialect_conflicts],
            "checks": checks.to_dict(),
            "decided_in_ms": decided_ms,
            "model_calls": model_calls,
            "tool_calls": 0,
            "fallback_used": fallback_used,
            "model_fault": model.fault,
            "rule_version": self.catalog.base_version,
            "status": status,
            "arm": arm,
        }
        action_record = None
        action_payload = None
        if publish_action is not None and checks.ok:
            self.action_seq[0] += 1
            action_id = f"A-{self.action_seq[0]:06d}"
            action_payload = {
                "action_id": action_id,
                "decision_id": decision_id,
                "user_id": viewer_id,
                "trigger_event_id": trigger["event_id"],
                "type": publish_action["type"],
                "published_at": format_utc(published_at),
            }
            for key, value in publish_action.items():
                if key == "type":
                    continue
                action_payload[key] = value
            action_record = {
                "action_id": action_id,
                "decision_id": decision_id,
                "trigger_event_id": trigger["event_id"],
                "user_id": viewer_id,
                "type": publish_action["type"],
                "published_at": format_utc(published_at),
                "payload": action_payload,
            }
        published = self.store.publish(
            {
                "decision_id": decision_id,
                "viewer_id": viewer_id,
                "trigger_event_id": trigger["event_id"],
                "received_at": trigger.get("received_at"),
                "payload": payload,
            },
            action_record,
        )
        if not published:
            payload["status"] = "DUPLICATE"
            payload["no_action_reason"] = "duplicate trigger_event_id; no second publish"
            payload["action"] = "NO_ACTION"
            return {"decision": payload, "action": None, "duplicate": True}
        self.store.save_viewer_state(
            viewer_id,
            {
                "facts": snapshot.facts.to_dict(),
                "goal": None if resolution.goal is None else resolution.goal.to_dict(),
                "last_trigger": trigger.get("event_id"),
            },
            as_of,
        )
        self.store.conn.commit()
        return {"decision": payload, "action": action_payload, "duplicate": False}

    def ingest_live_event(self, event: dict) -> None:
        viewer_id = event["user_id"]
        as_of = event.get("deliver_at") or event.get("received_at")
        if self.store.event_processed(event["event_id"]):
            return
        events = self._events(viewer_id)
        if not any(item.get("event_id") == event.get("event_id") for item in events):
            events = events + [event]
            self.pack.events_by_user.setdefault(viewer_id, []).append(event)
        snapshot = Snapshot(
            viewer_id=viewer_id,
            as_of=as_of,
            events=events,
            profile=self.pack.profiles.get(viewer_id),
            catalogue=self.pack.catalogue,
            episodes=self.pack.episodes,
        )
        self._cancel_pending_for_event(event, as_of)
        active = self.store.get_goal(viewer_id)
        if active is None:
            self.store.mark_event_processed(event["event_id"], event.get("received_at"), viewer_id)
            self.store.conn.commit()
            return
        from src.goals import ActiveGoal, refresh_goal

        goal = ActiveGoal(**active)
        updated = refresh_goal(goal, snapshot, self.catalog)
        if updated.status != "ACTIVE":
            self.store.append_goal_history(
                f"{viewer_id}:{updated.goal_id}:{updated.started_at}:{updated.status}",
                viewer_id,
                updated.to_dict(),
            )
            self.store.delete_goal(viewer_id)
            # Goal premise failed: cancel any still-scheduled messages for this viewer.
            self.store.cancel_pending_actions(
                viewer_id,
                as_of=as_of,
                reason=f"goal {updated.goal_id} closed as {updated.status}",
            )
        else:
            self.store.upsert_goal(viewer_id, updated.to_dict())
        self.store.mark_event_processed(event["event_id"], event.get("received_at"), viewer_id)
        self.store.conn.commit()

    def _cancel_pending_for_event(self, event: dict, as_of: str) -> None:
        viewer_id = event["user_id"]
        name = event.get("event")
        props = event.get("properties") or {}
        if name == "purchase_completed":
            self.store.cancel_pending_actions(
                viewer_id,
                as_of=as_of,
                reason="purchase_completed invalidates pending conversion messaging",
            )
        elif name == "notification_opt_out":
            channel = props.get("channel")
            if channel in {"push", "whatsapp"}:
                self.store.cancel_pending_actions(
                    viewer_id,
                    as_of=as_of,
                    reason=f"notification_opt_out for {channel}",
                    channel=channel,
                )

    def _events(self, viewer_id: str) -> list[dict]:
        return self.pack.events_for(viewer_id)


def _content_refs(action: dict | None) -> list[str]:
    if not action:
        return []
    refs = []
    if action.get("hero_title_id"):
        refs.append(f"title:{action['hero_title_id']}")
    if action.get("title_id"):
        refs.append(f"title:{action['title_id']}")
    for row in action.get("rows") or []:
        for title_id in row.get("title_ids") or []:
            refs.append(f"title:{title_id}")
    return refs
