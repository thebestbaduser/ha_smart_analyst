"""Автообнаружение и подсказки по сущностям (ENT-01…ENT-03).

Модуль работает с уже собранными дескрипторами реестра HA и не зависит от
Home Assistant. Подсказка — это **предположение**: тип системы и роль всегда
требуют подтверждения пользователем (ENT-02), а уверенность выражается числом
и текстовой причиной, а не выдаётся за факт.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from .model import SUPPORTED_DOMAINS, MeasurementRole, SystemPreset

#: Подсказки по именам: RU + EN, без привязки к конкретному дому.
_LEAK_WORDS = ("протеч", "leak", "вода на полу", "moisture")
_SMOKE_WORDS = ("дым", "smoke", "пожар", "gas", "газ", "co")
_FAULT_WORDS = ("авар", "fault", "ошибк", "error", "проблем", "problem")
_SUPPLY_WORDS = ("подач", "supply", "прям", "flow line")
_RETURN_WORDS = ("обрат", "return", "обратн")
_SETPOINT_WORDS = ("уставк", "цель", "target", "setpoint", "задан")
_OUTDOOR_WORDS = ("улиц", "outdoor", "наруж", "outside")
_INDOOR_WORDS = ("комнат", "indoor", "помещен", "room")
_BOILER_WORDS = ("котёл", "котел", "boiler")
_PUMP_WORDS = ("насос", "pump")
_FLOOR_WORDS = ("тёплый пол", "теплый пол", "floor heat")
_DHW_WORDS = ("гвс", "dhw", "горяч", "hot water")
_VENT_WORDS = ("вентил", "vent")

DOMAIN_PRESETS: Mapping[str, SystemPreset] = {
    "climate": SystemPreset.CLIMATE,
    "water_heater": SystemPreset.DHW,
    "light": SystemPreset.LIGHTING,
    "cover": SystemPreset.CUSTOM,
    "alarm_control_panel": SystemPreset.SECURITY,
}

DEVICE_CLASS_PRESETS: Mapping[str, SystemPreset] = {
    "temperature": SystemPreset.HEATING,
    "power": SystemPreset.ELECTRICITY,
    "energy": SystemPreset.ENERGY,
    "voltage": SystemPreset.ELECTRICITY,
    "current": SystemPreset.ELECTRICITY,
    "pressure": SystemPreset.HEATING,
    "leak": SystemPreset.WATER,
    "moisture": SystemPreset.WATER,
    "smoke": SystemPreset.SECURITY,
    "gas": SystemPreset.SECURITY,
    "carbon_monoxide": SystemPreset.SECURITY,
    "door": SystemPreset.SECURITY,
    "window": SystemPreset.SECURITY,
    "motion": SystemPreset.SECURITY,
    "problem": SystemPreset.SECURITY,
}


@dataclass(frozen=True, slots=True)
class EntityDescriptor:
    """Источник для каталога: реестр + текущее состояние (ENT-01)."""

    entity_id: str
    name: str = ""
    domain: str = ""
    unit: str | None = None
    device_class: str | None = None
    state_class: str | None = None
    area_id: str | None = None
    area_name: str | None = None
    device_id: str | None = None
    device_name: str | None = None
    registry_entry_id: str | None = None
    platform: str | None = None
    disabled: bool = False
    hidden: bool = False
    available: bool = True
    raw_state: str | None = None
    last_reported: datetime | None = None

    def __post_init__(self) -> None:
        if not self.entity_id:
            raise ValueError("entity_id обязателен")
        if not self.domain:
            object.__setattr__(self, "domain", self.entity_id.split(".", 1)[0])

    @property
    def supported(self) -> bool:
        return self.domain in SUPPORTED_DOMAINS

    @property
    def display_name(self) -> str:
        return self.name or self.entity_id

    def to_dict(self) -> dict[str, object]:
        return {
            "entity_id": self.entity_id,
            "name": self.display_name,
            "domain": self.domain,
            "unit": self.unit,
            "device_class": self.device_class,
            "state_class": self.state_class,
            "area_id": self.area_id,
            "area_name": self.area_name,
            "device_id": self.device_id,
            "device_name": self.device_name,
            "registry_entry_id": self.registry_entry_id,
            "platform": self.platform,
            "disabled": self.disabled,
            "hidden": self.hidden,
            "available": self.available,
            "raw_state": self.raw_state,
            "supported": self.supported,
        }


@dataclass(frozen=True, slots=True)
class Suggestion:
    """Предположение о системе и роли — всегда с причиной и уверенностью."""

    preset: SystemPreset
    role: MeasurementRole
    confidence: float
    reason: str
    suggested_name: str
    needs_confirmation: bool = True

    def to_dict(self) -> dict[str, object]:
        return {
            "preset": self.preset.value,
            "role": self.role.value,
            "confidence": round(self.confidence, 2),
            "reason": self.reason,
            "suggested_name": self.suggested_name,
            "needs_confirmation": self.needs_confirmation,
        }


def _lower(descriptor: EntityDescriptor) -> str:
    return f"{descriptor.entity_id} {descriptor.name}".lower()


def _contains(text: str, words: Sequence[str]) -> str | None:
    for word in words:
        if word in text:
            return word
    return None


def suggest(descriptor: EntityDescriptor) -> Suggestion:
    """Предложить систему и роль по метаданным и именам (ENT-02).

    Подсказка никогда не считается подтверждённой: ``needs_confirmation``
    всегда ``True``, а уверенность ниже единицы даже при точном ``device_class``.
    """
    text = _lower(descriptor)
    preset = SystemPreset.CUSTOM
    role = MeasurementRole.CUSTOM
    confidence = 0.2
    reasons: list[str] = []

    device_preset = DEVICE_CLASS_PRESETS.get(descriptor.device_class or "")
    if device_preset is not None:
        preset = device_preset
        confidence = max(confidence, 0.55)
        reasons.append(f"device_class={descriptor.device_class}")

    if descriptor.domain in DOMAIN_PRESETS:
        preset = DOMAIN_PRESETS[descriptor.domain]
        confidence = max(confidence, 0.6)
        reasons.append(f"домен {descriptor.domain}")

    if _contains(text, _LEAK_WORDS):
        preset, role = SystemPreset.WATER, MeasurementRole.LEAK
        confidence = max(confidence, 0.7)
        reasons.append("имя указывает на протечку")
    elif _contains(text, _SMOKE_WORDS):
        preset, role = SystemPreset.SECURITY, MeasurementRole.FAULT
        confidence = max(confidence, 0.7)
        reasons.append("имя указывает на пожар/газ")
    elif _contains(text, _FAULT_WORDS):
        role = MeasurementRole.FAULT
        confidence = max(confidence, 0.55)
        reasons.append("имя указывает на аварию")

    if _contains(text, _BOILER_WORDS):
        preset = SystemPreset.HEATING
        confidence = max(confidence, 0.65)
        reasons.append("имя указывает на котёл")
    elif _contains(text, _PUMP_WORDS):
        preset = SystemPreset.HEATING
        confidence = max(confidence, 0.6)
        reasons.append("имя указывает на насос")
    elif _contains(text, _FLOOR_WORDS):
        preset = SystemPreset.FLOOR_HEATING
        confidence = max(confidence, 0.7)
        reasons.append("имя указывает на тёплый пол")
    elif _contains(text, _DHW_WORDS):
        preset = SystemPreset.DHW
        confidence = max(confidence, 0.7)
        reasons.append("имя указывает на ГВС")
    elif _contains(text, _VENT_WORDS):
        preset = SystemPreset.VENTILATION
        confidence = max(confidence, 0.6)
        reasons.append("имя указывает на вентиляцию")

    if descriptor.device_class == "temperature":
        if _contains(text, _SUPPLY_WORDS):
            role = MeasurementRole.SUPPLY
            confidence = max(confidence, 0.75)
            reasons.append("подача")
        elif _contains(text, _RETURN_WORDS):
            role = MeasurementRole.RETURN
            confidence = max(confidence, 0.75)
            reasons.append("обратка")
        elif _contains(text, _OUTDOOR_WORDS):
            role = MeasurementRole.TEMPERATURE_OUTDOOR
            preset = SystemPreset.CLIMATE
            confidence = max(confidence, 0.75)
            reasons.append("уличная температура")
        elif _contains(text, _INDOOR_WORDS):
            role = MeasurementRole.TEMPERATURE_INDOOR
            preset = SystemPreset.CLIMATE
            confidence = max(confidence, 0.7)
            reasons.append("температура в помещении")
        else:
            role = MeasurementRole.ACTUAL
            confidence = max(confidence, 0.6)
            reasons.append("температура без уточнения роли")

    if descriptor.domain in {"number", "input_number"} or _contains(text, _SETPOINT_WORDS):
        role = MeasurementRole.SETPOINT
        confidence = max(confidence, 0.7)
        reasons.append("задающее значение")

    if descriptor.device_class == "power":
        role = MeasurementRole.POWER
        confidence = max(confidence, 0.8)
        reasons.append("мощность")
    elif descriptor.device_class == "energy":
        preset, role = SystemPreset.ENERGY, MeasurementRole.ENERGY
        confidence = max(confidence, 0.8)
        reasons.append("энергия")
    elif descriptor.device_class == "voltage":
        preset, role = SystemPreset.ELECTRICITY, MeasurementRole.VOLTAGE_PHASE
        confidence = max(confidence, 0.8)
        reasons.append("напряжение")
    elif descriptor.device_class == "current":
        preset, role = SystemPreset.ELECTRICITY, MeasurementRole.POWER
        confidence = max(confidence, 0.6)
        reasons.append("ток")
    elif descriptor.device_class == "pressure":
        role = MeasurementRole.PRESSURE
        confidence = max(confidence, 0.7)
        reasons.append("давление")

    if (
        descriptor.domain in {"switch", "input_boolean", "binary_sensor"}
        and role in {MeasurementRole.CUSTOM, MeasurementRole.FAULT}
        and _contains(text, _PUMP_WORDS + _BOILER_WORDS)
    ):
        role = MeasurementRole.RUNNING
        confidence = max(confidence, 0.7)
        reasons.append("состояние оборудования")

    if descriptor.domain == "climate":
        role = MeasurementRole.MODE
    if descriptor.domain == "water_heater":
        role = MeasurementRole.MODE

    if not descriptor.available:
        reasons.append("сейчас недоступна")
    if not descriptor.supported:
        reasons.append("домен вне поддерживаемого списка — только наблюдение")

    if not reasons:
        reasons.append("нет явных признаков, нужен ручной выбор")
    return Suggestion(
        preset=preset,
        role=role,
        confidence=min(confidence, 0.9),
        reason="; ".join(reasons),
        suggested_name=descriptor.display_name,
    )


@dataclass(frozen=True, slots=True)
class CatalogFilter:
    """Фильтр каталога (ENT-03)."""

    query: str | None = None
    domains: tuple[str, ...] = ()
    unit: str | None = None
    device_class: str | None = None
    area_id: str | None = None
    device_id: str | None = None
    available: bool | None = None
    include_disabled: bool = False
    include_unsupported: bool = True


def matches(descriptor: EntityDescriptor, criteria: CatalogFilter) -> bool:
    """Проверить одно описание по фильтру."""
    if not criteria.include_disabled and descriptor.disabled:
        return False
    if not criteria.include_unsupported and not descriptor.supported:
        return False
    if criteria.domains and descriptor.domain not in criteria.domains:
        return False
    if criteria.unit and (descriptor.unit or "").lower() != criteria.unit.lower():
        return False
    if criteria.device_class and (descriptor.device_class or "").lower() != criteria.device_class.lower():
        return False
    if criteria.area_id and descriptor.area_id != criteria.area_id:
        return False
    if criteria.device_id and descriptor.device_id != criteria.device_id:
        return False
    if criteria.available is not None and descriptor.available != criteria.available:
        return False
    if criteria.query:
        haystack = " ".join(
            filter(
                None,
                (
                    descriptor.entity_id,
                    descriptor.name,
                    descriptor.device_name,
                    descriptor.area_name,
                    descriptor.unit,
                ),
            )
        ).lower()
        if criteria.query.lower() not in haystack:
            return False
    return True


def search(
    descriptors: Iterable[EntityDescriptor], criteria: CatalogFilter | None = None, limit: int = 200
) -> list[EntityDescriptor]:
    """Отфильтровать каталог с ограничением размера ответа."""
    filter_value = criteria or CatalogFilter()
    found = [descriptor for descriptor in descriptors if matches(descriptor, filter_value)]
    found.sort(key=lambda item: (not item.available, item.domain, item.entity_id))
    return found[: max(0, limit)]


def suggest_many(
    descriptors: Iterable[EntityDescriptor], criteria: CatalogFilter | None = None, limit: int = 200
) -> list[tuple[EntityDescriptor, Suggestion]]:
    """Каталог с подсказками для UI."""
    return [(descriptor, suggest(descriptor)) for descriptor in search(descriptors, criteria, limit)]


def used_entity_ids(model_entities: Iterable[str]) -> set[str]:
    """Сущности, уже занятые каноническими источниками (SEM-05)."""
    return set(model_entities)


def unavailable_share(descriptors: Sequence[EntityDescriptor]) -> float:
    """Доля недоступных источников — метрика для вкладки «Диагностика»."""
    if not descriptors:
        return 0.0
    return sum(1 for descriptor in descriptors if not descriptor.available) / len(descriptors)


def group_by_device(descriptors: Iterable[EntityDescriptor]) -> dict[str, list[EntityDescriptor]]:
    """Сгруппировать по устройству: удобно добавлять комплектом (ENT-03)."""
    groups: dict[str, list[EntityDescriptor]] = {}
    for descriptor in descriptors:
        key = descriptor.device_id or "no-device"
        groups.setdefault(key, []).append(descriptor)
    return groups
