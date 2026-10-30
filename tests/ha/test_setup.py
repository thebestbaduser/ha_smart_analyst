"""Загрузка интеграции, службы, диагностика и выгрузка (AC-01, NFR-05)."""

from __future__ import annotations

import json

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_smart_analyst.const import (
    CONF_AI_ENABLED,
    CONF_INSTANCE_NAME,
    DOMAIN,
    SERVICE_ACKNOWLEDGE_INCIDENT,
    SERVICE_EXPORT_CONFIGURATION,
    SERVICE_GENERATE_REPORT,
    SERVICE_REFRESH_ANALYSIS,
    SERVICE_RESOLVE_INCIDENT,
    SERVICE_RUN_RULE_TEST,
    SERVICE_TEST_NOTIFICATION,
)
from custom_components.ha_smart_analyst.diagnostics import async_get_config_entry_diagnostics


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Дом",
        data={
            CONF_INSTANCE_NAME: "Дом",
            CONF_AI_ENABLED: False,
            "ai_api_key": "secret-should-not-leak",
        },
        options={},
    )
    entry.add_to_hass(hass)
    return entry


async def test_setup_registers_services_and_unloads(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    for service in (
        SERVICE_GENERATE_REPORT,
        SERVICE_TEST_NOTIFICATION,
        SERVICE_RUN_RULE_TEST,
        SERVICE_ACKNOWLEDGE_INCIDENT,
        SERVICE_RESOLVE_INCIDENT,
        SERVICE_REFRESH_ANALYSIS,
        SERVICE_EXPORT_CONFIGURATION,
    ):
        assert hass.services.has_service(DOMAIN, service)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_diagnostics_hide_secrets(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    dumped = json.dumps(diagnostics, ensure_ascii=False, default=str)
    assert "secret-should-not-leak" not in dumped
    assert diagnostics["entry"]["title"] == "Дом"
    assert diagnostics["snapshot"] is not None


async def test_export_configuration_service_returns_json_without_key(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_EXPORT_CONFIGURATION,
        {},
        blocking=True,
        return_response=True,
    )
    assert "secret-should-not-leak" not in response["configuration"]
    assert json.loads(response["configuration"])["schema_version"] == 1


async def test_refresh_analysis_runs(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await hass.services.async_call(DOMAIN, SERVICE_REFRESH_ANALYSIS, {}, blocking=True)
