"""Тесты расписания отчётов (REP-05)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from core.reports import (
    ReportProfile,
    ReportType,
    describe_schedule,
    next_run_at,
    parse_schedule_times,
)

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)  # 16:00 в Самаре (UTC+4)


def profile(**overrides) -> ReportProfile:
    payload = {
        "id": "p1",
        "name": "Отчёт",
        "timezone": "Europe/Samara",
        "schedule_times": ("20:00",),
    }
    payload.update(overrides)
    return ReportProfile.from_dict(payload)


def test_manual_profile_has_no_next_run() -> None:
    manual = profile(schedule_times=())
    assert next_run_at(manual, NOW) is None
    assert manual.report_type is ReportType.ON_DEMAND
    assert describe_schedule(manual) == "вручную"


def test_next_run_today_in_profile_timezone() -> None:
    scheduled = profile()
    when = next_run_at(scheduled, NOW)
    assert when == datetime(2026, 10, 30, 16, 0, tzinfo=UTC)


def test_past_slot_moves_to_tomorrow() -> None:
    scheduled = profile(schedule_times=("08:00",))
    when = next_run_at(scheduled, NOW)
    assert when == datetime(2026, 10, 31, 4, 0, tzinfo=UTC)


def test_weekdays_filter() -> None:
    # 30.10.2026 — пятница; профиль только по будням, слот уже прошёл
    weekday_profile = profile(schedule_times=("08:00",), schedule_weekdays=(0, 1, 2, 3, 4))
    when = next_run_at(weekday_profile, NOW)
    assert when == datetime(2026, 11, 2, 4, 0, tzinfo=UTC)  # понедельник 08:00 самарского


def test_weekends_filter() -> None:
    weekend_profile = profile(schedule_times=("08:00",), schedule_weekdays=(5, 6))
    when = next_run_at(weekend_profile, NOW)
    assert when == datetime(2026, 10, 31, 4, 0, tzinfo=UTC)


def test_earliest_slot_wins() -> None:
    scheduled = profile(schedule_times=("20:00", "18:00", "08:00"))
    when = next_run_at(scheduled, datetime(2026, 10, 30, 13, 0, tzinfo=UTC))  # 17:00 Самара
    assert when == datetime(2026, 10, 30, 14, 0, tzinfo=UTC)  # 18:00 местного


def test_disabled_profile_never_runs() -> None:
    scheduled = profile(enabled=False)
    assert next_run_at(scheduled, NOW) is None
    assert "выключен" in describe_schedule(scheduled)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ((), ()),
        ("", ()),
        ("08:00", (8, 0)),
        ("20:00, 08:00", (8, 0)),
        (["07:30", "07:30"], (7, 30)),
    ],
)
def test_parse_schedule_times(value, expected) -> None:
    parsed = parse_schedule_times(value)
    if expected:
        assert parsed[0].hour == expected[0]
        assert parsed[-1].minute == expected[1]
    else:
        assert parsed == ()


@pytest.mark.parametrize("value", ["25:00", "8", "утро", "12:75"])
def test_invalid_time_rejected(value) -> None:
    with pytest.raises(ValueError):
        parse_schedule_times(value)


def test_from_dict_normalizes_scheduled_without_time() -> None:
    restored = ReportProfile.from_dict(
        {"id": "p", "name": "Старый", "report_type": "scheduled"}
    )
    assert restored.report_type is ReportType.ON_DEMAND
    assert restored.schedule_times == ()


def test_from_dict_rejects_bad_weekday() -> None:
    with pytest.raises(ValueError):
        ReportProfile.from_dict({"id": "p", "name": "x", "schedule_weekdays": [7]})


def test_describe_schedule_variants() -> None:
    assert describe_schedule(profile(schedule_times=("08:00", "20:00"))) == "ежедневно, 08:00, 20:00"
    assert describe_schedule(
        profile(schedule_times=("08:00",), schedule_weekdays=(0, 1, 2, 3, 4))
    ) == "по будням, 08:00"
    assert describe_schedule(
        profile(schedule_times=("08:00",), schedule_weekdays=(5, 6))
    ) == "по выходным, 08:00"
