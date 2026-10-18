# Implementation log

## Stage 1 — scaffold, clocks, loaders, audit

Done. `audit` CLI and timestamp/dedupe/profile helpers tested.

## Stage 2 — SQLite state and fact precedence

Done. Idempotent publish and dialect/subscription/consent precedence tested.

## Stage 3 — Goal engine

Done. All configured goals evaluated; G10/G11 excluded; sticky active goal; G03 correction; ranking documented.

Validated ranking against pack overlap:

- G08 is broadly eligible for active subscribers whenever recent titles exist → placed after G09.
- G02 is very broad → residual last place.
- Final order: G04, G01, G03, G07, G06, G12, G05, G09, G08, G02.

## Stage 4 — Planner and verifier

Done. Home-page-first planner; independent verifier for holdout, freshness, consent, quiet hours, caps, dialect, availability, page shape, templates, copy, injection. 12 dedicated tests passed.

## Stage 5 — Replay

Done. Delivery-time merge, controls, model faults without sleep, duplicate suppression. Full-pack smoke test passed (~83s).

## Stage 6 — Outcomes and evaluation

Done. Supplied simulator run; outcomes judged; replay ITT and pilot ITT written. Observed replay uplift -0.59 pp; pilot ITT -1.30 pp; biased served-vs-others +22.4 pp reproduced for explanation only.

## Stage 7 — Testing and docs

Done. `pytest` 52 passed. Docs written. Fresh `python -m src.main replay ...` inspected.
