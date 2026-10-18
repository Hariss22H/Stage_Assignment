"""Deterministic replay harness: triggers, live events, controls, and model faults."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from src.agent import ViewerAgent
from src.goals import GoalCatalog, retarget_open_goal, ActiveGoal
from src.llm.mock import MockProvider
from src.llm.openai_provider import OpenAIProvider
from src.pack_index import PackIndex
from src.state_store import StateStore
from src.time_utils import format_utc, parse_utc


@dataclass
class ReplayResult:
    decisions: list[dict] = field(default_factory=list)
    actions: list[dict] = field(default_factory=list)
    latency_ms: list[int] = field(default_factory=list)
    controls_applied: list[dict] = field(default_factory=list)
    duplicates: int = 0
    faults_seen: int = 0


def build_timeline(pack: PackIndex) -> list[tuple[datetime, int, str, dict]]:
    """Order by delivery time, then kind priority, then stable source order.

    Kind priority: control (0), live (1), trigger (2). A control at the same
    timestamp must apply before a decision that uses the catalogue.
    """
    items: list[tuple[datetime, int, int, str, dict]] = []
    for index, control in enumerate(pack.controls):
        at = parse_utc(control.get("at"))
        if at is None:
            continue
        items.append((at, 0, index, "control", control))
    for index, event in enumerate(pack.live_events):
        at = parse_utc(event.get("deliver_at") or event.get("received_at"))
        if at is None:
            continue
        items.append((at, 1, index, "live", event))
    for index, trigger in enumerate(pack.triggers):
        at = parse_utc(trigger.get("deliver_at") or trigger.get("received_at"))
        if at is None:
            continue
        items.append((at, 2, index, "trigger", trigger))
    items.sort(key=lambda row: (row[0], row[1], row[2]))
    return [(when, kind_rank, kind, payload) for when, kind_rank, _index, kind, payload in items]


def run_replay(
    pack_path: str | Path,
    out_dir: str | Path,
    *,
    model: str = "mock",
    reset: bool = True,
) -> ReplayResult:
    pack = PackIndex(pack_path)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    state_path = out / "viewer_state" / "state.sqlite"
    store = StateStore(state_path)
    if reset:
        store.reset()
    catalog = GoalCatalog(pack.goals_doc, pack.observed_event_types)
    if model == "openai":
        provider = OpenAIProvider()
    else:
        provider = MockProvider(pack.model_faults)
    decision_seq = [0]
    action_seq = [0]
    agent = ViewerAgent(
        pack=pack,
        catalog=catalog,
        store=store,
        provider=provider,
        decision_seq=decision_seq,
        action_seq=action_seq,
    )
    result = ReplayResult()
    dependency = {"model_provider_until": None, "store_degraded_until": None}
    timeline = build_timeline(pack)

    for when, _rank, kind, payload in timeline:
        if kind == "control":
            _apply_control(payload, pack, catalog, store, dependency, result)
            continue
        if kind == "live":
            agent.ingest_live_event(payload)
            continue
        # trigger
        if dependency["model_provider_until"] is not None and when < dependency["model_provider_until"]:
            # Provider outage: force deterministic path by using an empty fault map clone.
            agent.provider = MockProvider({})
            outage = True
        else:
            agent.provider = provider
            outage = False
        wall_start = time.perf_counter()
        # Degraded store: do not sleep; record a constant synthetic penalty of 50ms.
        if dependency["store_degraded_until"] is not None and when < dependency["store_degraded_until"]:
            wall_start -= 0.05
        outcome = agent.handle_trigger(trigger=payload, wall_start=wall_start, wall_now=time.perf_counter)
        decision = outcome["decision"]
        if outage:
            decision["fallback_used"] = True
            decision["model_fault"] = decision.get("model_fault") or "dependency_unavailable"
        if outcome.get("duplicate"):
            result.duplicates += 1
            continue
        result.decisions.append(decision)
        result.latency_ms.append(int(decision.get("decided_in_ms") or 0))
        if decision.get("model_fault"):
            result.faults_seen += 1
        if outcome.get("action"):
            result.actions.append(outcome["action"])

    # Rebuild from SQLite so mid-replay cancellations are excluded from the simulator input.
    result.actions = store.export_actions()
    _write_jsonl(out / "decisions.jsonl", result.decisions)
    _write_jsonl(out / "actions.jsonl", result.actions)
    latency = _latency_report(result.latency_ms)
    (out / "latency_report.json").write_text(
        __import__("json").dumps(latency, indent=2) + "\n",
        encoding="utf-8",
    )
    store.close()
    return result


def _apply_control(control, pack, catalog, store, dependency, result) -> None:
    kind = control.get("type")
    applied = {"type": kind, "at": control.get("at")}
    if kind == "title_withdrawn":
        title_id = control.get("title_id")
        at = control.get("at")
        if title_id in pack.catalogue and at:
            pack.catalogue[title_id] = dict(pack.catalogue[title_id])
            pack.catalogue[title_id]["available_until"] = at
            applied["title_id"] = title_id
            cancelled = store.cancel_pending_with_title(
                title_id,
                as_of=at,
                reason=f"title_withdrawn:{title_id}",
            )
            applied["cancelled_pending_actions"] = cancelled
            store.conn.commit()
    elif kind == "goal_rule_corrected":
        goal_id = catalog.apply_control(control)
        applied["goal_id"] = goal_id
        # Re-judge open goals of this id only.
        rows = store.conn.execute("SELECT viewer_id, payload_json FROM active_goals").fetchall()
        import json

        for row in rows:
            goal = ActiveGoal(**json.loads(row["payload_json"]))
            if goal.goal_id != goal_id:
                continue
            events = pack.events_for(row["viewer_id"])
            from src.goals import Snapshot

            snapshot = Snapshot(
                viewer_id=row["viewer_id"],
                as_of=control.get("at"),
                events=events,
                profile=pack.profiles.get(row["viewer_id"]),
                catalogue=pack.catalogue,
                episodes=pack.episodes,
            )
            revised = retarget_open_goal(goal, snapshot, catalog)
            if revised.status != "ACTIVE":
                store.append_goal_history(
                    f"{row['viewer_id']}:{revised.goal_id}:{revised.started_at}:corrected",
                    row["viewer_id"],
                    revised.to_dict(),
                )
                store.delete_goal(row["viewer_id"])
            else:
                store.upsert_goal(row["viewer_id"], revised.to_dict())
        store.conn.commit()
    elif kind == "dependency_unavailable":
        until = parse_utc(control.get("until"))
        if control.get("dependency") == "model_provider":
            dependency["model_provider_until"] = until
            applied["until"] = control.get("until")
    elif kind == "dependency_degraded":
        until = parse_utc(control.get("until"))
        if control.get("dependency") == "user_data_store":
            dependency["store_degraded_until"] = until
            applied["until"] = control.get("until")
    result.controls_applied.append(applied)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    import json

    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")


def _latency_report(values: list[int]) -> dict:
    if not values:
        return {
            "count": 0,
            "p50": None,
            "p95": None,
            "p99": None,
            "max": None,
            "over_60s": 0,
            "over_5m": 0,
            "method": "wall-clock processing time per trigger; published_at = deliver_at + decided_in_ms",
        }
    ordered = sorted(values)

    def pct(p):
        index = min(len(ordered) - 1, int(round((p / 100) * (len(ordered) - 1))))
        return ordered[index]

    return {
        "count": len(values),
        "p50": pct(50),
        "p95": pct(95),
        "p99": pct(99),
        "max": max(values),
        "over_60s": sum(1 for value in values if value > 60_000),
        "over_5m": sum(1 for value in values if value > 300_000),
        "method": "wall-clock processing time per trigger; published_at = deliver_at + decided_in_ms. Injected timeouts do not sleep.",
    }
