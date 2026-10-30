"""Config Flow: мастер без YAML и установка без ключа ИИ (CFG-02, CFG-03)."""

from __future__ import annotations

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType

from custom_components.ha_smart_analyst.const import (
    CONF_AI_ENABLED,
    CONF_INSTANCE_NAME,
    CONF_LANGUAGE,
    CONF_TELEGRAM_RECIPIENTS,
    DOMAIN,
)


async def test_full_flow_without_ai_key(hass) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_INSTANCE_NAME: "Дом", CONF_LANGUAGE: "ru"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "ai"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_AI_ENABLED: False}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "telegram"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_TELEGRAM_RECIPIENTS: "127001, persistent"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Дом"
    assert result["data"][CONF_AI_ENABLED] is False
    assert result["data"][CONF_TELEGRAM_RECIPIENTS] == ["127001", "persistent"]


async def test_empty_name_is_rejected(hass) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_INSTANCE_NAME: "   "}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "name_required"}


async def test_ai_requires_key(hass) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_INSTANCE_NAME: "Дом"}
    )
    assert result["step_id"] == "ai"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_AI_ENABLED: True}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "api_key_required"}
