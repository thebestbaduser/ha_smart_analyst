"""Адаптер Recorder для ядра истории (HIST-01, HIST-02, NFR-03).

Чтение истории выполняется в executor: event loop не блокируется. Провайдер
не обещает long-term statistics там, где их нет, и не выдаёт отсутствие истории
за «всё в норме».
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import datetime

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.history import state_changes_during_period
from homeassistant.core import HomeAssistant

from .core.history import Sample, Window
from .core.observations import Quality

_LOGGER = logging.getLogger(__name__)


def _to_sample(state: object) -> Sample | None:
    raw = getattr(state, "state", None)
    if raw in (None, "unknown", "unavailable", ""):
        return None
    try:
        value = float(str(raw).replace(",", "."))
    except ValueError:
        return None
    moment = getattr(state, "last_updated", None) or getattr(state, "last_changed", None)
    if not isinstance(moment, datetime):
        return None
    return Sample(moment, value, Quality.OK)


def _read_states(
    hass: HomeAssistant, window: Window, entity_ids: Sequence[str]
) -> dict[str, list[Sample]]:
    result: dict[str, list[Sample]] = {entity_id: [] for entity_id in entity_ids}
    if not entity_ids:
        return result
    try:
        states = state_changes_during_period(
            hass, window.start, window.end, entity_id=list(entity_ids)
        )
    except Exception as err:  # noqa: BLE001 - Recorder может быть выключен
        _LOGGER.debug("История недоступна: %s", err)
        return result
    for entity_id, items in states.items():
        samples = [_to_sample(state) for state in items]
        result[entity_id] = [sample for sample in samples if sample is not None]
    return result


class CachedHistory:
    """Снимок истории для одного цикла оценки (реализует ``HistoryProvider``)."""

    def __init__(self, by_measurement: Mapping[str, Sequence[Sample]]) -> None:
        self._by_measurement = {key: list(value) for key, value in by_measurement.items()}

    def samples(self, measurement_id: str, window: Window) -> Sequence[Sample]:
        return self._by_measurement.get(measurement_id, ())

    @classmethod
    async def async_load(
        cls, hass: HomeAssistant, window: Window, entity_map: Mapping[str, str]
    ) -> CachedHistory:
        """Прочитать историю всех нужных измерений одним заходом в executor."""
        entity_ids = sorted({entity_id for entity_id in entity_map.values() if entity_id})
        if not entity_ids:
            return cls({})
        recorder = get_instance(hass)
        await recorder.async_wait_recorder()
        raw = await hass.async_add_executor_job(_read_states, hass, window, entity_ids)
        by_measurement = {
            measurement_id: raw.get(entity_id, [])
            for measurement_id, entity_id in entity_map.items()
        }
        return cls(by_measurement)
