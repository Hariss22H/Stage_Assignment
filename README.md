# One User, One Agent

Offline, auditable lifecycle manager for the STAGE take-home. One logical agent owns each viewer. On every replay trigger the system reconstructs evidence, chooses at most one measurable goal, plans a proportionate action or abstains, verifies the proposal independently, publishes idempotently, then measures outcomes with intention-to-treat on randomized arms.

This is not a title recommender. Decision quality, provenance, verification, reliability, and honest measurement matter more than uplift.

## Requirements

- Python 3.11+
- Standard library for the replay path
- `pytest` for tests (`pip install -r requirements.txt`)

## Data pack

Place the supplied pack at `./one_user_one_agent_pack` (already present in this repository). Treat it as personal data. Do not upload raw events, profiles, catalogue text, or identifiers to a hosted model.

## One-command mock replay

```bash
python -m src.main replay --pack ./one_user_one_agent_pack --out ./sample_run --model mock --reset
```

No API key and no network are required. The command:

1. Audits the pack
2. Replays triggers, live events, controls, and injected model faults
3. Writes decisions, actions, viewer state, and latency
4. Runs the supplied simulator
5. Judges goal outcomes
6. Computes replay ITT and pilot ITT
7. Writes `impact_report.md` and `final_report.md`

## Tests

```bash
python -m pytest tests -q
```

## Optional OpenAI mode

OpenAI is opt-in and not used for grading.

```bash
# .env is gitignored. Copy from .env.example if needed.
set ENABLE_OPENAI=1
set OPENAI_API_KEY=...
python -m src.main replay --pack ./one_user_one_agent_pack --out ./sample_run --model openai --reset
```

Even when selected, this submission keeps decisions on the deterministic planner and does not send pack rows to a hosted model. Mock mode remains the supported evaluation path.

## Outputs

| Path | Contents |
|---|---|
| `sample_run/decisions.jsonl` | One decision per unique trigger, including abstentions |
| `sample_run/actions.jsonl` | Simulator-compatible published actions |
| `sample_run/viewer_state/state.sqlite` | Durable per-viewer state |
| `sample_run/outcomes.jsonl` | Goal judgments after simulation |
| `sample_run/latency_report.json` | Processing-time percentiles |
| `sample_run/impact_report.md` | Replay ITT and pilot re-analysis |
| `sample_run/final_report.md` | Run summary |
| `sample_run/data_audit.json` | Recomputed pack traps |

## Design principles

- Deterministic core owns facts, eligibility, policy, idempotency, and impact
- Mock model is default; injected faults never sleep for 45 seconds
- Verifier recomputes safety checks and can reject the planner
- Holdout users receive zero actions of every kind
- Honest low or negative uplift is preferred to a biased served-vs-others claim

See `ARCHITECTURE.md`, `KNOWN_LIMITATIONS.md`, and `AI_COLLABORATION.md`.
