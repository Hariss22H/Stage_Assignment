"""Compact, recomputed audit of the supplied pack. Counts are not hard-coded."""

from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path

from src.config import load_goals, resolve_pack
from src.data_loader import (
    index_catalogue,
    iter_jsonl,
    parse_campaign_deeplink,
    read_csv_rows,
    resolve_profiles,
)
from src.time_utils import OFFLINE_BUFFER_SECONDS, gap_seconds, parse_utc


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round((pct / 100.0) * (len(ordered) - 1))))
    return round(ordered[index], 3)


def _delay_summary(values: list[float]) -> dict:
    if not values:
        return {"count": 0}
    return {
        "count": len(values),
        "min": round(min(values), 3),
        "p50": _percentile(values, 50),
        "p95": _percentile(values, 95),
        "p99": _percentile(values, 99),
        "max": round(max(values), 3),
        "negative": sum(1 for value in values if value < 0),
        "over_600s": sum(1 for value in values if value > OFFLINE_BUFFER_SECONDS),
        "over_3600s": sum(1 for value in values if value > 3600),
        "over_86400s": sum(1 for value in values if value > 86400),
    }


def audit_pack(pack: str | Path) -> dict:
    root = resolve_pack(pack)
    goals = load_goals(root)
    event_types: Counter[str] = Counter()
    event_ids: Counter[str] = Counter()
    users: set[str] = set()
    missing_timestamps = 0
    negative_clock = 0
    recv_occ: list[float] = []
    sent_occ: list[float] = []
    app_open = 0
    buffered_app_open = 0
    deeplink_app_open = 0
    deeplink_dialect_conflict = 0
    app_dialects: Counter[str] = Counter()
    latest_dialect: dict[str, tuple[str, str]] = {}
    seen_event_ids: set[str] = set()
    injection_titles = []
    lines = 0

    for _line, event in iter_jsonl(root / "data" / "events.jsonl"):
        lines += 1
        event_id = event.get("event_id") or ""
        event_ids[event_id] += 1
        # File order in this pack delivers the earlier received_at first. Behavioral
        # counts use the canonical copy so a redelivery is not a second visit.
        if event_id and event_id in seen_event_ids:
            continue
        if event_id:
            seen_event_ids.add(event_id)
        event_types[event.get("event") or ""] += 1
        user_id = event.get("user_id")
        if user_id:
            users.add(user_id)
        occurred = parse_utc(event.get("occurred_at"))
        sent = parse_utc(event.get("sent_at"))
        received = parse_utc(event.get("received_at"))
        if occurred is None or sent is None or received is None:
            missing_timestamps += 1
            continue
        recv_gap = gap_seconds(received, occurred)
        sent_gap = gap_seconds(sent, occurred)
        recv_occ.append(recv_gap)
        sent_occ.append(sent_gap)
        if sent < occurred or received < occurred:
            negative_clock += 1
        dialect = event.get("app_dialect")
        app_dialects[dialect if dialect else "<missing>"] += 1
        if user_id and dialect:
            previous = latest_dialect.get(user_id)
            if previous is None or received.isoformat() >= previous[0]:
                latest_dialect[user_id] = (received.isoformat(), dialect)
        if event.get("event") == "app_open":
            app_open += 1
            if recv_gap > OFFLINE_BUFFER_SECONDS:
                buffered_app_open += 1
            parsed = parse_campaign_deeplink((event.get("properties") or {}).get("deeplink"))
            if parsed:
                deeplink_app_open += 1
                if parsed["dialect"] != dialect:
                    deeplink_dialect_conflict += 1

    duplicate_ids = {key: count for key, count in event_ids.items() if count > 1 and key}
    profile_rows = read_csv_rows(root / "data" / "profiles_snapshot.csv")
    profiles, profile_conflicts = resolve_profiles(profile_rows)
    profile_users = set(profiles)
    dialect_profile_conflict = 0
    for user_id, (_received, dialect) in latest_dialect.items():
        profile = profiles.get(user_id)
        if not profile:
            continue
        preferred = profile.get("preferred_dialect") or ""
        if preferred and preferred != dialect:
            dialect_profile_conflict += 1

    catalogue_rows = read_csv_rows(root / "data" / "catalogue.csv")
    catalogue = index_catalogue(catalogue_rows)
    slug_titles: dict[str, set[str]] = defaultdict(set)
    slug_dialects: dict[str, set[str]] = defaultdict(set)
    inverted_windows = []
    for row in catalogue_rows:
        slug_titles[row["content_slug"]].add(row["title_id"])
        slug_dialects[row["content_slug"]].add(row["dialect"])
        start = parse_utc(row.get("available_from"))
        end_raw = (row.get("available_until") or "").strip()
        end = parse_utc(end_raw) if end_raw else None
        if start is not None and end is not None and end <= start:
            inverted_windows.append(row["title_id"])
        description = row.get("description") or ""
        lowered = description.lower()
        if "ignore your previous" in lowered or "system note" in lowered:
            injection_titles.append(row["title_id"])

    observed_events = set(event_types)
    unmeasurable = []
    for goal in goals["goals"]:
        success_event = (goal.get("success") or {}).get("event")
        if not success_event or success_event not in observed_events:
            unmeasurable.append(
                {
                    "goal_id": goal["goal_id"],
                    "success_event": success_event,
                    "reason": "success event is null"
                    if not success_event
                    else "success event is absent from data/events.jsonl",
                }
            )

    def stream_summary(relative: str, kind: str) -> dict:
        count = 0
        ids: Counter[str] = Counter()
        types: Counter[str] = Counter()
        stream_users: set[str] = set()
        for _line, row in iter_jsonl(root / relative):
            count += 1
            if row.get("event_id"):
                ids[row["event_id"]] += 1
            if row.get("user_id"):
                stream_users.add(row["user_id"])
            types[row.get("type") or row.get("event") or row.get("fault") or ""] += 1
        extra = sum(value - 1 for value in ids.values() if value > 1)
        return {
            "path": relative,
            "lines": count,
            "unique_event_ids": len(ids) if kind == "event" else None,
            "duplicate_event_ids": sum(1 for value in ids.values() if value > 1) if kind == "event" else None,
            "extra_lines": extra if kind == "event" else None,
            "users": len(stream_users),
            "types": dict(sorted(types.items())),
        }

    arms = read_csv_rows(root / "replay" / "arms.csv")
    assignment = read_csv_rows(root / "pilot" / "pilot_assignment.csv")
    outcomes = read_csv_rows(root / "pilot" / "pilot_outcomes.csv")

    def rate(rows: list[dict], arm: str) -> dict:
        chosen = [row for row in rows if row.get("arm") == arm]
        successes = sum(1 for row in chosen if str(row.get("succeeded")).lower() == "true")
        return {"users": len(chosen), "successes": successes}

    return {
        "offline_buffer_threshold_seconds": OFFLINE_BUFFER_SECONDS,
        "offline_buffer_definition": (
            "app_open with received_at - occurred_at > threshold. "
            "Negative gaps are device clocks ahead of the server and are counted separately."
        ),
        "events": {
            "lines": lines,
            "unique_event_ids": len([key for key in event_ids if key]),
            "duplicate_event_ids": len(duplicate_ids),
            "extra_lines": sum(count - 1 for count in duplicate_ids.values()),
            "users": len(users),
            "event_types_canonical": dict(sorted(event_types.items())),
            "app_dialect_counts_canonical": dict(sorted(app_dialects.items())),
            "missing_timestamps": missing_timestamps,
            "negative_received_or_sent_vs_occurred": negative_clock,
            "received_minus_occurred_seconds": _delay_summary(recv_occ),
            "sent_minus_occurred_seconds": _delay_summary(sent_occ),
            "app_open_canonical": app_open,
            "offline_buffered_app_open_canonical": buffered_app_open,
            "campaign_deeplinks_canonical": deeplink_app_open,
            "deeplink_dialect_conflicts_canonical": deeplink_dialect_conflict,
        },
        "profiles": {
            "rows": len(profile_rows),
            "unique_users": len(profiles),
            "duplicate_users": len({row["user_id"] for row in profile_conflicts}),
            "event_users_without_profile": len(users - profile_users),
            "profile_users_without_events": len(profile_users - users),
            "profile_vs_latest_event_dialect_conflicts": dialect_profile_conflict,
        },
        "catalogue": {
            "titles": len(catalogue),
            "unique_content_slugs": len(slug_titles),
            "slugs_with_multiple_title_ids": sum(1 for ids in slug_titles.values() if len(ids) > 1),
            "slugs_spanning_dialects": sum(1 for dialects in slug_dialects.values() if len(dialects) > 1),
            "inverted_availability_windows": inverted_windows,
            "prompt_injection_title_ids": injection_titles,
        },
        "goals_missing_success_event": unmeasurable,
        "replay": {
            "triggers": stream_summary("replay/triggers.jsonl", "event"),
            "live_events": stream_summary("replay/live_events.jsonl", "event"),
            "controls": stream_summary("replay/control_events.jsonl", "control"),
            "model_faults": stream_summary("replay/model_faults.jsonl", "fault"),
            "arms": dict(sorted(Counter(row["arm"] for row in arms).items())),
            "arm_rows": len(arms),
        },
        "pilot_outcome_counts_by_arm": {
            "treatment": rate(outcomes, "treatment"),
            "holdout": rate(outcomes, "holdout"),
            "assignment_rows": len(assignment),
            "outcome_rows": len(outcomes),
        },
    }
