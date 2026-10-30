"""Сервисные действия интеграции (UI-07).

Все действия, меняющие настройки или состояние, выполняются только
администратором и не управляют устройствами (SEC-01).
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError

from .const import (
    DOMAIN,
    SERVICE_ACKNOWLEDGE_INCIDENT,
    SERVICE_EXPORT_CONFIGURATION,
    SERVICE_GENERATE_REPORT,
    SERVICE_REFRESH_ANALYSIS,
    SERVICE_RESOLVE_INCIDENT,
    SERVICE_RUN_RULE_TEST,
    SERVICE_TEST_NOTIFICATION,
)
from .coordinator import SmartAnalystCoordinator

ATTR_ENTRY_ID = "entry_id"
ATTR_PROFILE_ID = "profile_id"
ATTR_USE_AI = "use_ai"
ATTR_RECIPIENT = "recipient"
ATTR_MESSAGE = "message"
ATTR_RULE_ID = "rule_id"
ATTR_INCIDENT_ID = "incident_id"
ATTR_BY = "by"
ATTR_NOTE = "note"

_ENTRY_FIELD = {vol.Optional(ATTR_ENTRY_ID): str}


def _coordinator(hass: HomeAssistant, call: ServiceCall) -> SmartAnalystCoordinator:
    entries: dict[str, SmartAnalystCoordinator] = hass.data.get(DOMAIN, {})
    entry_id = call.data.get(ATTR_ENTRY_ID)
    if entry_id:
        coordinator = entries.get(entry_id)
        if coordinator is None:
            raise HomeAssistantError(f"неизвестный экземпляр {entry_id!r}")
        return coordinator
    if len(entries) == 1:
        return next(iter(entries.values()))
    if not entries:
        raise HomeAssistantError("HA Smart Analyst не настроен")
    raise HomeAssistantError("несколько экземпляров: укажите entry_id")


SERVICE_GENERATE_REPORT_SCHEMA = vol.Schema(
    {**_ENTRY_FIELD, vol.Optional(ATTR_PROFILE_ID): str, vol.Optional(ATTR_USE_AI): bool}
)
SERVICE_TEST_NOTIFICATION_SCHEMA = vol.Schema(
    {
        **_ENTRY_FIELD,
        vol.Optional(ATTR_RECIPIENT): str,
        vol.Optional(ATTR_MESSAGE, default="Проверка уведомлений HA Smart Analyst"): str,
    }
)
SERVICE_RULE_TEST_SCHEMA = vol.Schema({**_ENTRY_FIELD, vol.Required(ATTR_RULE_ID): str})
SERVICE_ACK_SCHEMA = vol.Schema(
    {
        **_ENTRY_FIELD,
        vol.Required(ATTR_INCIDENT_ID): str,
        vol.Optional(ATTR_BY, default="ha_user"): str,
    }
)
SERVICE_RESOLVE_SCHEMA = vol.Schema(
    {
        **_ENTRY_FIELD,
        vol.Required(ATTR_INCIDENT_ID): str,
        vol.Optional(ATTR_BY, default="ha_user"): str,
        vol.Optional(ATTR_NOTE, default=""): str,
    }
)
SERVICE_EXPORT_SCHEMA = vol.Schema(_ENTRY_FIELD)
SERVICE_REFRESH_SCHEMA = vol.Schema(_ENTRY_FIELD)


async def async_register_services(hass: HomeAssistant) -> None:
    """Зарегистрировать действия один раз на инстанс HA."""

    async def handle_generate_report(call: ServiceCall) -> ServiceResponse:
        coordinator = _coordinator(hass, call)
        rendered = await coordinator.async_generate_report(
            call.data.get(ATTR_PROFILE_ID), call.data.get(ATTR_USE_AI)
        )
        return {
            "text": rendered.text,
            "detail": rendered.detail.value,
            "used_ai": rendered.used_ai,
            "attention": rendered.attention.value,
        }

    async def handle_test_notification(call: ServiceCall) -> ServiceResponse:
        coordinator = _coordinator(hass, call)
        recipient = call.data.get(ATTR_RECIPIENT)
        text = call.data.get(ATTR_MESSAGE) or "Проверка уведомлений HA Smart Analyst"
        return await coordinator.async_send_test_notification(text, recipient)

    async def handle_run_rule_test(call: ServiceCall) -> ServiceResponse:
        coordinator = _coordinator(hass, call)
        evaluation = await coordinator.async_run_rule_test(call.data[ATTR_RULE_ID])
        return {
            "value": evaluation.value,
            "recovery": evaluation.recovery,
            "reasons": [
                {"code": reason.code, "message": reason.message, "measurement_id": reason.measurement_id}
                for reason in evaluation.reasons
            ],
            "deviation": evaluation.deviation,
        }

    async def handle_acknowledge(call: ServiceCall) -> ServiceResponse:
        coordinator = _coordinator(hass, call)
        incident = await coordinator.async_acknowledge(call.data[ATTR_INCIDENT_ID], call.data[ATTR_BY])
        return {"incident_id": incident.incident_id, "state": incident.state.value}

    async def handle_resolve(call: ServiceCall) -> ServiceResponse:
        coordinator = _coordinator(hass, call)
        incident = await coordinator.async_resolve_manually(
            call.data[ATTR_INCIDENT_ID], call.data[ATTR_BY], call.data[ATTR_NOTE]
        )
        return {"incident_id": incident.incident_id, "state": incident.state.value}

    async def handle_refresh(call: ServiceCall) -> None:
        coordinator = _coordinator(hass, call)
        await coordinator.async_request_refresh()

    async def handle_export(call: ServiceCall) -> ServiceResponse:
        coordinator = _coordinator(hass, call)
        return {"configuration": coordinator.export_configuration()}

    registrations: tuple[tuple[str, Any, Any, SupportsResponse], ...] = (
        (SERVICE_GENERATE_REPORT, handle_generate_report, SERVICE_GENERATE_REPORT_SCHEMA, SupportsResponse.OPTIONAL),
        (SERVICE_TEST_NOTIFICATION, handle_test_notification, SERVICE_TEST_NOTIFICATION_SCHEMA, SupportsResponse.OPTIONAL),
        (SERVICE_RUN_RULE_TEST, handle_run_rule_test, SERVICE_RULE_TEST_SCHEMA, SupportsResponse.OPTIONAL),
        (SERVICE_ACKNOWLEDGE_INCIDENT, handle_acknowledge, SERVICE_ACK_SCHEMA, SupportsResponse.OPTIONAL),
        (SERVICE_RESOLVE_INCIDENT, handle_resolve, SERVICE_RESOLVE_SCHEMA, SupportsResponse.OPTIONAL),
        (SERVICE_REFRESH_ANALYSIS, handle_refresh, SERVICE_REFRESH_SCHEMA, SupportsResponse.NONE),
        (SERVICE_EXPORT_CONFIGURATION, handle_export, SERVICE_EXPORT_SCHEMA, SupportsResponse.OPTIONAL),
    )
    for name, handler, schema, response in registrations:
        if hass.services.has_service(DOMAIN, name):
            continue
        hass.services.async_register(
            DOMAIN,
            name,
            handler,
            schema=schema,
            supports_response=response,
        )
