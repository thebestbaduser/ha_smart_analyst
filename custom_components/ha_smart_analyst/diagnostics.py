"""Диагностика конфигурации без секретов и персональных данных (NFR-06)."""

from __future__ import annotations

import json
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import SmartAnalystCoordinator
from .core.storage import strip_secrets


def _snapshot(coordinator: SmartAnalystCoordinator) -> dict[str, Any] | None:
    data = coordinator.data
    if data is None:
        return None
    return {
        "generated_at": data.generated_at.isoformat(),
        "rules_total": data.rules_total,
        "rules_active": data.rules_active,
        "active_incidents": data.active_incidents,
        "critical_incidents": data.critical_incidents,
        "queue_pending": data.queue_pending,
        "stale_sources": data.stale_sources,
        "ai_available": data.ai_available,
        "ai_error": data.ai_error,
        "recorder_available": data.recorder_available,
        "tokens_used": data.tokens_used,
        "measurements_total": data.measurements_total,
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Экспорт диагностики: без ключей, переписок и координат."""
    coordinator: SmartAnalystCoordinator = hass.data[DOMAIN][entry.entry_id]
    return strip_secrets(
        {
            "entry": {
                "title": entry.title,
                "data": dict(entry.data),
                "options": dict(entry.options),
                "version": entry.version,
            },
            "settings": dict(coordinator.config_state.settings),
            "snapshot": _snapshot(coordinator),
            "rules": [rule.to_dict() for rule in coordinator.rules.values()],
            "incidents": coordinator.incidents.to_list(),
            "queue": coordinator.queue.to_list(),
            "model": coordinator.model.to_dict(),
            "configuration": json.loads(coordinator.export_configuration()),
        }
    )
