"""SQLite state for one logical agent per viewer.

Writes are transactional. A duplicate trigger event id cannot publish a second
decision or action. The database contains no secrets and no API keys.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS viewer_state (
    viewer_id TEXT PRIMARY KEY,
    state_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS processed_events (
    event_id TEXT PRIMARY KEY,
    received_at TEXT,
    user_id TEXT
);
CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    viewer_id TEXT NOT NULL,
    trigger_event_id TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actions (
    action_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL,
    trigger_event_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    type TEXT NOT NULL,
    published_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS active_goals (
    viewer_id TEXT PRIMARY KEY,
    goal_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    deadline TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS goal_outcomes (
    outcome_id TEXT PRIMARY KEY,
    viewer_id TEXT NOT NULL,
    goal_id TEXT NOT NULL,
    status TEXT NOT NULL,
    judged_at TEXT NOT NULL,
    evidence_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS goal_history (
    history_id TEXT PRIMARY KEY,
    viewer_id TEXT NOT NULL,
    goal_id TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    deadline TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
"""


class StateStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if self.path.parent and not self.path.parent.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def reset(self) -> None:
        """Drop decision state so a replay can be repeated from an empty store."""
        with self.transaction():
            for table in (
                "goal_history",
                "goal_outcomes",
                "active_goals",
                "actions",
                "decisions",
                "processed_events",
                "viewer_state",
            ):
                self.conn.execute(f"DELETE FROM {table}")

    @contextmanager
    def transaction(self) -> Iterator[None]:
        try:
            yield
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def mark_event_processed(self, event_id: str, received_at: str | None, user_id: str | None) -> bool:
        """Return False when this event_id was already processed."""
        cursor = self.conn.execute(
            "INSERT INTO processed_events (event_id, received_at, user_id) VALUES (?, ?, ?)",
            (event_id, received_at, user_id),
        )
        return cursor.rowcount == 1

    def event_processed(self, event_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM processed_events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        return row is not None

    def save_viewer_state(self, viewer_id: str, state: dict, updated_at: str) -> None:
        payload = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
        self.conn.execute(
            """
            INSERT INTO viewer_state (viewer_id, state_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(viewer_id) DO UPDATE SET
                state_json = excluded.state_json,
                updated_at = excluded.updated_at
            """,
            (viewer_id, payload, updated_at),
        )

    def load_viewer_state(self, viewer_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT state_json FROM viewer_state WHERE viewer_id = ?",
            (viewer_id,),
        ).fetchone()
        if row is None:
            return None
        return json.loads(row["state_json"])

    def publish(self, decision: dict, action: dict | None = None) -> bool:
        """Insert a decision and optional action. Duplicate trigger_event_id publishes nothing.

        The whole insert rolls back together, so a duplicate trigger cannot leave an action behind.
        """
        try:
            with self.transaction():
                self.conn.execute(
                    """
                    INSERT INTO decisions (decision_id, viewer_id, trigger_event_id, payload_json)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        decision["decision_id"],
                        decision["viewer_id"],
                        decision["trigger_event_id"],
                        json.dumps(decision["payload"], ensure_ascii=False, separators=(",", ":")),
                    ),
                )
                self.conn.execute(
                    "INSERT INTO processed_events (event_id, received_at, user_id) VALUES (?, ?, ?)",
                    (decision["trigger_event_id"], decision.get("received_at"), decision["viewer_id"]),
                )
                if action is not None:
                    self.conn.execute(
                        """
                        INSERT INTO actions (
                            action_id, decision_id, trigger_event_id, user_id, type, published_at, payload_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            action["action_id"],
                            action["decision_id"],
                            action["trigger_event_id"],
                            action["user_id"],
                            action["type"],
                            action["published_at"],
                            json.dumps(action["payload"], ensure_ascii=False, separators=(",", ":")),
                        ),
                    )
            return True
        except sqlite3.IntegrityError:
            return False

    def decision_count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0])

    def action_count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0])

    def actions_for_user(self, user_id: str, *, include_cancelled: bool = False) -> list[dict]:
        rows = self.conn.execute(
            "SELECT payload_json FROM actions WHERE user_id = ? ORDER BY action_id",
            (user_id,),
        ).fetchall()
        actions = [json.loads(row["payload_json"]) for row in rows]
        if include_cancelled:
            return actions
        return [action for action in actions if not action.get("cancelled")]

    def export_actions(self) -> list[dict]:
        """All non-cancelled actions in action_id order (simulator input)."""
        rows = self.conn.execute(
            "SELECT payload_json FROM actions ORDER BY action_id"
        ).fetchall()
        return [
            json.loads(row["payload_json"])
            for row in rows
            if not json.loads(row["payload_json"]).get("cancelled")
        ]

    def cancel_pending_actions(
        self,
        user_id: str,
        *,
        as_of: str,
        reason: str,
        channel: str | None = None,
        title_id: str | None = None,
    ) -> list[str]:
        """Cancel scheduled messages whose send_at is still in the future.

        Home pages are immediate and are left alone. Returns cancelled action ids.
        """
        from src.time_utils import parse_utc

        as_of_dt = parse_utc(as_of)
        if as_of_dt is None:
            return []
        rows = self.conn.execute(
            "SELECT action_id, payload_json FROM actions WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        cancelled_ids: list[str] = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            if payload.get("cancelled"):
                continue
            kind = payload.get("type")
            if kind not in {"push", "whatsapp", "in_app_message"}:
                continue
            send_at = parse_utc(payload.get("send_at") or payload.get("published_at"))
            if send_at is None or send_at <= as_of_dt:
                continue
            if channel is not None and kind != channel:
                continue
            if title_id is not None and payload.get("title_id") != title_id:
                continue
            payload["cancelled"] = True
            payload["cancel_reason"] = reason
            payload["cancelled_at"] = as_of
            self.conn.execute(
                "UPDATE actions SET payload_json = ? WHERE action_id = ?",
                (json.dumps(payload, ensure_ascii=False, separators=(",", ":")), row["action_id"]),
            )
            cancelled_ids.append(row["action_id"])
        return cancelled_ids

    def cancel_pending_with_title(self, title_id: str, *, as_of: str, reason: str) -> list[str]:
        """Cancel any pending message that references a withdrawn title."""
        rows = self.conn.execute("SELECT DISTINCT user_id FROM actions").fetchall()
        cancelled: list[str] = []
        for row in rows:
            cancelled.extend(
                self.cancel_pending_actions(
                    row["user_id"],
                    as_of=as_of,
                    reason=reason,
                    title_id=title_id,
                )
            )
        return cancelled

    def upsert_goal(self, viewer_id: str, goal: dict) -> None:
        self.conn.execute(
            """
            INSERT INTO active_goals (
                viewer_id, goal_id, started_at, deadline, rule_version, status, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(viewer_id) DO UPDATE SET
                goal_id = excluded.goal_id,
                started_at = excluded.started_at,
                deadline = excluded.deadline,
                rule_version = excluded.rule_version,
                status = excluded.status,
                payload_json = excluded.payload_json
            """,
            (
                viewer_id,
                goal["goal_id"],
                goal["started_at"],
                goal["deadline"],
                goal["rule_version"],
                goal["status"],
                json.dumps(goal, ensure_ascii=False, separators=(",", ":")),
            ),
        )

    def delete_goal(self, viewer_id: str) -> None:
        self.conn.execute("DELETE FROM active_goals WHERE viewer_id = ?", (viewer_id,))

    def append_goal_history(self, history_id: str, viewer_id: str, goal: dict) -> None:
        """Keep a closed or corrected goal. A second write of the same id is ignored."""
        self.conn.execute(
            """
            INSERT OR IGNORE INTO goal_history (
                history_id, viewer_id, goal_id, status, started_at, deadline, rule_version, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                history_id,
                viewer_id,
                goal["goal_id"],
                goal["status"],
                goal["started_at"],
                goal["deadline"],
                goal["rule_version"],
                json.dumps(goal, ensure_ascii=False, separators=(",", ":")),
            ),
        )

    def goal_history(self, viewer_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT payload_json FROM goal_history WHERE viewer_id = ? ORDER BY started_at, history_id",
            (viewer_id,),
        ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def get_goal(self, viewer_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT payload_json FROM active_goals WHERE viewer_id = ?",
            (viewer_id,),
        ).fetchone()
        if row is None:
            return None
        return json.loads(row["payload_json"])
