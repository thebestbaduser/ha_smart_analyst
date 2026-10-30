"""WebSocket API панели: просмотр, каталог и редактирование (UI-01…UI-04).

Чтение — обзор и каталог сущностей. Изменения (системы, измерения, правила,
профили) выполняются только администратором и проходят серверную валидацию:
панель не может записать в хранилище правило, не прошедшее ``parse_rule``.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import SmartAnalystCoordinator
from .core import CatalogFilter, SmartAnalystError, search, suggest
from .core.observations import Quality
from .entity_catalog import build_catalog

ATTR_ENTRY_ID = "entry_id"


def _coordinator(hass: HomeAssistant, msg: dict[str, Any]) -> SmartAnalystCoordinator | None:
    entries: dict[str, SmartAnalystCoordinator] = hass.data.get(DOMAIN, {})
    entry_id = msg.get(ATTR_ENTRY_ID)
    if entry_id:
        return entries.get(str(entry_id))
    return next(iter(entries.values())) if entries else None


def _fail(connection: Any, msg: dict[str, Any], err: Exception) -> None:
    connection.send_error(msg["id"], "invalid_request", str(err))


def _overview_payload(coordinator: SmartAnalystCoordinator) -> dict[str, Any]:
    snapshot = coordinator.data
    now = snapshot.generated_at if snapshot else dt_util.utcnow()
    observations = coordinator.build_observations(now)
    rule_names = {rule_id: rule.name for rule_id, rule in coordinator.rules.items()}
    systems = []
    for system in coordinator.model.systems.values():
        systems.append(
            {
                "id": system.id,
                "name": system.name,
                "preset": system.preset.value,
                "measurements": [
                    {
                        "id": measurement.id,
                        "name": measurement.name,
                        "role": measurement.role.value,
                        "unit": measurement.unit,
                        "value": observations[measurement.id].value
                        if measurement.id in observations
                        else None,
                        "quality": observations[measurement.id].quality.value
                        if measurement.id in observations
                        else Quality.UNCONFIGURED.value,
                        "entity_id": measurement.source.entity_id if measurement.source else None,
                    }
                    for measurement in coordinator.model.measurements_of(system.id)
                ],
            }
        )
    incidents = [
        {
            "incident_id": incident.incident_id,
            "rule": rule_names.get(incident.rule_id, incident.rule_id),
            "severity": incident.severity.value,
            "state": incident.state.value,
            "opened_at": incident.opened_at.isoformat(),
            "duration_seconds": incident.duration_seconds,
            "peak_deviation": incident.peak_deviation,
            "peak_unit": incident.peak_unit,
            "acknowledged": incident.ack_at is not None,
            "data_quality": incident.data_quality,
        }
        for incident in coordinator.incidents.incidents
    ]
    return {
        "overview": {
            "generated_at": snapshot.generated_at.isoformat() if snapshot else None,
            "rules_total": snapshot.rules_total if snapshot else 0,
            "rules_active": snapshot.rules_active if snapshot else 0,
            "active_incidents": snapshot.active_incidents if snapshot else 0,
            "critical_incidents": snapshot.critical_incidents if snapshot else 0,
            "queue_pending": snapshot.queue_pending if snapshot else 0,
            "stale_sources": snapshot.stale_sources if snapshot else 0,
            "ai_available": snapshot.ai_available if snapshot else False,
            "ai_error": snapshot.ai_error if snapshot else None,
            "recorder_available": snapshot.recorder_available if snapshot else False,
            "tokens_used": snapshot.tokens_used if snapshot else 0,
        },
        "systems": systems,
        "rules": [
            {
                "id": rule.id,
                "name": rule.name,
                "severity": rule.severity.value,
                "enabled": rule.enabled,
                "for_seconds": rule.for_seconds,
                "missing_data_policy": rule.missing_data_policy.value,
                "state": coordinator.runtimes.get(rule.id).state.value
                if coordinator.runtimes.get(rule.id)
                else "idle",
            }
            for rule in coordinator.rules.values()
        ],
        "incidents": incidents,
        "relationships": [
            {
                "id": relationship.id,
                "type": relationship.type.value,
                "source_id": relationship.source_id,
                "target_id": relationship.target_id,
            }
            for relationship in coordinator.model.relationships
        ],
        "reports": [profile for profile in coordinator.config_state.report_profiles],
        "ai": {
            "enabled": bool(coordinator.config_state.settings.get("ai_enabled", True)),
            "model": coordinator.config_state.settings.get("ai_model"),
            "available": snapshot.ai_available if snapshot else False,
            "error": snapshot.ai_error if snapshot else None,
        },
        "telegram": {
            "recipients": coordinator.config_state.settings.get("telegram_recipients", []),
            "service": coordinator.config_state.settings.get("telegram_service"),
            "targets": coordinator.notification_targets(),
        },
        "history": {
            "available": snapshot.recorder_available if snapshot else False,
        },
        "diagnostics": {
            "measurements_total": snapshot.measurements_total if snapshot else 0,
            "stale_sources": snapshot.stale_sources if snapshot else 0,
        },
    }


# -- чтение -----------------------------------------------------------
@callback
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/overview"})
def websocket_overview(hass: HomeAssistant, connection, msg) -> None:
    """Срез данных для вкладок панели."""
    coordinator = _coordinator(hass, msg)
    if coordinator is None:
        connection.send_error(msg["id"], "not_found", "Интеграция не настроена")
        return
    connection.send_result(msg["id"], _overview_payload(coordinator))


ENTITIES_SCHEMA = {
    vol.Required("type"): f"{DOMAIN}/entities",
    vol.Optional("query"): str,
    vol.Optional("domains"): [str],
    vol.Optional("unit"): str,
    vol.Optional("device_class"): str,
    vol.Optional("area_id"): str,
    vol.Optional("device_id"): str,
    vol.Optional("available"): bool,
    vol.Optional("include_disabled", default=False): bool,
    vol.Optional("only_unused", default=True): bool,
    vol.Optional("limit", default=200): int,
}


@websocket_api.require_admin
@callback
@websocket_api.websocket_command(ENTITIES_SCHEMA)
def websocket_entities(hass: HomeAssistant, connection, msg) -> None:
    """Каталог сущностей с подсказками (ENT-01…ENT-03)."""
    coordinator = _coordinator(hass, msg)
    if coordinator is None:
        connection.send_error(msg["id"], "not_found", "Интеграция не настроена")
        return
    catalog = build_catalog(hass)
    criteria = CatalogFilter(
        query=msg.get("query"),
        domains=tuple(msg.get("domains") or ()),
        unit=msg.get("unit"),
        device_class=msg.get("device_class"),
        area_id=msg.get("area_id"),
        device_id=msg.get("device_id"),
        available=msg.get("available"),
        include_disabled=bool(msg.get("include_disabled", False)),
    )
    found = search(catalog, criteria, limit=int(msg.get("limit", 200)))
    used = set(coordinator.model.canonical_entity_index())
    if msg.get("only_unused", True):
        found = [item for item in found if item.entity_id not in used]
    connection.send_result(
        msg["id"],
        {
            "entities": [
                {**descriptor.to_dict(), "suggestion": suggest(descriptor).to_dict()}
                for descriptor in found
            ],
            "total": len(catalog),
            "unavailable": sum(1 for item in catalog if not item.available),
        },
    )


@websocket_api.async_response
@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/rule_test", vol.Required("rule_id"): str}
)
async def websocket_rule_test(hass: HomeAssistant, connection, msg) -> None:
    """Тест правила на текущем снимке (UI-03)."""
    coordinator = _coordinator(hass, msg)
    if coordinator is None:
        connection.send_error(msg["id"], "not_found", "Интеграция не настроена")
        return
    try:
        evaluation = await coordinator.async_run_rule_test(msg["rule_id"])
    except Exception as err:  # noqa: BLE001 - понятная ошибка вместо 500
        connection.send_error(msg["id"], "rule_test_failed", str(err))
        return
    connection.send_result(
        msg["id"],
        {
            "value": evaluation.value,
            "recovery": evaluation.recovery,
            "deviation": evaluation.deviation,
            "reasons": [
                {"code": reason.code, "message": reason.message, "measurement_id": reason.measurement_id}
                for reason in evaluation.reasons
            ],
        },
    )


# -- системы и измерения ----------------------------------------------
@websocket_api.require_admin
@websocket_api.async_response
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/system/create",
        vol.Required("name"): str,
        vol.Optional("preset", default="custom"): str,
    }
)
async def websocket_system_create(hass: HomeAssistant, connection, msg) -> None:
    coordinator = _coordinator(hass, msg)
    if coordinator is None:
        connection.send_error(msg["id"], "not_found", "Интеграция не настроена")
        return
    try:
        system = await coordinator.async_create_system(msg["name"], msg.get("preset", "custom"))
    except SmartAnalystError as err:
        _fail(connection, msg, err)
        return
    connection.send_result(msg["id"], {"system": system.to_dict()})


@websocket_api.require_admin
@websocket_api.async_response
@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/system/delete", vol.Required("system_id"): str}
)
async def websocket_system_delete(hass: HomeAssistant, connection, msg) -> None:
    coordinator = _coordinator(hass, msg)
    if coordinator is None:
        connection.send_error(msg["id"], "not_found", "Интеграция не настроена")
        return
    try:
        await coordinator.async_delete_system(msg["system_id"])
    except SmartAnalystError as err:
        _fail(connection, msg, err)
        return
    connection.send_result(msg["id"], {"deleted": True})


@websocket_api.require_admin
@websocket_api.async_response
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/measurement/add",
        vol.Required("system_id"): str,
        vol.Required("entity_id"): str,
        vol.Optional("name"): str,
        vol.Optional("role", default="custom"): str,
        vol.Optional("unit"): vol.Any(str, None),
        vol.Optional("registry_entry_id"): vol.Any(str, None),
        vol.Optional("attribute"): vol.Any(str, None),
    }
)
async def websocket_measurement_add(hass: HomeAssistant, connection, msg) -> None:
    coordinator = _coordinator(hass, msg)
    if coordinator is None:
        connection.send_error(msg["id"], "not_found", "Интеграция не настроена")
        return
    try:
        measurement = await coordinator.async_add_measurement(
            system_id=msg["system_id"],
            entity_id=msg["entity_id"],
            name=msg.get("name"),
            role=msg.get("role", "custom"),
            unit=msg.get("unit"),
            registry_entry_id=msg.get("registry_entry_id"),
            attribute=msg.get("attribute"),
        )
    except SmartAnalystError as err:
        _fail(connection, msg, err)
        return
    connection.send_result(msg["id"], {"measurement": measurement.to_dict()})


@websocket_api.require_admin
@websocket_api.async_response
@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/measurement/delete", vol.Required("measurement_id"): str}
)
async def websocket_measurement_delete(hass: HomeAssistant, connection, msg) -> None:
    coordinator = _coordinator(hass, msg)
    if coordinator is None:
        connection.send_error(msg["id"], "not_found", "Интеграция не настроена")
        return
    try:
        await coordinator.async_delete_measurement(msg["measurement_id"])
    except SmartAnalystError as err:
        _fail(connection, msg, err)
        return
    connection.send_result(msg["id"], {"deleted": True})


# -- правила и профили ------------------------------------------------
@websocket_api.require_admin
@websocket_api.async_response
@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/rule/save", vol.Required("rule"): dict}
)
async def websocket_rule_save(hass: HomeAssistant, connection, msg) -> None:
    coordinator = _coordinator(hass, msg)
    if coordinator is None:
        connection.send_error(msg["id"], "not_found", "Интеграция не настроена")
        return
    try:
        rule = await coordinator.async_save_rule(dict(msg["rule"]))
    except SmartAnalystError as err:
        _fail(connection, msg, err)
        return
    connection.send_result(msg["id"], {"rule": rule.to_dict()})


@websocket_api.require_admin
@websocket_api.async_response
@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/rule/delete", vol.Required("rule_id"): str}
)
async def websocket_rule_delete(hass: HomeAssistant, connection, msg) -> None:
    coordinator = _coordinator(hass, msg)
    if coordinator is None:
        connection.send_error(msg["id"], "not_found", "Интеграция не настроена")
        return
    try:
        await coordinator.async_delete_rule(msg["rule_id"])
    except SmartAnalystError as err:
        _fail(connection, msg, err)
        return
    connection.send_result(msg["id"], {"deleted": True})


@websocket_api.require_admin
@websocket_api.async_response
@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/profile/save", vol.Required("profile"): dict}
)
async def websocket_profile_save(hass: HomeAssistant, connection, msg) -> None:
    coordinator = _coordinator(hass, msg)
    if coordinator is None:
        connection.send_error(msg["id"], "not_found", "Интеграция не настроена")
        return
    try:
        profile = await coordinator.async_save_profile(dict(msg["profile"]))
    except (SmartAnalystError, ValueError) as err:
        _fail(connection, msg, err)
        return
    connection.send_result(msg["id"], {"profile": profile.to_dict()})


@websocket_api.require_admin
@websocket_api.async_response
@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/profile/delete", vol.Required("profile_id"): str}
)
async def websocket_profile_delete(hass: HomeAssistant, connection, msg) -> None:
    coordinator = _coordinator(hass, msg)
    if coordinator is None:
        connection.send_error(msg["id"], "not_found", "Интеграция не настроена")
        return
    try:
        await coordinator.async_delete_profile(msg["profile_id"])
    except SmartAnalystError as err:
        _fail(connection, msg, err)
        return
    connection.send_result(msg["id"], {"deleted": True})


COMMANDS = (
    websocket_overview,
    websocket_entities,
    websocket_rule_test,
    websocket_system_create,
    websocket_system_delete,
    websocket_measurement_add,
    websocket_measurement_delete,
    websocket_rule_save,
    websocket_rule_delete,
    websocket_profile_save,
    websocket_profile_delete,
)


async def async_register_websocket_api(hass: HomeAssistant) -> None:
    """Зарегистрировать команды один раз."""
    if hass.data.get(f"{DOMAIN}_ws"):
        return
    for command in COMMANDS:
        websocket_api.async_register_command(hass, command)
    hass.data[f"{DOMAIN}_ws"] = True
