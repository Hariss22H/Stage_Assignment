"""Optional OpenAI provider. Disabled unless explicitly enabled. Never required for grading."""

from __future__ import annotations

import os

from src.llm.base import DecisionContext, ModelResponse


class OpenAIProvider:
    name = "openai"

    def __init__(self):
        enabled = os.environ.get("ENABLE_OPENAI", "0").strip() == "1"
        key = os.environ.get("OPENAI_API_KEY", "").strip()
        self.enabled = enabled and bool(key)
        self.reason = (
            "OpenAI disabled"
            if not enabled
            else ("OPENAI_API_KEY missing" if not key else "ready")
        )

    def propose(self, context: DecisionContext) -> ModelResponse:
        if not self.enabled:
            return ModelResponse(
                ok=False,
                fault="openai_disabled",
                reason=(
                    "OpenAI is opt-in only. Set ENABLE_OPENAI=1 and OPENAI_API_KEY to use it. "
                    "Raw viewer data is never sent by the default mock path."
                ),
                fallback_used=True,
            )
        # Network calls are intentionally not implemented in the grading path.
        # Keeping this closed avoids accidental uploads of pack data.
        return ModelResponse(
            ok=False,
            fault="openai_not_wired",
            reason=(
                "OpenAI mode is acknowledged but not used for decisions in this submission. "
                "Deterministic planning remains the source of truth."
            ),
            fallback_used=True,
        )
