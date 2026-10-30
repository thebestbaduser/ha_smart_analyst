"""Наблюдения и качество измерений (DATA-01…DATA-04).

``Observation`` — нормализованный снимок источника HA. Модуль не импортирует
Home Assistant: адаптер интеграции передаёт уже разобранные значения и метки
времени, а ядро отвечает за качество, возраст данных и типы.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum

from .errors import InvalidValueError
from .units import find_unit

UNKNOWN_RAW_STATES = frozenset({"unknown", ""})
UNAVAILABLE_RAW_STATES = frozenset({"unavailable", "none"})
TRUE_RAW_STATES = frozenset({"on", "true", "1", "yes", "open"})
FALSE_RAW_STATES = frozenset({"off", "false", "0", "no", "closed"})


class Quality(StrEnum):
    """Качество измерения (DATA-02)."""

    OK = "ok"
    STALE = "stale"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"
    UNCONFIGURED = "unconfigured"
    HISTORY_MISSING = "history_missing"


class ValueType(StrEnum):
    """Тип значения наблюдения."""

    NUMBER = "number"
    BOOLEAN = "boolean"
    STATE = "state"
    UNKNOWN = "unknown"


def ensure_utc(moment: datetime | None, fallback: datetime) -> datetime:
    """Привести метку времени к UTC (DATA-04).

    Наивные значения трактуются как UTC: HA отдаёт aware-datetime, но данные
    из хранилища и тестов могут быть без зоны.
    """
    if moment is None:
        return fallback
    if moment.tzinfo is None:
        return moment.replace(tzinfo=fallback.tzinfo)
    return moment.astimezone(fallback.tzinfo)


@dataclass(frozen=True, slots=True)
class Observation:
    """Нормализованное наблюдение источника."""

    source_id: str
    entity_id: str
    value: float | bool | str | None
    value_type: ValueType
    unit: str | None
    observed_at: datetime
    quality: Quality
    last_changed: datetime | None = None
    last_updated: datetime | None = None
    last_reported: datetime | None = None
    source: str = "home_assistant"
    attributes_used: tuple[str, ...] = ()

    @property
    def numeric_value(self) -> float | None:
        """Числовое значение или ``None`` (без неявных приведений)."""
        if self.value_type is ValueType.NUMBER and isinstance(self.value, float):
            return self.value
        return None

    @property
    def is_usable(self) -> bool:
        return self.quality is Quality.OK and self.value is not None

    def age_seconds(self, now: datetime) -> float | None:
        """Возраст последнего сообщения источника, а не последнего изменения."""
        reported = self.last_reported or self.last_updated or self.last_changed
        if reported is None:
            return None
        return (ensure_utc(now, self.observed_at) - ensure_utc(reported, self.observed_at)).total_seconds()

    def with_quality(self, quality: Quality) -> Observation:
        return replace(self, quality=quality)


def _parse_number(raw: str, path: str) -> float:
    try:
        number = float(raw.replace(",", "."))
    except (TypeError, ValueError) as err:
        raise InvalidValueError(f"{path}: {raw!r} не является числом") from err
    if math.isnan(number) or math.isinf(number):
        raise InvalidValueError(f"{path}: значение {raw!r} недопустимо")
    return number


def build_observation(
    *,
    source_id: str,
    entity_id: str,
    raw_state: object,
    now: datetime,
    unit: str | None = None,
    attributes: dict[str, object] | None = None,
    last_changed: datetime | None = None,
    last_updated: datetime | None = None,
    last_reported: datetime | None = None,
    expected_numeric: bool = True,
    max_age: timedelta | None = None,
    source: str = "home_assistant",
) -> Observation:
    """Собрать наблюдение из «сырого» состояния HA.

    Качество вычисляется по факту значения и возрасту последнего сообщения,
    а не по неизменности числа (DATA-03): статический датчик с повторной
    публикацией остаётся ``ok``.
    """
    observed_at = ensure_utc(now, now)
    changed = ensure_utc(last_changed, observed_at) if last_changed else None
    updated = ensure_utc(last_updated, observed_at) if last_updated else None
    reported = ensure_utc(last_reported, observed_at) if last_reported else None

    text = "" if raw_state is None else str(raw_state).strip()
    lowered = text.lower()

    if raw_state is None or lowered in UNKNOWN_RAW_STATES:
        return Observation(
            source_id=source_id,
            entity_id=entity_id,
            value=None,
            value_type=ValueType.UNKNOWN,
            unit=unit,
            observed_at=observed_at,
            quality=Quality.UNKNOWN,
            last_changed=changed,
            last_updated=updated,
            last_reported=reported,
            source=source,
        )
    if lowered in UNAVAILABLE_RAW_STATES:
        return Observation(
            source_id=source_id,
            entity_id=entity_id,
            value=None,
            value_type=ValueType.UNKNOWN,
            unit=unit,
            observed_at=observed_at,
            quality=Quality.UNAVAILABLE,
            last_changed=changed,
            last_updated=updated,
            last_reported=reported,
            source=source,
        )

    quality = Quality.OK
    if expected_numeric:
        try:
            value: float | bool | str = _parse_number(text, entity_id)
        except InvalidValueError:
            value = text
            value_type = ValueType.STATE
            quality = Quality.INVALID
        else:
            value_type = ValueType.NUMBER
    elif lowered in TRUE_RAW_STATES or lowered in FALSE_RAW_STATES:
        value = lowered in TRUE_RAW_STATES
        value_type = ValueType.BOOLEAN
    else:
        value = text
        value_type = ValueType.STATE

    observation = Observation(
        source_id=source_id,
        entity_id=entity_id,
        value=value,
        value_type=value_type,
        unit=unit,
        observed_at=observed_at,
        quality=quality,
        last_changed=changed,
        last_updated=updated,
        last_reported=reported,
        source=source,
    )
    if max_age is not None:
        age = observation.age_seconds(observed_at)
        if age is not None and age > max_age.total_seconds():
            observation = observation.with_quality(Quality.STALE)
    return observation


def unconfigured(source_id: str, entity_id: str, now: datetime, unit: str | None = None) -> Observation:
    """Наблюдение для сущности, которая ещё не сопоставлена модели."""
    moment = ensure_utc(now, now)
    return Observation(
        source_id=source_id,
        entity_id=entity_id,
        value=None,
        value_type=ValueType.UNKNOWN,
        unit=unit,
        observed_at=moment,
        quality=Quality.UNCONFIGURED,
    )


def history_missing(source_id: str, entity_id: str, now: datetime) -> Observation:
    """Заглушка качества для отсутствующей истории (DATA-02, AC-10)."""
    moment = ensure_utc(now, now)
    return Observation(
        source_id=source_id,
        entity_id=entity_id,
        value=None,
        value_type=ValueType.UNKNOWN,
        unit=None,
        observed_at=moment,
        quality=Quality.HISTORY_MISSING,
    )


def described_unit(observation: Observation) -> str:
    """Человекочитаемая единица; неизвестная единица не подменяется догадкой."""
    if not observation.unit:
        return ""
    return observation.unit if find_unit(observation.unit) else f"{observation.unit} (?)"
