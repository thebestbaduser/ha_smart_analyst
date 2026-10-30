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
    result = await coordinator.async_send_test_notification("Проверка")
    assert result["ok"] is True
    assert result["results"][0]["target"] == "notify.telegram_bot_2130376715_181714455"
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
    result = await coordinator.async_send_test_notification("Проверка")
    assert result["ok"] is True
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

async def test_button_sends_and_reports_result(hass: HomeAssistant) -> None:
    from homeassistant.helpers import entity_registry as er

    hass.states.async_set("notify.robot", "idle")
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Дом",
        data={
            CONF_INSTANCE_NAME: "Дом",
            CONF_AI_ENABLED: False,
            CONF_TELEGRAM_SERVICE: "notify.robot",
        },
        options={},
    )
    entry.add_to_hass(hass)
    coordinator = await _setup(hass, entry)
    calls = async_mock_service(hass, "notify", "send_message")
    entity_id = er.async_get(hass).async_get_entity_id(
        "button", DOMAIN, f"{entry.entry_id}_test_notification"
    )
    assert entity_id is not None

    await hass.services.async_call("button", "press", {"entity_id": entity_id}, blocking=True)
    await hass.async_block_till_done()

    assert len(calls) == 1
    assert calls[0].data["entity_id"] == "notify.robot"
    assert coordinator.last_delivery is not None
    assert coordinator.last_delivery["ok"] is True
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.attributes["results"][0]["target"] == "notify.robot"


async def test_missing_channel_is_reported_as_failure(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Дом",
        data={
            CONF_INSTANCE_NAME: "Дом",
            CONF_AI_ENABLED: False,
            CONF_TELEGRAM_SERVICE: "notify.missing",
        },
        options={},
    )
    entry.add_to_hass(hass)
    coordinator = await _setup(hass, entry)

    result = await coordinator.async_send_test_notification("Проверка")
    assert result["ok"] is False
    assert result["results"][0]["sent"] is False
    assert "не найдена" in result["results"][0]["detail"]
