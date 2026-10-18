# Known limitations

Only limitations that remain after the implemented and tested path.

## Messaging is rare in this run

The planner prefers a dialect-matching home page on a fresh trigger. In the sample mock run, published actions were home pages only. Push, WhatsApp, and in-app paths are implemented, independently verified, and covered by cancellation tests. Messaging is selected only when a page cannot be built. Impact is therefore mostly a home-page exposure effect.

## Goal ranking is a documented heuristic

The priority order is deterministic and tested, but it is not learned. A different defensible order would change the goal mix. G06 treats any `lapsed` status as eligible because `goals.json` gives no “recently” window.

## Business time vs simulator visit skip

Stale triggers use `received_at - occurred_at > 600`. The simulator skips live sessions using `sent_at - occurred_at > 600`. On this pack those sets are close but not identical. A few triggers the simulator would still treat as visits are abstained here.

## Content-slug resume is dialect-local

G07 matches the stored `content_slug`. In this pack slugs do not span dialects, so a dialect switch does not resume a cross-dialect family.

## OpenAI provider is not a live decision path

`--model openai` is accepted for interface completeness. It does not call the network or upload pack data. Decisions remain deterministic. Wiring a real structured-output call was deferred to protect privacy and the eight-hour budget.

## ITT success definition

Replay ITT counts a user as successful if any judged goal outcome is `SUCCEEDED`. Users who never triggered, and therefore never received a goal, remain in the denominator as non-success. That is intentional ITT, but it makes absolute rates small.

## Outcome attribution is not causal per user

Individual `SUCCEEDED` rows are not claimed as caused by the action. Causal reading is only the randomized-arm aggregate in `impact_report.md`.

## Dependency degradation is synthetic

The degraded user-data-store window adds a fixed 50 ms to measured processing time rather than injecting real timeouts. The outage is visible in decision metadata without making the replay non-reproducible.
