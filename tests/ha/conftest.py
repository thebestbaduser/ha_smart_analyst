"""Окружение интеграционных тестов Home Assistant."""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_smart_analyst.const import CONF_AI_ENABLED, CONF_INSTANCE_NAME, DOMAIN


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Разрешить загрузку custom_components в тестовом HA."""
    yield


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    """Настроенный экземпляр без ключа ИИ (CFG-03)."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Дом",
        data={CONF_INSTANCE_NAME: "Дом", CONF_AI_ENABLED: False},
        options={},
    )
    entry.add_to_hass(hass)
    return entry
