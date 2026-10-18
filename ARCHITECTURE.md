# Architecture

## Ownership

Each viewer has one logical agent record in SQLite (`viewer_state`, `active_goals`, `decisions`, `actions`, `processed_events`, `goal_history`). A unique constraint on `trigger_event_id` makes publish transactional: a redelivered event cannot create a second decision or action.

## Replay clocks

- **Replay clock:** `deliver_at` / `received_at` from the pack. Ordering, quiet hours, goal windows, and IST calendar days use this clock.
- **Processing clock:** wall time spent on one trigger. `published_at = deliver_at + decided_in_ms`.
- Injected model timeouts and dependency windows do not sleep in wall time. They force deterministic fallback and are recorded on the decision.

Timeline merge order at equal timestamps: controls, then live events, then triggers.

## Facts and precedence

`src/facts.py` resolves subscription, consent, signup, and dialect with provenance.

- Current dialect: triggering `app_dialect`. Profile preference and campaign deeplink remain as conflicts.
- Subscription: latest `received_at` among purchase/renewal/lapse events when newer than the profile; otherwise the profile.
- Consent: profile opt-in, overridden by a later `notification_opt_out` for that channel. Unknown is not consent.
- Signup: earliest `signup_completed.received_at`, else profile `signup_at`.
- Offline buffer: `received_at - occurred_at > 600` seconds. Negative gaps are clock skew, not buffers.

## Goals

`src/goals.py` evaluates every configured goal. G10 and G11 are excluded because their success events are not measurable in the pack.

Deterministic priority (lower wins):

`G04 > G01 > G03 > G07 > G06 > G12 > G05 > G09 > G08 > G02`

G09 ranks above G08 because every active subscriber is eligible for G08 whenever recent titles exist, which would make G09 unreachable. G02 is last because “never completed an episode” is very broad. An active goal stays active until success, cancelled premise, or deadline. A G03 rule correction retargets only open G03 goals.

## Planner and verifier

`src/interventions.py` prefers `home_page` for a fresh visit, then consented messaging, then in-app. Catalogue descriptions are never treated as instructions.

`src/verifier.py` recomputes holdout, freshness, goal measurability, dialect, availability, page shape, consent, quiet hours, frequency caps, templates, copy, budgets, and prompt-injection markers. Failed checks publish `NO_ACTION`.

## Model providers

- `mock` (default): reproduces `replay/model_faults.jsonl` without sleeping.
- `openai` (opt-in): acknowledged but closed for pack upload; decisions stay deterministic.

## Controls

- `title_withdrawn`: sets `available_until` on the title.
- `goal_rule_corrected`: versions the rule and re-judges matching open goals only.
- `dependency_unavailable` / `dependency_degraded`: force fallback / synthetic latency without wall-clock waits.

## Outcomes and ITT

The supplied simulator consumes `actions.jsonl`. Outcomes are judged from configured success predicates inside `[started_at, deadline]`. Replay ITT and pilot ITT both use randomized-arm denominators. Served-versus-others is reported only to explain the biased pilot headline.
