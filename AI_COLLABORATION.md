# AI collaboration

Cursor (Composer) assisted implementation of this repository. The candidate reviewed the generated code, ran the tests and end-to-end command, and kept or rejected suggestions based on the pack and `spec.md`.

## Accepted after inspection

- Modular layout matching the spec (`facts`, `goals`, `verifier`, `replay`, SQLite store).
- Offline-buffer threshold of 600 seconds aligned with the simulator’s live-session skip.
- Mock provider that records an injected 45-second timeout without sleeping.
- ITT denominators over all randomized arm users, with an explicit served-versus-others contrast for the pilot bias explanation.

## Corrected or rejected suggestions

1. **Shortest-window goal ranking as the primary order.**  
   Rejected. Pack counts show G08 is eligible for essentially every active subscriber whenever recent titles exist, so ranking G08 by window would hide G09. G02 would also dominate because “never completed an episode” is common. The implemented order prefers rare or specific states (trial, first play, resume, lapse, absence, habit) and keeps G08 and G02 later. Validated by unit tests for G07-over-G02 and G09-over-G08.

2. **Treating negative `received_at - occurred_at` as offline buffering.**  
   Rejected. A device clock ahead of the server is skew, not a buffered upload. Only positive gaps above 600 seconds are stale. Validated in `tests/test_time_utils.py` and the audit’s separate negative-gap count.

3. **Generating the entire application in one pass / claiming replay complete before tests.**  
   Rejected. Stages 1–2 were scaffolded and tested first; `replay` exited nonzero until the decision loop existed. The end-to-end claim was made only after `python -m src.main replay ...` and `pytest` were actually run.

4. **Optional OpenAI calls on the default path.**  
   Rejected for evaluation. The default provider is mock. OpenAI remains opt-in and does not upload pack rows in this submission.

## Additional correction before submission

5. **Treating an unsupported model goal claim as a hard verifier failure that voids a verified home page.**  
   Rejected after the final audit. Spec requires rejecting the unsupported claim, not discarding an independently verified deterministic plan. Claims are now warnings; regression covered by `test_unsupported_model_goal_claim_is_warning_not_hard_fail`.

## What was run

- `python -m pytest tests -q` → 60 passed
- `python -m src.main replay --pack ./one_user_one_agent_pack --out ./sample_run --model mock --reset` → decisions 2679, actions 1062, holdout actions 0
