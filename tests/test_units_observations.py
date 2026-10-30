"""Тесты единиц измерения и наблюдений."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from core.errors import InvalidValueError, UnitError
from core.observations import (
    Quality,
    ValueType,
    build_observation,
)
from core.units import convert, require_unit, validate_number

NOW = datetime(2026, 10, 30, 20, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("value", "source", "target", "expected"),
    [
        (32.0, "°F", "°C", 0.0),
        (0.0, "°C", "°F", 32.0),
        (1.0, "kW", "W", 1000.0),
        (1500.0, "Wh", "kWh", 1.5),
        (1.0, "bar", "Pa", 100_000.0),
        (30.0, "min", "s", 1800.0),
    ],
)
def test_convert_compatible_units(value: float, source: str, target: str, expected: float) -> None:
    assert convert(value, source, target) == pytest.approx(expected)


def test_power_and_energy_are_not_mixed() -> None:
    with pytest.raises(UnitError):
        convert(1.0, "kW", "kWh")


def test_unknown_unit_rejected() -> None:
    with pytest.raises(UnitError):
        require_unit("furlong")


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, "21.5"])
def test_invalid_numbers_rejected(value: object) -> None:
    with pytest.raises(InvalidValueError):
        validate_number(value)


def test_unknown_and_unavailable_quality() -> None:
    unknown = build_observation(
        source_id="m1", entity_id="sensor.temp", raw_state="unknown", now=NOW
    )
    unavailable = build_observation(
        source_id="m1", entity_id="sensor.temp", raw_state="unavailable", now=NOW
    )
    assert unknown.quality is Quality.UNKNOWN
    assert unavailable.quality is Quality.UNAVAILABLE
    assert unknown.numeric_value is None


def test_numeric_observation_parsed() -> None:
    observation = build_observation(
        source_id="m1",
        entity_id="sensor.temp",
        raw_state="21.5",
        unit="°C",
        now=NOW,
        last_updated=NOW,
    )
    assert observation.quality is Quality.OK
    assert observation.value_type is ValueType.NUMBER
    assert observation.numeric_value == pytest.approx(21.5)


def test_non_numeric_value_marked_invalid() -> None:
    observation = build_observation(
        source_id="m1", entity_id="sensor.temp", raw_state="очень тепло", now=NOW
    )
    assert observation.quality is Quality.INVALID
    assert observation.value_type is ValueType.STATE


def test_binary_state_parsed() -> None:
    observation = build_observation(
        source_id="leak", entity_id="binary_sensor.leak", raw_state="on", now=NOW, expected_numeric=False
    )
    assert observation.value is True
    assert observation.value_type is ValueType.BOOLEAN


def test_stale_by_last_reported_not_by_value_change() -> None:
    """AC-09: неизменное число не означает отказ, если публикации продолжаются."""
    fresh = build_observation(
        source_id="m1",
        entity_id="sensor.static",
        raw_state="21.5",
        now=NOW,
        last_changed=NOW - timedelta(days=2),
        last_updated=NOW - timedelta(days=2),
        last_reported=NOW - timedelta(minutes=1),
        max_age=timedelta(minutes=10),
    )
    stale = build_observation(
        source_id="m1",
        entity_id="sensor.silent",
        raw_state="21.5",
        now=NOW,
        last_changed=NOW - timedelta(days=2),
        last_updated=NOW - timedelta(days=1),
        max_age=timedelta(minutes=10),
    )
    assert fresh.quality is Quality.OK
    assert stale.quality is Quality.STALE


def test_age_uses_reported_then_updated() -> None:
    observation = build_observation(
        source_id="m1",
        entity_id="sensor.temp",
        raw_state="20",
        now=NOW,
        last_updated=NOW - timedelta(minutes=5),
    )
    assert observation.age_seconds(NOW) == pytest.approx(300.0)
