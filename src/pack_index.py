"""Load and index the data pack once for replay."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from src.config import (
    known_prices,
    load_goals,
    load_interventions,
    load_pricing,
    resolve_pack,
)
from src.data_loader import (
    dedupe_events,
    episode_ids_by_slug,
    index_catalogue,
    iter_jsonl,
    read_csv_rows,
    resolve_profiles,
)


class PackIndex:
    def __init__(self, pack: str | Path):
        self.root = resolve_pack(pack)
        self.goals_doc = load_goals(self.root)
        self.interventions = load_interventions(self.root)
        self.pricing = load_pricing(self.root)
        self.allowed_prices = known_prices(self.pricing)
        profile_rows = read_csv_rows(self.root / "data" / "profiles_snapshot.csv")
        self.profiles, self.profile_conflicts = resolve_profiles(profile_rows)
        self.catalogue = index_catalogue(read_csv_rows(self.root / "data" / "catalogue.csv"))
        self.episodes = episode_ids_by_slug(read_csv_rows(self.root / "data" / "episodes.csv"))
        self.arms = {
            row["user_id"]: row["arm"]
            for row in read_csv_rows(self.root / "replay" / "arms.csv")
        }
        history = [event for _line, event in iter_jsonl(self.root / "data" / "events.jsonl")]
        history, self.history_dupes = dedupe_events(history, "data/events.jsonl")
        self.events_by_user: dict[str, list[dict]] = defaultdict(list)
        self.observed_event_types: set[str] = set()
        for event in history:
            self.events_by_user[event["user_id"]].append(event)
            if event.get("event"):
                self.observed_event_types.add(event["event"])
        self.triggers = [event for _line, event in iter_jsonl(self.root / "replay" / "triggers.jsonl")]
        self.live_events = [event for _line, event in iter_jsonl(self.root / "replay" / "live_events.jsonl")]
        self.controls = [event for _line, event in iter_jsonl(self.root / "replay" / "control_events.jsonl")]
        self.model_faults = {
            row["trigger_event_id"]: row
            for _line, row in iter_jsonl(self.root / "replay" / "model_faults.jsonl")
        }
        self.pilot_assignment = read_csv_rows(self.root / "pilot" / "pilot_assignment.csv")
        self.pilot_outcomes = read_csv_rows(self.root / "pilot" / "pilot_outcomes.csv")

    def events_for(self, user_id: str) -> list[dict]:
        return list(self.events_by_user.get(user_id, []))
