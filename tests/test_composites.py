"""Тесты составных измерений."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from core.composites import compute_composite
from core.errors import ModelError
from core.model import Composite, CompositeOperation, Measurement, SourceRef
from core.observations import Observation, Quality, ValueType

NOW = datetime(2026, 10, 30, 20, 0, tzinfo=UTC)


def observation(measurement_id: str, value: float | None, unit: str = "°C", quality: Quality = Quality.OK) -> Observation:
    return Observation(
        source_id=measurement_id,
        entity_id=f"sensor.{measurement_id}",
        value=value,
        value_type=ValueType.NUMBER if value is not None else ValueType.UNKNOWN,
        unit=unit,
        observed_at=NOW,
        quality=quality,
    )


def composite_measurement(operation: CompositeOperation, operands: tuple[str, ...], unit: str | None = "°C", factor: float = 1.0) -> Measurement:
    return Measurement(
        id="result",
        name="Результат",
        unit=unit,
        composite=Composite(operation, operands, factor),
    )


@pytest.mark.parametrize(
    ("operation", "values", "expected"),
    [
        (CompositeOperation.SUM, (20.0, 22.0), 42.0),
        (CompositeOperation.DIFFERENCE, (60.0, 57.8), 2.2),
        (CompositeOperation.AVERAGE, (20.0, 22.0), 21.0),
    ],
)
def test_basic_operations(operation, values, expected) -> None:
    measurement = composite_measurement(operation, ("a", "b"))
    observations = {"a": observation("a", values[0]), "b": observation("b", values[1])}
    result = compute_composite(measurement, observations)
    assert result is not None
    assert result.numeric_value == pytest.approx(expected)
    assert result.quality is Quality.OK


def test_scale_operation() -> None:
    measurement = composite_measurement(CompositeOperation.SCALE, ("a",), unit="W", factor=1000.0)
    result = compute_composite(measurement, {"a": observation("a", 1.5, "W")})
    assert result is not None
    assert result.numeric_value == pytest.approx(1500.0)


def test_units_converted_to_target() -> None:
    measurement = composite_measurement(CompositeOperation.SUM, ("a", "b"), unit="V")
    observations = {
        "a": observation("a", 230.0, "V"),
        "b": observation("b", 0.25, "kV"),
    }
    result = compute_composite(measurement, observations)
    assert result is not None
    assert result.numeric_value == pytest.approx(480.0)


def test_incompatible_units_rejected() -> None:
    measurement = composite_measurement(CompositeOperation.SUM, ("a", "b"), unit="V")
    observations = {"a": observation("a", 230.0, "V"), "b": observation("b", 20.0, "°C")}
    with pytest.raises(ModelError):
        compute_composite(measurement, observations)


def test_worst_quality_is_inherited() -> None:
    measurement = composite_measurement(CompositeOperation.AVERAGE, ("a", "b"))
    observations = {
        "a": observation("a", 20.0),
        "b": observation("b", 22.0, quality=Quality.STALE),
    }
    result = compute_composite(measurement, observations)
    assert result is not None
    assert result.quality is Quality.STALE


def test_missing_value_returns_unknown_not_zero() -> None:
    measurement = composite_measurement(CompositeOperation.SUM, ("a", "b"))
    observations = {
        "a": observation("a", 20.0),
        "b": observation("b", None, quality=Quality.UNAVAILABLE),
    }
    result = compute_composite(measurement, observations)
    assert result is not None
    assert result.value is None
    assert result.quality is Quality.UNAVAILABLE


def test_unready_operand_returns_none() -> None:
    measurement = composite_measurement(CompositeOperation.SUM, ("a", "b"))
    assert compute_composite(measurement, {"a": observation("a", 20.0)}) is None


def test_observed_at_is_latest_operand() -> None:
    measurement = composite_measurement(CompositeOperation.SUM, ("a", "b"))
    earlier = Observation(
        source_id="a",
        entity_id="sensor.a",
        value=20.0,
        value_type=ValueType.NUMBER,
        unit="°C",
        observed_at=NOW - timedelta(minutes=5),
        quality=Quality.OK,
    )
    later = observation("b", 22.0)
    result = compute_composite(measurement, {"a": earlier, "b": later})
    assert result is not None
    assert result.observed_at == NOW


def test_non_composite_measurement_returns_none() -> None:
    measurement = Measurement(id="m", name="m", source=SourceRef("sensor.x"))
    assert compute_composite(measurement, {}) is None
