# Final report

## Implementation summary

Built an offline decide → verify → act → measure loop over the supplied STAGE pack. Deterministic code owns facts, goals, policy, idempotency, and impact. The mock provider reproduces injected faults without sleeping. Unsupported model claims are rejected as warnings and do not void an independently verified deterministic action. Pending messages are cancelled on purchase, channel opt-out, goal closure, or title withdrawal. OpenAI is optional and unused on the default path.

## Commands actually run

```bash
python -m src.main replay --pack ./one_user_one_agent_pack --out ./sample_run --model mock --reset
python -m pytest tests -q
```

Results from the inspected gate run:

- Decisions: 2679 unique triggers (90 duplicate deliveries suppressed)
- Actions: 1062 (home pages only in this run)
- Holdout actions: 0
- Latency p50 / p95 / max: 7 / 50 / 119 ms; over 60s: 0; over 5m: 0
- Simulator: completed against `sample_run/actions.jsonl`
- Tests: 60 passed

## Data traps found and handling

| Trap | Handling |
|---|---|
| 2176 duplicate history event ids | Keep earliest `received_at`; second delivery publishes nothing |
| Offline-buffered opens | `received_at - occurred_at > 600` → stale → `NO_ACTION` |
| Dialect conflicts / deeplinks | Trigger `app_dialect` wins; conflicts retained |
| Duplicate profiles | Latest `updated_at` wins |
| `T0002` prompt injection | Description never enters copy; verifier rejects injection markers |
| Withdrawn `T0001` | Catalogue `available_until` set; pending messages with that title cancelled |
| G03 success correction | Only open G03 goals re-versioned |
| G10 / G11 | Excluded: null success event / missing `tv_app_open` |
| Invalid dialects (`d7`, `NA`, missing) | No title action |
| Unsupported model claims | Claim rejected as a warning; verified deterministic plan may still publish |

## Goal policy

Priority: G04, G01, G03, G07, G06, G12, G05, G09, G08, G02. Active goals are sticky. G09 outranks G08 so habit building is reachable. G02 is residual.

## Sample walkthrough

Trigger `D-000001` for `U-02344` (`app_open`, dialect `d3`):

1. Evidence showed an unfinished series with a play gap ≥ 3 days.
2. Goal `G07 resume_series_3d` was committed with a 72h deadline and slug-specific success.
3. Verifier passed dialect, availability, and page shape.
4. A `home_page` action was published for dialect `d3`.
5. Later simulator events and the outcome judge decide success or failure inside the window.

## Replay ITT

- Agent: 62 / 2513 (2.47%)
- Holdout: 76 / 2487 (3.06%)
- Absolute uplift: **-0.59 pp**
- 95% CI for (agent - holdout): about -1.50 to 0.32 pp

This does not support a causal claim that the agent improved outcomes on this replay day.

## Pilot randomized-arm re-analysis

- Treatment: 349 / 1912 (18.25%)
- Holdout: 387 / 1979 (19.56%)
- Absolute uplift: **-1.30 pp**

The internal headline (served 40.1% vs all others 17.7%, +22.4 pp) is selection bias: served users were chosen by the agent. The randomized comparison is the causal estimate.

## Known limitations

See `KNOWN_LIMITATIONS.md`. Messaging paths are implemented but home page dominated this run; OpenAI is not wired for live calls; ITT rates are low because non-triggered users remain in the denominator.

## Conclusion

The submission is a complete, tested mock pipeline with independent verification, idempotent publish, simulator integration, pending-message cancellation, and honest negative ITT. It does not claim positive causal impact.
