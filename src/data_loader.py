"""CSV/JSONL readers, dedupe, and profile resolution.

Catalogue descriptions, titles, search queries, and deeplinks are untrusted strings.
This module never interprets them as instructions.
"""

from __future__ import annotations

import csv
import json
import re
from collections import defaultdict
from pathlib import Path

from src.time_utils import parse_utc

CAMPAIGN_DEEPLINK = re.compile(r"^ott://c/([^/]+)/([^/]+)/title/([^/]+)$")
TITLE_DEEPLINK = re.compile(r"^ott://title/([^/]+)$")


class RowError(Exception):
    """A pack row is malformed. The row number is included when known."""


def iter_jsonl(path: Path):
    """Yield (line_number, object) for non-empty lines. Raises RowError on bad JSON."""
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                obj = json.loads(text)
            except json.JSONDecodeError as exc:
                raise RowError(f"{path}:{line_number}: invalid JSON") from exc
            if not isinstance(obj, dict):
                raise RowError(f"{path}:{line_number}: expected a JSON object")
            yield line_number, obj


def read_csv_rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise RowError(f"{path}: missing header")
        return list(reader)


def parse_campaign_deeplink(value: str | None) -> dict | None:
    if not value:
        return None
    match = CAMPAIGN_DEEPLINK.match(str(value).strip())
    if not match:
        return None
    return {
        "campaign": match.group(1),
        "dialect": match.group(2),
        "title_id": match.group(3),
    }


def dedupe_events(events: list[dict], source: str) -> tuple[list[dict], list[dict]]:
    """Keep one record per event_id: the earliest `received_at`, then earliest file order.

    Redeliveries in this pack differ only by a later `received_at` (and `deliver_at`
    on replay files). The first server receipt is the canonical copy. Conflicts are
    returned and must not be discarded silently by callers that persist state.
    """
    kept: dict[str, tuple[int, dict]] = {}
    conflicts: list[dict] = []
    for index, event in enumerate(events):
        event_id = event.get("event_id")
        if not event_id:
            raise RowError(f"{source}: event is missing event_id")
        received = parse_utc(event.get("received_at"))
        if received is None:
            raise RowError(f"{source}: {event_id} is missing received_at")
        previous = kept.get(event_id)
        if previous is None:
            kept[event_id] = (index, event)
            continue
        prev_index, prev_event = previous
        prev_received = parse_utc(prev_event.get("received_at"))
        assert prev_received is not None
        drop = event
        keep_event = prev_event
        if received < prev_received or (received == prev_received and index < prev_index):
            kept[event_id] = (index, event)
            drop = prev_event
            keep_event = event
        conflicts.append(
            {
                "event_id": event_id,
                "source": source,
                "kept_received_at": keep_event.get("received_at"),
                "dropped_received_at": drop.get("received_at"),
                "reason": "duplicate event_id; kept the earliest received_at",
            }
        )
    ordered = [item[1] for item in sorted(kept.values(), key=lambda pair: pair[0])]
    return ordered, conflicts


def resolve_profiles(rows: list[dict]) -> tuple[dict[str, dict], list[dict]]:
    """One profile per user. Latest `updated_at` wins; file order breaks a tie.

    `snapshot_at` is the export time and is identical for every row in the supplied
    pack, so it is not a useful precedence key. Duplicate rows are logged.
    """
    grouped: dict[str, list[tuple[int, dict]]] = defaultdict(list)
    for index, row in enumerate(rows):
        user_id = row.get("user_id")
        if not user_id:
            raise RowError("profile row is missing user_id")
        grouped[user_id].append((index, row))

    resolved: dict[str, dict] = {}
    conflicts: list[dict] = []
    for user_id, items in grouped.items():
        def sort_key(item: tuple[int, dict]) -> tuple[str, int]:
            index, row = item
            return (row.get("updated_at") or "", index)

        ranked = sorted(items, key=sort_key)
        winner_index, winner = ranked[-1]
        resolved[user_id] = winner
        if len(ranked) == 1:
            continue
        for index, row in ranked[:-1]:
            differing = sorted(
                key for key in winner.keys() if (row.get(key) or "") != (winner.get(key) or "")
            )
            conflicts.append(
                {
                    "user_id": user_id,
                    "fact": "profile_snapshot",
                    "kept_updated_at": winner.get("updated_at"),
                    "dropped_updated_at": row.get("updated_at"),
                    "kept_row": winner_index,
                    "dropped_row": index,
                    "differing_fields": differing,
                    "precedence": "latest updated_at, then later file order",
                }
            )
    return resolved, conflicts


def index_catalogue(rows: list[dict]) -> dict[str, dict]:
    """Index titles by title_id. Descriptions stay ordinary string fields."""
    catalogue: dict[str, dict] = {}
    for row in rows:
        title_id = row.get("title_id")
        if not title_id:
            raise RowError("catalogue row is missing title_id")
        if title_id in catalogue:
            raise RowError(f"duplicate title_id: {title_id}")
        catalogue[title_id] = row
    return catalogue


def episode_ids_by_slug(rows: list[dict]) -> dict[str, list[str]]:
    """Episode ids for each content slug, ordered by season and episode number."""
    grouped: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
    for row in rows:
        slug = row.get("content_slug")
        episode_id = row.get("episode_id")
        if not slug or not episode_id:
            raise RowError("episode row is missing content_slug or episode_id")
        grouped[slug].append((int(row["season"]), int(row["episode"]), episode_id))
    return {slug: [item[2] for item in sorted(items)] for slug, items in grouped.items()}


def title_available(row: dict, at) -> bool:
    """Availability is `available_from <= at` and (`available_until` empty or `at < available_until`).

    An inverted window (`available_until <= available_from`) is never available.
    Withdrawn titles are represented by setting `available_until` to the withdrawal time.
    """
    start = parse_utc(row.get("available_from"))
    if start is None or at < start:
        return False
    end_raw = row.get("available_until") or ""
    if not str(end_raw).strip():
        return True
    end = parse_utc(str(end_raw))
    if end is None or end <= start:
        return False
    return at < end
