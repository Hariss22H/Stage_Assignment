"""UTC/IST conversions and the replay clock policy.

All stored timestamps are UTC. IST (Asia/Kolkata, fixed UTC+05:30, no DST) is used
only for quiet hours, calendar-day caps, and distinct-day goals.

Two clocks are kept separate:
- Replay clock: `deliver_at` / `received_at` from the pack. Policy, ordering, and
  goal windows use this clock. Wall-clock "now" is never used for a replay decision.
- Processing clock: measured wall time spent handling one trigger. `published_at`
  is `deliver_at` plus that duration. Fault injection must not sleep in wall time.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))

# Matches the simulator's live-session skip: a gap above 10 minutes is an
# offline-buffered or badly skewed device timestamp, not a live visit.
OFFLINE_BUFFER_SECONDS = 600


def parse_utc(value: str | None) -> datetime | None:
    """Parse an ISO-8601 UTC timestamp. Empty values return None."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp is missing a timezone: {value}")
    return parsed.astimezone(UTC)


def format_utc(moment: datetime) -> str:
    """Format a timezone-aware datetime as ISO-8601 UTC with millisecond precision."""
    utc = moment.astimezone(UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def to_ist(moment: datetime) -> datetime:
    return moment.astimezone(IST)


def ist_date(moment: datetime):
    return to_ist(moment).date()


def in_quiet_hours(moment: datetime) -> bool:
    """True from 22:00 IST inclusive until 08:00 IST exclusive.

    This matches the simulator (`hour >= 22 or hour < 8`).
    """
    hour = to_ist(moment).hour
    return hour >= 22 or hour < 8


def earliest_allowed_send(moment: datetime) -> datetime:
    """Return `moment` when it is outside quiet hours, otherwise the next 08:00 IST."""
    if not in_quiet_hours(moment):
        return moment.astimezone(UTC)
    local = to_ist(moment)
    day = local.date()
    if local.hour >= 22:
        day = day + timedelta(days=1)
    send = datetime(day.year, day.month, day.day, 8, 0, tzinfo=IST)
    return send.astimezone(UTC)


def gap_seconds(later: datetime, earlier: datetime) -> float:
    return (later - earlier).total_seconds()


def is_offline_buffered(
    occurred_at: datetime,
    received_at: datetime,
    threshold_seconds: float = OFFLINE_BUFFER_SECONDS,
) -> bool:
    """A positive received-minus-occurred gap above the threshold is buffered or skewed.

    A negative gap means the device clock is ahead of the server. That is clock skew,
    not an offline buffer, and does not by itself make the server delivery stale.
    """
    return gap_seconds(received_at, occurred_at) > threshold_seconds


def business_time(
    occurred_at: datetime,
    received_at: datetime,
    threshold_seconds: float = OFFLINE_BUFFER_SECONDS,
) -> datetime:
    """Choose the timestamp used for goal windows and distinct IST days.

    When the device clock and the server disagree by more than the threshold in
    either direction, the server `received_at` is the only trustworthy time.
    """
    if abs(gap_seconds(received_at, occurred_at)) <= threshold_seconds:
        return occurred_at
    return received_at


def freshness_seconds(observed_at: datetime, as_of: datetime) -> float:
    return gap_seconds(as_of, observed_at)


def add_window(start: datetime, window_hours: float) -> datetime:
    return start + timedelta(hours=window_hours)
