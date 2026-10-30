"""Адаптер Recorder для ядра истории (HIST-01, HIST-02, NFR-03).

Чтение истории выполняется в executor: event loop не блокируется. Провайдер
не обещает long-term statistics там, где их нет, и не выдаёт отсутствие истории
за «всё в норме». Если у измерения задан атрибут, история читается по нему:
``state.state`` для ``climate`` — это режим, а не температура.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.history import state_changes_during_period
from homeassistant.core import HomeAssistant

from .core.history import Sample, Window
from .core.observations import Quality

_LOGGER = logging.getLogger(__name__)

_NON_NUMERIC = frozenset({"", "unknown", "unavailable", "none", "nan", "inf", "-inf"})


@dataclass(frozen=True, slots=True)
class HistorySource:
    """Откуда брать историю измерения: сущность и, если нужно, её атрибут."""

    entity_id: str
    attribute: str | None = None


def _numeric(raw: object) -> float | None:
    """Привести значение к числу, не выдумывая данные для режимов и мусора."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
    else:
        text = str(raw).strip().replace(",", ".")
        if text.lower() in _NON_NUMERIC:
            return None
        try:
            value = float(text)
        except ValueError:
            return None
    if math.isnan(value) or math.isinf(value):
        return None
    return value


def state_sample(state: object, attribute: str | None = None) -> Sample | None:
    """Сэмпл из состояния Recorder: атрибут важнее основного состояния."""
    if attribute:
        attributes = getattr(state, "attributes", None) or {}
        raw = attributes.get(attribute)
    else:
        raw = getattr(state, "state", None)
    value = _numeric(raw)
    if value is None:
        return None
    moment = getattr(state, "last_updated", None) or getattr(state, "last_changed", None)
    if not isinstance(moment, datetime):
        return None
    return Sample(moment, value, Quality.OK)


def _read_states(
    hass: HomeAssistant, window: Window, sources: Mapping[str, HistorySource]
) -> dict[str, list[Sample]]:
    result: dict[str, list[Sample]] = {measurement_id: [] for measurement_id in sources}
    entity_ids = sorted({source.entity_id for source in sources.values() if source.entity_id})
    if not entity_ids:
        return result
    raw_states: dict[str, list[object]] = {}
    for entity_id in entity_ids:
        try:
            # В HA 2025.1 функция принимает одну сущность, а не список.
            states = state_changes_during_period(
                hass,
                window.start,
                window.end,
                entity_id=entity_id,
                include_start_time_state=True,
            )
        except Exception as err:  # noqa: BLE001 - Recorder может быть выключен
            _LOGGER.debug("История %s недоступна: %s", entity_id, err)
            raw_states[entity_id] = []
            continue
        raw_states[entity_id] = list(states.get(entity_id, ()))
    for measurement_id, source in sources.items():
        samples = [
            state_sample(state, source.attribute)
            for state in raw_states.get(source.entity_id, ())
        ]
        result[measurement_id] = [sample for sample in samples if sample is not None]
    return result


class CachedHistory:
    """Снимок истории для одного цикла оценки (реализует ``HistoryProvider``)."""

    def __init__(self, by_measurement: Mapping[str, Sequence[Sample]]) -> None:
        self._by_measurement = {key: list(value) for key, value in by_measurement.items()}

    def samples(self, measurement_id: str, window: Window) -> Sequence[Sample]:
        return self._by_measurement.get(measurement_id, ())

    @classmethod
    async def async_load(
        cls, hass: HomeAssistant, window: Window, sources: Mapping[str, HistorySource]
    ) -> CachedHistory:
        """Прочитать историю всех нужных измерений одним заходом в executor."""
        if not sources:
            return cls({})
        recorder = get_instance(hass)
        await recorder.async_wait_recorder()
        raw = await hass.async_add_executor_job(_read_states, hass, window, sources)
        return cls(raw)
