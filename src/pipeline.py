"""End-to-end mock replay: decide → simulate → judge → measure → report."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from src.audit import audit_pack
from src.goals import GoalCatalog
from src.impact import pilot_itt, render_impact_markdown, replay_itt
from src.outcomes import judge_outcomes, write_outcomes
from src.pack_index import PackIndex
from src.replay import run_replay
from src.time_utils import format_utc


def run_pipeline(
    pack_path: str | Path,
    out_dir: str | Path,
    *,
    model: str = "mock",
    reset: bool = True,
    seed: str = "oua-practice",
) -> dict:
    pack_path = Path(pack_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "viewer_state").mkdir(parents=True, exist_ok=True)

    audit = audit_pack(pack_path)
    (out_dir / "data_audit.json").write_text(json.dumps(audit, indent=2) + "\n", encoding="utf-8")

    replay = run_replay(pack_path, out_dir, model=model, reset=reset)
    pack = PackIndex(pack_path)

    sim_dir = out_dir / "sim"
    sim_dir.mkdir(parents=True, exist_ok=True)
    actions_path = out_dir / "actions.jsonl"
    if not actions_path.exists():
        actions_path.write_text("", encoding="utf-8")
    simulator = pack_path / "simulator" / "simulate_outcomes.py"
    command = [
        sys.executable,
        str(simulator),
        "--pack",
        str(pack_path),
        "--actions",
        str(actions_path),
        "--out",
        str(sim_dir),
        "--seed",
        seed,
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(
            "simulator failed:\n" + (completed.stderr or completed.stdout or "no output")
        )

    catalog = GoalCatalog(pack.goals_doc, pack.observed_event_types)
    # Horizon is 14 days after 2026-10-01 IST; judge after that window.
    judged_at = "2026-10-16T00:00:00.000Z"
    decisions = [json.loads(line) for line in (out_dir / "decisions.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    outcomes = judge_outcomes(
        decisions=decisions,
        outcome_events_path=sim_dir / "outcome_events.jsonl",
        catalogue=pack.catalogue,
        episodes=pack.episodes,
        catalog=catalog,
        judged_at=judged_at,
    )
    write_outcomes(out_dir / "outcomes.jsonl", outcomes)

    replay_report = replay_itt(pack.arms, outcomes, decisions)
    pilot_report = pilot_itt(pack.pilot_assignment, pack.pilot_outcomes)
    impact_md = render_impact_markdown(replay_report, pilot_report)
    (out_dir / "impact_report.md").write_text(impact_md, encoding="utf-8")
    (out_dir / "impact_report.json").write_text(
        json.dumps({"replay": replay_report, "pilot": pilot_report}, indent=2) + "\n",
        encoding="utf-8",
    )

    latency = json.loads((out_dir / "latency_report.json").read_text(encoding="utf-8"))
    holdout_actions = sum(1 for action in replay.actions if pack.arms.get(action["user_id"]) == "holdout")
    final = _final_report(
        pack_path=pack_path,
        out_dir=out_dir,
        replay=replay,
        latency=latency,
        replay_report=replay_report,
        pilot_report=pilot_report,
        audit=audit,
        holdout_actions=holdout_actions,
        simulator_stderr=completed.stderr.strip(),
    )
    (out_dir / "final_report.md").write_text(final, encoding="utf-8")
    return {
        "decisions": len(replay.decisions),
        "actions": len(replay.actions),
        "outcomes": len(outcomes),
        "duplicates": replay.duplicates,
        "holdout_actions": holdout_actions,
        "replay_itt": replay_report,
        "pilot_itt": pilot_report,
        "latency": latency,
    }


def _final_report(*, pack_path, out_dir, replay, latency, replay_report, pilot_report, audit, holdout_actions, simulator_stderr) -> str:
    return "\n".join(
        [
            "# Final report",
            "",
            "## Commands",
            "",
            "```bash",
            f"python -m src.main replay --pack {pack_path} --out {out_dir} --model mock --reset",
            "python -m pytest tests -q",
            "```",
            "",
            "## Replay summary",
            "",
            f"- Decisions: {len(replay.decisions)}",
            f"- Actions: {len(replay.actions)}",
            f"- Duplicate triggers suppressed: {replay.duplicates}",
            f"- Model faults observed: {replay.faults_seen}",
            f"- Holdout actions published: {holdout_actions} (must be 0)",
            f"- Latency p50/p95/max ms: {latency.get('p50')}/{latency.get('p95')}/{latency.get('max')}",
            f"- Over 60s: {latency.get('over_60s')}; over 5m: {latency.get('over_5m')}",
            f"- Simulator: {simulator_stderr}",
            "",
            "## Data traps handled",
            "",
            f"- Event duplicates: {audit['events']['duplicate_event_ids']}",
            f"- Offline-buffered app opens: {audit['events']['offline_buffered_app_open_canonical']}",
            f"- Prompt-injection title ids: {audit['catalogue']['prompt_injection_title_ids']}",
            f"- Unmeasurable goals: {[g['goal_id'] for g in audit['goals_missing_success_event']]}",
            "",
            "## Replay ITT",
            "",
            f"- Agent {replay_report['agent']['successes']}/{replay_report['agent']['users']} "
            f"({100 * replay_report['agent']['rate']:.2f}%)",
            f"- Holdout {replay_report['holdout']['successes']}/{replay_report['holdout']['users']} "
            f"({100 * replay_report['holdout']['rate']:.2f}%)",
            f"- Absolute uplift: {replay_report['absolute_uplift_pp']:.2f} pp",
            "",
            "## Pilot ITT",
            "",
            f"- Treatment {pilot_report['treatment']['successes']}/{pilot_report['treatment']['users']} "
            f"({100 * pilot_report['treatment']['rate']:.2f}%)",
            f"- Holdout {pilot_report['holdout']['successes']}/{pilot_report['holdout']['users']} "
            f"({100 * pilot_report['holdout']['rate']:.2f}%)",
            f"- Absolute uplift: {pilot_report['absolute_uplift_pp']:.2f} pp",
            "",
            pilot_report["selection_bias_explanation"],
            "",
            f"Generated at {format_utc(datetime.now(timezone.utc))}",
            "",
        ]
    )
