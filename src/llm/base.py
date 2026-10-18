"""Provider interface. The planner owns decisions; providers only return claims."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class ModelResponse:
    ok: bool
    fault: str | None = None
    claim: dict | None = None
    raw: object | None = None
    reason: str = ""
    fallback_used: bool = False


@dataclass
class DecisionContext:
    trigger_event_id: str
    viewer_id: str
    eligible_goal_ids: list[str] = field(default_factory=list)


class ModelProvider(Protocol):
    name: str

    def propose(self, context: DecisionContext) -> ModelResponse:
        ...
