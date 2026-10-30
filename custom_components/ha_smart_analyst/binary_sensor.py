"""Диагностические бинарные сенсоры: доступность ИИ и Recorder."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
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
class AnalystBinaryDescription(BinarySensorEntityDescription):
    """Бинарный сенсор с функцией извлечения значения."""

    value_fn: Callable[[AnalystSnapshot], bool]


BINARY_SENSORS: tuple[AnalystBinaryDescription, ...] = (
    AnalystBinaryDescription(
        key="ai_available",
        translation_key="ai_available",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        value_fn=lambda data: data.ai_available,
    ),
    AnalystBinaryDescription(
        key="recorder_available",
        translation_key="recorder_available",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        value_fn=lambda data: data.recorder_available,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: SmartAnalystCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        AnalystBinarySensor(coordinator, entry, description) for description in BINARY_SENSORS
    )


class AnalystBinarySensor(CoordinatorEntity[SmartAnalystCoordinator], BinarySensorEntity):
    """Бинарный сенсор состояния подсистем интеграции."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: SmartAnalystCoordinator,
        entry: ConfigEntry,
        description: AnalystBinaryDescription,
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
    def is_on(self) -> bool | None:
        data = self.coordinator.data
        if data is None:
            return None
        return self.entity_description.value_fn(data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        data = self.coordinator.data
        if data is None:
            return None
        return {"ai_error": data.ai_error} if data.ai_error else None
