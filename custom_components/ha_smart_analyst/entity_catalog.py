"""Каталог сущностей Home Assistant (ENT-01, ENT-04).

Слой только собирает описания из реестров и состояний; подсказки и фильтры —
в ``core.discovery``. Переименование сущности отслеживается через стабильный
``registry_entry_id``.
"""

from __future__ import annotations

from collections.abc import Iterable

from homeassistant.core import HomeAssistant

from .core.discovery import EntityDescriptor
from .core.model import HomeModel

_UNAVAILABLE_STATES = frozenset({"unavailable", "unknown"})


def _registry_name(entry: object) -> str | None:
    for attribute in ("name", "original_name"):
        value = getattr(entry, attribute, None)
        if value:
            return str(value)
    return None


def build_catalog(hass: HomeAssistant) -> list[EntityDescriptor]:
    """Собрать каталог: реестр сущностей/устройств/зон плюс состояния."""
    from homeassistant.helpers import area_registry as ar
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    area_registry = ar.async_get(hass)

    catalog: dict[str, EntityDescriptor] = {}
    for entry in entity_registry.entities.values():
        state = hass.states.get(entry.entity_id)
        device = device_registry.async_get(entry.device_id) if entry.device_id else None
        area_id = entry.area_id or (device.area_id if device else None)
        area = area_registry.async_get_area(area_id) if area_id else None
        catalog[entry.entity_id] = EntityDescriptor(
            entity_id=entry.entity_id,
            name=_registry_name(entry) or (state.name if state else entry.entity_id),
            domain=entry.domain,
            unit=getattr(entry, "unit_of_measurement", None)
            or (state.attributes.get("unit_of_measurement") if state else None),
            device_class=getattr(entry, "device_class", None)
            or getattr(entry, "original_device_class", None)
            or (state.attributes.get("device_class") if state else None),
            state_class=getattr(entry, "state_class", None)
            or getattr(entry, "original_state_class", None)
            or (state.attributes.get("state_class") if state else None),
            area_id=area_id,
            area_name=area.name if area else None,
            device_id=entry.device_id,
            device_name=(device.name_by_user or device.name) if device else None,
            registry_entry_id=entry.id,
            platform=entry.platform,
            disabled=bool(entry.disabled_by),
            hidden=bool(entry.hidden_by),
            available=state is not None and state.state not in _UNAVAILABLE_STATES,
            raw_state=state.state if state else None,
            last_reported=getattr(state, "last_reported", None) if state else None,
        )

    for state in hass.states.async_all():
        if state.entity_id in catalog:
            continue
        catalog[state.entity_id] = EntityDescriptor(
            entity_id=state.entity_id,
            name=state.name or state.entity_id,
            unit=state.attributes.get("unit_of_measurement"),
            device_class=state.attributes.get("device_class"),
            state_class=state.attributes.get("state_class"),
            available=state.state not in _UNAVAILABLE_STATES,
            raw_state=state.state,
            last_reported=getattr(state, "last_reported", None),
        )
    return list(catalog.values())


def model_entity_index(model: HomeModel) -> dict[str, str]:
    """Сущности, уже занятые моделью: entity_id → measurement_id."""
    return model.canonical_entity_index()


def unused_catalog(
    descriptors: Iterable[EntityDescriptor], model: HomeModel
) -> list[EntityDescriptor]:
    """Только ещё не добавленные в модель сущности (ENT-03)."""
    used = set(model_entity_index(model))
    return [descriptor for descriptor in descriptors if descriptor.entity_id not in used]
