"""Clock policy: IST quiet hours, stale gaps, and business time."""

from datetime import datetime, timezone

from src.time_utils import (
    OFFLINE_BUFFER_SECONDS,
    business_time,
    earliest_allowed_send,
    format_utc,
    in_quiet_hours,
    is_offline_buffered,
    ist_date,
    parse_utc,
)


def _utc(text: str) -> datetime:
    parsed = parse_utc(text)
    assert parsed is not None
    return parsed


def test_parse_and_format_round_trip():
    moment = _utc("2026-10-01T16:30:00.000Z")
    assert moment.tzinfo == timezone.utc
    assert format_utc(moment) == "2026-10-01T16:30:00.000Z"


def test_ist_date_crosses_midnight_utc():
    # 18:30Z is 00:00 IST the next calendar day.
    assert str(ist_date(_utc("2026-09-30T18:30:00.000Z"))) == "2026-10-01"
    assert str(ist_date(_utc("2026-09-30T18:29:59.000Z"))) == "2026-09-30"


def test_quiet_hours_match_simulator_boundaries():
    assert in_quiet_hours(_utc("2026-10-01T16:30:00.000Z"))  # 22:00 IST
    assert in_quiet_hours(_utc("2026-10-01T02:29:00.000Z"))  # 07:59 IST
    assert not in_quiet_hours(_utc("2026-10-01T02:30:00.000Z"))  # 08:00 IST
    assert not in_quiet_hours(_utc("2026-10-01T16:29:00.000Z"))  # 21:59 IST


def test_earliest_send_moves_to_next_0800_ist():
    send = earliest_allowed_send(_utc("2026-10-01T16:30:00.000Z"))
    assert format_utc(send) == "2026-10-02T02:30:00.000Z"
    daytime = _utc("2026-10-01T06:00:00.000Z")  # 11:30 IST
    assert earliest_allowed_send(daytime) == daytime


def test_offline_buffer_ignores_clock_ahead():
    received = _utc("2026-10-01T12:00:00.000Z")
    occurred_ahead = _utc("2026-10-01T12:30:00.000Z")
    assert not is_offline_buffered(occurred_ahead, received)
    occurred_old = _utc("2026-10-01T11:00:00.000Z")
    assert is_offline_buffered(occurred_old, received)
    assert OFFLINE_BUFFER_SECONDS == 600


def test_business_time_falls_back_to_server_when_skewed():
    received = _utc("2026-10-01T12:00:00.000Z")
    plausible = _utc("2026-10-01T11:59:30.000Z")
    skewed = _utc("2026-10-01T08:00:00.000Z")
    assert business_time(plausible, received) == plausible
    assert business_time(skewed, received) == received
