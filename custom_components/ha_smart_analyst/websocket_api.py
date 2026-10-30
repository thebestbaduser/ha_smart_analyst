"""WebSocket API для панели интеграции (UI-01).

Модель не получает свободного доступа к HA API: панель запрашивает только
подготовленные срезы данных, а тест правила выполняется на сервере.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import SmartAnalystCoordinator
from .core.observations import Quality


def _first_coordinator(hass: HomeAssistant) -> SmartAnalystCoordinator | None:
    entries: dict[str, SmartAnalystCoordinator] = hass.data.get(DOMAIN, {})
    return next(iter(entries.values())) if entries else None


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
        },
        "history": {
            "available": snapshot.recorder_available if snapshot else False,
        },
        "diagnostics": {
            "measurements_total": snapshot.measurements_total if snapshot else 0,
            "stale_sources": snapshot.stale_sources if snapshot else 0,
        },
    }


@callback
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/overview"})
def websocket_overview(hass: HomeAssistant, connection, msg) -> None:
    """Срез данных для вкладок панели."""
    coordinator = _first_coordinator(hass)
    if coordinator is None:
        connection.send_error(msg["id"], "not_found", "Интеграция не настроена")
        return
    connection.send_result(msg["id"], _overview_payload(coordinator))


@websocket_api.async_response
@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/rule_test", vol.Required("rule_id"): str}
)
async def websocket_rule_test(hass: HomeAssistant, connection, msg) -> None:
    """Тест правила на текущем снимке (UI-03)."""
    coordinator = _first_coordinator(hass)
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


async def async_register_websocket_api(hass: HomeAssistant) -> None:
    """Зарегистрировать команды один раз."""
    if hass.data.get(f"{DOMAIN}_ws"):
        return
    websocket_api.async_register_command(hass, websocket_overview)
    websocket_api.async_register_command(hass, websocket_rule_test)
    hass.data[f"{DOMAIN}_ws"] = True
