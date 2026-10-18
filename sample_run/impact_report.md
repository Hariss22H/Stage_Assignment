# Impact report

## Replay intention-to-treat

- Denominator: all users in replay/arms.csv
- Agent: 62/2513 (2.47%)
- Holdout: 76/2487 (3.06%)
- Absolute uplift: -0.59 percentage points
- 95% CI for (agent - holdout): -1.50 to 0.32 pp
- Agent users with a goal: 862; with an action: 703
- Holdout users with an action (must be 0): 0

## Pilot randomized-arm re-analysis

- Treatment: 349/1912 (18.25%)
- Holdout: 387/1979 (19.56%)
- Absolute uplift: -1.30 percentage points

### Why the internal headline differs

The internal headline compared users the agent chose to serve with everyone else. Served users were selected by the agent and were already more likely to succeed. The randomized comparison keeps every treatment-arm user in the treatment denominator, including those never served, and compares that group with the holdout arm.

Biased served-vs-others contrast on the same rows: 40.1% vs 17.7% (+22.4 pp).
