"""Invariants of the supplied pack. Exact user ids and outcome rates are not hard-coded."""

from pathlib import Path

from src.audit import audit_pack

PACK = Path("one_user_one_agent_pack")


def test_supplied_pack_audit_invariants():
    report = audit_pack(PACK)
    events = report["events"]
    assert events["lines"] > events["unique_event_ids"]
    assert events["extra_lines"] == events["lines"] - events["unique_event_ids"]
    assert events["duplicate_event_ids"] > 0
    assert events["missing_timestamps"] == 0
    assert events["offline_buffered_app_open_canonical"] > 0
    assert events["deeplink_dialect_conflicts_canonical"] > 0
    assert "tv_app_open" not in events["event_types_canonical"]
    assert "<missing>" in events["app_dialect_counts_canonical"]
    assert "d7" in events["app_dialect_counts_canonical"]

    profiles = report["profiles"]
    assert profiles["rows"] > profiles["unique_users"]
    assert profiles["duplicate_users"] > 0
    assert profiles["event_users_without_profile"] > 0

    catalogue = report["catalogue"]
    assert catalogue["slugs_with_multiple_title_ids"] > 0
    assert catalogue["slugs_spanning_dialects"] == 0
    assert catalogue["inverted_availability_windows"]
    assert catalogue["prompt_injection_title_ids"] == ["T0002"]

    missing = {item["goal_id"] for item in report["goals_missing_success_event"]}
    assert missing == {"G10", "G11"}

    replay = report["replay"]
    assert replay["triggers"]["lines"] > replay["triggers"]["unique_event_ids"]
    assert set(replay["controls"]["types"]) == {
        "title_withdrawn",
        "goal_rule_corrected",
        "dependency_unavailable",
        "dependency_degraded",
    }
    assert set(replay["model_faults"]["types"]) == {"unsupported_claim", "malformed_output", "timeout"}
    assert set(replay["arms"]) == {"agent", "holdout"}
    assert replay["arm_rows"] == sum(replay["arms"].values())

    pilot = report["pilot_outcome_counts_by_arm"]
    assert pilot["assignment_rows"] == pilot["outcome_rows"]
    assert pilot["treatment"]["users"] > 0
    assert pilot["holdout"]["users"] > 0
