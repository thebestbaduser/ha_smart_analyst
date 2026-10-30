"""Составные измерения (SEM-07).

Сумма, разность, среднее и масштабирование нескольких источников. Произвольный
код и шаблоны не исполняются; единицы проверяются, качество наследуется от
худшего операнда, а не выдаётся за нормальное.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from .errors import ModelError
from .model import CompositeOperation, Measurement
from .observations import Observation, Quality, ValueType
from .units import compatible, convert

#: Чем хуже качество, тем больше число: итог берёт худший операнд.
_QUALITY_RANK = {
    Quality.OK: 0,
    Quality.STALE: 1,
    Quality.HISTORY_MISSING: 2,
    Quality.INVALID: 3,
    Quality.UNKNOWN: 4,
    Quality.UNAVAILABLE: 5,
    Quality.UNCONFIGURED: 6,
}


def _worse(first: Quality, second: Quality) -> Quality:
    return first if _QUALITY_RANK[first] >= _QUALITY_RANK[second] else second


def _empty(
    measurement: Measurement, quality: Quality, observed_at: datetime
) -> Observation:
    return Observation(
        source_id=measurement.id,
        entity_id=f"composite:{measurement.id}",
        value=None,
        value_type=ValueType.UNKNOWN,
        unit=measurement.unit,
        observed_at=observed_at,
        quality=quality,
        source="composite",
    )


def compute_composite(
    measurement: Measurement, observations: Mapping[str, Observation]
) -> Observation | None:
    """Посчитать составное измерение.

    ``None`` означает, что операнд ещё не вычислен (многоуровневые выражения);
    вызывающий делает несколько проходов.
    """
    composite = measurement.composite
    if composite is None:
        return None
    operands = [observations.get(operand_id) for operand_id in composite.operands]
    if any(item is None for item in operands):
        return None
    present = [item for item in operands if item is not None]
    observed_at = max(item.observed_at for item in present)

    quality = Quality.OK
    values: list[float] = []
    operand_unit: str | None = None
    for observation in present:
        quality = _worse(quality, observation.quality)
        value = observation.numeric_value
        if value is None:
            continue
        unit = observation.unit
        target = measurement.unit or unit
        if unit and target and unit != target:
            if not compatible(unit, target):
                raise ModelError(
                    f"составное измерение {measurement.name!r}: "
                    f"единицы {unit!r} и {target!r} несопоставимы"
                )
            value = convert(value, unit, target)
        values.append(value)
        operand_unit = operand_unit or target

    if len(values) != len(present):
        return _empty(
            measurement,
            quality if quality is not Quality.OK else Quality.UNKNOWN,
            observed_at,
        )

    if composite.operation is CompositeOperation.SUM:
        result = sum(values)
    elif composite.operation is CompositeOperation.DIFFERENCE:
        result = values[0] - values[1]
    elif composite.operation is CompositeOperation.AVERAGE:
        result = sum(values) / len(values)
    else:
        result = values[0] * composite.factor

    return Observation(
        source_id=measurement.id,
        entity_id=f"composite:{measurement.id}",
        value=float(result),
        value_type=ValueType.NUMBER,
        unit=measurement.unit or operand_unit,
        observed_at=observed_at,
        quality=quality,
        source="composite",
    )
