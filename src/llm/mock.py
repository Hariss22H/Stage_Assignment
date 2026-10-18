"""Offline deterministic provider. Reproduces replay/model_faults.jsonl without sleeping."""

from __future__ import annotations

from src.llm.base import DecisionContext, ModelResponse


class MockProvider:
    name = "mock"

    def __init__(self, faults: dict[str, dict] | None = None):
        self.faults = faults or {}

    def propose(self, context: DecisionContext) -> ModelResponse:
        fault = self.faults.get(context.trigger_event_id)
        if fault is None:
            return ModelResponse(ok=True, claim=None, reason="no optional model claim")
        kind = fault.get("fault")
        if kind == "timeout":
            # Do not sleep. Record the injected timeout and force deterministic fallback.
            return ModelResponse(
                ok=False,
                fault="timeout",
                reason=f"injected timeout of {fault.get('seconds')}s; no wall-clock wait",
                fallback_used=True,
            )
        if kind == "malformed_output":
            return ModelResponse(
                ok=False,
                fault="malformed_output",
                raw=fault.get("model_output"),
                reason="injected malformed JSON",
                fallback_used=True,
            )
        if kind == "unsupported_claim":
            claim = fault.get("model_output")
            if not isinstance(claim, dict):
                return ModelResponse(
                    ok=False,
                    fault="unsupported_claim",
                    raw=claim,
                    reason="injected unsupported claim was not an object",
                    fallback_used=True,
                )
            return ModelResponse(
                ok=False,
                fault="unsupported_claim",
                claim=claim,
                raw=claim,
                reason="injected unsupported eligibility claim",
                fallback_used=True,
            )
        return ModelResponse(
            ok=False,
            fault=str(kind),
            raw=fault,
            reason="unknown injected fault",
            fallback_used=True,
        )
