"""Кнопка проверки уведомлений (TG-01, NFR-05).

Нажатие отправляет тестовое сообщение по текущему каналу и показывает
результат: что принято службой notify, а что нет.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SmartAnalystCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: SmartAnalystCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([TestNotificationButton(coordinator, entry)])


class TestNotificationButton(CoordinatorEntity[SmartAnalystCoordinator], ButtonEntity):
    """Отправить проверочное сообщение в настроенный канал."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "test_notification"
    _attr_icon = "mdi:send-check"

    def __init__(self, coordinator: SmartAnalystCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_test_notification"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="HA Smart Analyst",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_press(self) -> None:
        await self.coordinator.async_send_test_notification()
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        return self.coordinator.last_delivery
