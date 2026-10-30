"""Доставка уведомлений: notify-сущность, legacy-служба и persistent (TG-01, TG-07)."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.ha_smart_analyst.const import (
    CONF_AI_ENABLED,
    CONF_INSTANCE_NAME,
    CONF_TELEGRAM_RECIPIENTS,
    CONF_TELEGRAM_SERVICE,
    DOMAIN,
)


async def _setup(hass: HomeAssistant, entry: MockConfigEntry):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return hass.data[DOMAIN][entry.entry_id]


async def test_notify_entity_needs_no_chat_id(hass: HomeAssistant) -> None:
    hass.states.async_set("notify.telegram_bot_2130376715_181714455", "idle")
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Дом",
        data={
            CONF_INSTANCE_NAME: "Дом",
            CONF_AI_ENABLED: False,
            CONF_TELEGRAM_SERVICE: "notify.telegram_bot_2130376715_181714455",
        },
        options={},
    )
    entry.add_to_hass(hass)
    coordinator = await _setup(hass, entry)
    calls = async_mock_service(hass, "notify", "send_message")

    assert coordinator.notification_targets() == ["notify.telegram_bot_2130376715_181714455"]
    await coordinator.async_test_notification(
        "notify.telegram_bot_2130376715_181714455", "Проверка"
    )
    assert len(calls) == 1
    assert calls[0].data["entity_id"] == "notify.telegram_bot_2130376715_181714455"
    assert calls[0].data["message"] == "Проверка"


async def test_legacy_service_with_chat_id(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Дом",
        data={
            CONF_INSTANCE_NAME: "Дом",
            CONF_AI_ENABLED: False,
            CONF_TELEGRAM_SERVICE: "telegram_bot",
            CONF_TELEGRAM_RECIPIENTS: ["181714455"],
        },
        options={},
    )
    entry.add_to_hass(hass)
    coordinator = await _setup(hass, entry)
    calls = async_mock_service(hass, "notify", "telegram_bot")

    assert coordinator.notification_targets() == ["181714455"]
    await coordinator.async_test_notification("181714455", "Проверка")
    assert len(calls) == 1
    assert calls[0].data["target"] == "181714455"
    assert calls[0].data["data"]["chat_id"] == "181714455"


async def test_without_channel_uses_persistent(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Дом",
        data={CONF_INSTANCE_NAME: "Дом", CONF_AI_ENABLED: False},
        options={},
    )
    entry.add_to_hass(hass)
    coordinator = await _setup(hass, entry)
    assert coordinator.notification_targets() == ["persistent"]
