"""Диагностические сенсоры интеграции (NFR-05)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import AnalystSnapshot, SmartAnalystCoordinator


@dataclass(frozen=True, kw_only=True)
class AnalystSensorDescription(SensorEntityDescription):
    """Описание сенсора с функцией извлечения значения."""

    value_fn: Callable[[AnalystSnapshot], Any]


SENSORS: tuple[AnalystSensorDescription, ...] = (
    AnalystSensorDescription(
        key="active_incidents",
        translation_key="active_incidents",
        icon="mdi:alert-circle-outline",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.active_incidents,
    ),
    AnalystSensorDescription(
        key="critical_incidents",
        translation_key="critical_incidents",
        icon="mdi:alert-octagon",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.critical_incidents,
    ),
    AnalystSensorDescription(
        key="queue_pending",
        translation_key="queue_pending",
        icon="mdi:send-clock",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.queue_pending,
    ),
    AnalystSensorDescription(
        key="stale_sources",
        translation_key="stale_sources",
        icon="mdi:timer-sand",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.stale_sources,
    ),
    AnalystSensorDescription(
        key="rules_total",
        translation_key="rules_total",
        icon="mdi:script-text-outline",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.rules_total,
    ),
    AnalystSensorDescription(
        key="tokens_used",
        translation_key="tokens_used",
        icon="mdi:counter",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda data: data.tokens_used,
    ),
    AnalystSensorDescription(
        key="last_report",
        translation_key="last_report",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda data: data.last_report_at,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: SmartAnalystCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        AnalystSensor(coordinator, entry, description) for description in SENSORS
    )


class AnalystSensor(CoordinatorEntity[SmartAnalystCoordinator], SensorEntity):
    """Сенсор, читающий снимок координатора."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: SmartAnalystCoordinator,
        entry: ConfigEntry,
        description: AnalystSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="HA Smart Analyst",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def native_value(self) -> Any:
        data = self.coordinator.data
        if data is None:
            return None
        return self.entity_description.value_fn(data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        data = self.coordinator.data
        if data is None:
            return None
        return {
            "generated_at": data.generated_at,
            "ai_available": data.ai_available,
            "ai_error": data.ai_error,
            "recorder_available": data.recorder_available,
        }
