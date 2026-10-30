"""Семантическая модель дома (SEM-01…SEM-07).

Модель не привязана к конкретному дому: ``Site`` → ``System`` → ``Equipment``
→ ``Measurement``, плюс типизированные ``Relationship``. Внутренние
идентификаторы — UUID, названия HA ими не являются (SEM-02).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .errors import ModelError, SchemaError, UnitError
from .units import compatible, same_dimension

#: Домены HA, для которых числовые операции допустимы (SEM-06).
NUMERIC_DOMAINS = frozenset({"sensor", "number", "input_number"})
#: Домены HA, поддерживаемые моделью (SEM-06).
SUPPORTED_DOMAINS = frozenset(
    {
        "sensor",
        "binary_sensor",
        "climate",
        "water_heater",
        "switch",
        "input_boolean",
        "number",
        "input_number",
        "select",
        "input_select",
        "light",
        "cover",
        "alarm_control_panel",
    }
)


class MeasurementRole(StrEnum):
    """Роли измерений (SEM-03)."""

    ACTUAL = "actual"
    SETPOINT = "setpoint"
    SUPPLY = "supply"
    RETURN = "return"
    POWER = "power"
    ENERGY = "energy"
    VOLTAGE_PHASE = "voltage_phase"
    MODE = "mode"
    RUNNING = "running"
    FAULT = "fault"
    FLOW = "flow"
    PRESSURE = "pressure"
    TEMPERATURE_INDOOR = "temperature_indoor"
    TEMPERATURE_OUTDOOR = "temperature_outdoor"
    OCCUPANCY = "occupancy"
    LEAK = "leak"
    CUSTOM = "custom"


class RelationType(StrEnum):
    """Типы связей (SEM-04)."""

    BELONGS_TO = "belongs_to"
    MEASURES = "measures"
    SETPOINT_FOR = "setpoint_for"
    UPSTREAM_OF = "upstream_of"
    DOWNSTREAM_OF = "downstream_of"
    CONTROLS = "controls"
    CORRELATES_WITH = "correlates_with"
    SAME_CIRCUIT = "same_circuit"
    CUSTOM = "custom"


#: Связи, образующие направленный технологический поток: циклы запрещены.
DIRECTED_RELATIONS = frozenset(
    {
        RelationType.BELONGS_TO,
        RelationType.MEASURES,
        RelationType.SETPOINT_FOR,
        RelationType.UPSTREAM_OF,
        RelationType.DOWNSTREAM_OF,
        RelationType.CONTROLS,
    }
)


class ValidationStatus(StrEnum):
    """Статус проверки конфигурации объекта (SEM-02)."""

    UNVERIFIED = "unverified"
    CONFIRMED = "confirmed"
    NEEDS_ATTENTION = "needs_attention"
    BROKEN = "broken"


class SystemPreset(StrEnum):
    """Базовые пресеты систем (ENT-02)."""

    ELECTRICITY = "electricity"
    HEATING = "heating"
    DHW = "dhw"
    FLOOR_HEATING = "floor_heating"
    RADIATORS = "radiators"
    CLIMATE = "climate"
    WATER = "water"
    VENTILATION = "ventilation"
    SECURITY = "security"
    LIGHTING = "lighting"
    ENERGY = "energy"
    CUSTOM = "custom"


class CompositeOperation(StrEnum):
    """Разрешённые преобразования составного источника (SEM-07)."""

    SUM = "sum"
    DIFFERENCE = "difference"
    AVERAGE = "average"
    SCALE = "scale"


def new_identifier() -> str:
    """Новый внутренний идентификатор объекта."""
    return str(uuid.uuid4())


def _require_text(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SchemaError("ожидалось непустое имя", path)
    return value.strip()


@dataclass(frozen=True, slots=True)
class SourceRef:
    """Ссылка на источник Home Assistant (ENT-04).

    ``registry_entry_id`` — стабильная ссылка реестра, которая переживает
    переименование ``entity_id``; ``entity_id`` остаётся текущим fallback.
    """

    entity_id: str
    registry_entry_id: str | None = None
    attribute: str | None = None

    def __post_init__(self) -> None:
        if not self.entity_id or "." not in self.entity_id:
            raise ModelError(f"некорректный entity_id: {self.entity_id!r}")

    @property
    def domain(self) -> str:
        return self.entity_id.split(".", 1)[0]

    @property
    def numeric(self) -> bool:
        return self.domain in NUMERIC_DOMAINS and self.attribute is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "registry_entry_id": self.registry_entry_id,
            "attribute": self.attribute,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any], path: str = "source") -> SourceRef:
        if not isinstance(payload, dict):
            raise SchemaError("ожидался объект источника", path)
        return cls(
            entity_id=_require_text(payload.get("entity_id"), f"{path}.entity_id"),
            registry_entry_id=payload.get("registry_entry_id"),
            attribute=payload.get("attribute"),
        )


@dataclass(frozen=True, slots=True)
class Composite:
    """Арифметическое преобразование нескольких измерений (SEM-07)."""

    operation: CompositeOperation
    operands: tuple[str, ...]
    factor: float = 1.0

    def __post_init__(self) -> None:
        if len(self.operands) < 1:
            raise ModelError("составной источник требует хотя бы одного операнда")
        if self.operation is CompositeOperation.SCALE and len(self.operands) != 1:
            raise ModelError("операция scale принимает ровно один операнд")
        if self.operation is CompositeOperation.DIFFERENCE and len(self.operands) != 2:
            raise ModelError("операция difference принимает ровно два операнда")

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation.value,
            "operands": list(self.operands),
            "factor": self.factor,
        }


@dataclass(frozen=True, slots=True)
class Measurement:
    """Измерение: сущность HA или выражение из нескольких сущностей (SEM-01)."""

    id: str
    name: str
    role: MeasurementRole = MeasurementRole.CUSTOM
    unit: str | None = None
    system_id: str | None = None
    equipment_id: str | None = None
    description: str = ""
    tags: tuple[str, ...] = ()
    report_visible: bool = True
    config_status: ValidationStatus = ValidationStatus.UNVERIFIED
    source: SourceRef | None = None
    composite: Composite | None = None

    def __post_init__(self) -> None:
        if (self.source is None) == (self.composite is None):
            raise ModelError(
                f"измерение {self.name!r}: нужен ровно один источник — сущность или выражение"
            )

    @property
    def kind(self) -> str:
        """Тип данных измерения: numeric, state, unknown, composite."""
        if self.composite is not None:
            return "composite"
        assert self.source is not None
        if self.source.numeric:
            return "numeric"
        return "state" if self.source.domain in SUPPORTED_DOMAINS else "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "role": self.role.value,
            "unit": self.unit,
            "system_id": self.system_id,
            "equipment_id": self.equipment_id,
            "description": self.description,
            "tags": list(self.tags),
            "report_visible": self.report_visible,
            "config_status": self.config_status.value,
            "source": self.source.to_dict() if self.source else None,
            "composite": self.composite.to_dict() if self.composite else None,
        }


@dataclass(frozen=True, slots=True)
class Site:
    """Объект (дом, квартира, дача) — корень модели (SEM-01)."""

    id: str
    name: str
    description: str = ""
    tags: tuple[str, ...] = ()
    report_visible: bool = True
    config_status: ValidationStatus = ValidationStatus.UNVERIFIED

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "tags": list(self.tags),
            "report_visible": self.report_visible,
            "config_status": self.config_status.value,
        }


@dataclass(frozen=True, slots=True)
class System:
    """Система дома: отопление, электричество, безопасность и т. п."""

    id: str
    site_id: str
    name: str
    preset: SystemPreset = SystemPreset.CUSTOM
    description: str = ""
    tags: tuple[str, ...] = ()
    report_visible: bool = True
    config_status: ValidationStatus = ValidationStatus.UNVERIFIED

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "site_id": self.site_id,
            "name": self.name,
            "preset": self.preset.value,
            "description": self.description,
            "tags": list(self.tags),
            "report_visible": self.report_visible,
            "config_status": self.config_status.value,
        }


@dataclass(frozen=True, slots=True)
class Equipment:
    """Оборудование: котёл, контур, счётчик, насос (SEM-01)."""

    id: str
    system_id: str
    name: str
    kind: str = "generic"
    description: str = ""
    tags: tuple[str, ...] = ()
    report_visible: bool = True
    config_status: ValidationStatus = ValidationStatus.UNVERIFIED

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "system_id": self.system_id,
            "name": self.name,
            "kind": self.kind,
            "description": self.description,
            "tags": list(self.tags),
            "report_visible": self.report_visible,
            "config_status": self.config_status.value,
        }


@dataclass(frozen=True, slots=True)
class Relationship:
    """Типизированная связь между объектами модели (SEM-04)."""

    id: str
    type: RelationType
    source_id: str
    target_id: str

    def __post_init__(self) -> None:
        if self.source_id == self.target_id:
            raise ModelError("связь объекта с самим собой недопустима")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type.value,
            "source_id": self.source_id,
            "target_id": self.target_id,
        }


class HomeModel:
    """Контейнер модели с проверкой ссылок, канонических источников и циклов."""

    def __init__(self) -> None:
        self.site: Site | None = None
        self.systems: dict[str, System] = {}
        self.equipment: dict[str, Equipment] = {}
        self.measurements: dict[str, Measurement] = {}
        self._relationships: dict[str, Relationship] = {}

    # -- добавление ---------------------------------------------------
    def set_site(self, site: Site) -> Site:
        self.site = site
        return site

    def add_system(self, system: System) -> System:
        if self.site is None:
            raise ModelError("сначала нужен объект Site")
        if system.site_id != self.site.id:
            raise ModelError(f"система {system.name!r} ссылается на неизвестный объект")
        self.systems[system.id] = system
        return system

    def add_equipment(self, equipment: Equipment) -> Equipment:
        if equipment.system_id not in self.systems:
            raise ModelError(f"оборудование {equipment.name!r} ссылается на неизвестную систему")
        self.equipment[equipment.id] = equipment
        return equipment

    def add_measurement(self, measurement: Measurement) -> Measurement:
        if measurement.system_id is not None and measurement.system_id not in self.systems:
            raise ModelError(f"измерение {measurement.name!r} ссылается на неизвестную систему")
        if measurement.equipment_id is not None and measurement.equipment_id not in self.equipment:
            raise ModelError(f"измерение {measurement.name!r} ссылается на неизвестное оборудование")
        self._validate_composite(measurement)
        self._validate_canonical(measurement)
        self.measurements[measurement.id] = measurement
        return measurement

    def _validate_composite(self, measurement: Measurement) -> None:
        composite = measurement.composite
        if composite is None:
            return
        operands: list[Measurement] = []
        for index, operand_id in enumerate(composite.operands):
            operand = self.measurements.get(operand_id)
            if operand is None:
                raise ModelError(
                    f"измерение {measurement.name!r}: неизвестный операнд {operand_id!r} "
                    f"(позиция {index})"
                )
            if operand.kind not in {"numeric", "composite"}:
                raise ModelError(
                    f"измерение {measurement.name!r}: операнд {operand.name!r} не является числовым"
                )
            operands.append(operand)
        if composite.operation is CompositeOperation.SCALE:
            return
        units = {unit for unit in (operand.unit for operand in operands) if unit}
        if len(units) > 1 and not all(
            same_dimension(first, second) for first in units for second in units
        ):
            raise UnitError(
                f"измерение {measurement.name!r}: операнды в несопоставимых единицах {sorted(units)}"
            )
        if measurement.unit and units and not all(
            compatible(measurement.unit, unit) for unit in units
        ):
            raise UnitError(
                f"измерение {measurement.name!r}: единица {measurement.unit!r} не совпадает с операндами"
            )

    def _validate_canonical(self, measurement: Measurement) -> None:
        """Одна сущность — один канонический источник (SEM-05)."""
        if measurement.source is None:
            return
        for existing in self.measurements.values():
            if existing.id == measurement.id or existing.source is None:
                continue
            if existing.source.entity_id != measurement.source.entity_id:
                continue
            if existing.source.attribute == measurement.source.attribute:
                raise ModelError(
                    f"сущность {measurement.source.entity_id} уже используется измерением "
                    f"{existing.name!r}; у сущности может быть один канонический источник"
                )

    def add_relationship(self, relationship: Relationship) -> Relationship:
        if not self._object_exists(relationship.source_id):
            raise ModelError(f"связь ссылается на неизвестный объект {relationship.source_id!r}")
        if not self._object_exists(relationship.target_id):
            raise ModelError(f"связь ссылается на неизвестный объект {relationship.target_id!r}")
        if relationship.type in DIRECTED_RELATIONS and self._reaches(
            relationship.target_id, relationship.source_id
        ):
            raise ModelError(
                f"связь {relationship.type.value} образует цикл между "
                f"{relationship.source_id!r} и {relationship.target_id!r}"
            )
        self._relationships[relationship.id] = relationship
        return relationship

    # -- чтение -------------------------------------------------------
    @property
    def relationships(self) -> tuple[Relationship, ...]:
        return tuple(self._relationships.values())

    def _object_exists(self, object_id: str) -> bool:
        return (
            object_id in self.systems
            or object_id in self.equipment
            or object_id in self.measurements
            or (self.site is not None and object_id == self.site.id)
        )

    def _reaches(self, start: str, target: str) -> bool:
        graph: dict[str, list[str]] = {}
        for relationship in self._relationships.values():
            if relationship.type in DIRECTED_RELATIONS:
                graph.setdefault(relationship.source_id, []).append(relationship.target_id)
        stack = [start]
        seen: set[str] = set()
        while stack:
            current = stack.pop()
            if current == target:
                return True
            if current in seen:
                continue
            seen.add(current)
            stack.extend(graph.get(current, ()))
        return False

    def remove_measurement(self, measurement_id: str) -> Measurement:
        """Удалить измерение; составные источники, зависящие от него, блокируют удаление."""
        measurement = self.require_measurement(measurement_id)
        dependents = [
            other
            for other in self.measurements.values()
            if other.composite and measurement_id in other.composite.operands
        ]
        if dependents:
            names = ", ".join(item.name for item in dependents)
            raise ModelError(
                f"измерение {measurement.name!r} используется составными источниками: {names}"
            )
        self._relationships = {
            key: value
            for key, value in self._relationships.items()
            if measurement_id not in {value.source_id, value.target_id}
        }
        del self.measurements[measurement_id]
        return measurement

    def remove_system(self, system_id: str) -> System:
        """Удалить систему вместе с её оборудованием и измерениями.

        Удаляется только описание в модели: сами сущности Home Assistant не
        затрагиваются (SEM-05).
        """
        system = self.systems.get(system_id)
        if system is None:
            raise ModelError(f"неизвестная система {system_id!r}")
        for measurement in [item for item in self.measurements.values() if item.system_id == system_id]:
            self.remove_measurement(measurement.id)
        self.equipment = {
            key: value for key, value in self.equipment.items() if value.system_id != system_id
        }
        self._relationships = {
            key: value
            for key, value in self._relationships.items()
            if system_id not in {value.source_id, value.target_id}
        }
        del self.systems[system_id]
        return system

    def composite_dependents(self, measurement_id: str) -> tuple[Measurement, ...]:
        """Составные измерения, которые нельзя удалить без пересборки."""
        return tuple(
            other
            for other in self.measurements.values()
            if other.composite and measurement_id in other.composite.operands
        )

    def systems_at(self, site_id: str) -> tuple[System, ...]:
        return tuple(system for system in self.systems.values() if system.site_id == site_id)

    def measurements_of(self, system_id: str) -> tuple[Measurement, ...]:
        return tuple(
            measurement
            for measurement in self.measurements.values()
            if measurement.system_id == system_id
        )

    def measurement(self, measurement_id: str) -> Measurement | None:
        return self.measurements.get(measurement_id)

    def require_measurement(self, measurement_id: str) -> Measurement:
        measurement = self.measurements.get(measurement_id)
        if measurement is None:
            raise ModelError(f"неизвестное измерение {measurement_id!r}")
        return measurement

    def by_entity(self, entity_id: str) -> Measurement | None:
        for measurement in self.measurements.values():
            if measurement.source and measurement.source.entity_id == entity_id:
                return measurement
        return None

    def canonical_entity_index(self) -> dict[str, str]:
        return {
            measurement.source.entity_id: measurement.id
            for measurement in self.measurements.values()
            if measurement.source is not None
        }

    def broken_sources(self, known_entity_ids: Iterable[str]) -> tuple[Measurement, ...]:
        """Измерения, чьи сущности исчезли из реестра (ENT-04, AC-03)."""
        known = set(known_entity_ids)
        return tuple(
            measurement
            for measurement in self.measurements.values()
            if measurement.source is not None and measurement.source.entity_id not in known
        )

    # -- сериализация -------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "site": self.site.to_dict() if self.site else None,
            "systems": [system.to_dict() for system in self.systems.values()],
            "equipment": [item.to_dict() for item in self.equipment.values()],
            "measurements": [item.to_dict() for item in self.measurements.values()],
            "relationships": [item.to_dict() for item in self._relationships.values()],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> HomeModel:
        if not isinstance(payload, dict):
            raise SchemaError("модель дома должна быть объектом", "model")
        model = cls()
        site_payload = payload.get("site")
        if site_payload:
            model.set_site(
                Site(
                    id=_require_text(site_payload.get("id"), "site.id"),
                    name=_require_text(site_payload.get("name"), "site.name"),
                    description=site_payload.get("description", ""),
                    tags=tuple(site_payload.get("tags", ())),
                    report_visible=bool(site_payload.get("report_visible", True)),
                    config_status=ValidationStatus(site_payload.get("config_status", "unverified")),
                )
            )
        for system_payload in payload.get("systems", ()):
            model.add_system(
                System(
                    id=_require_text(system_payload.get("id"), "system.id"),
                    site_id=_require_text(system_payload.get("site_id"), "system.site_id"),
                    name=_require_text(system_payload.get("name"), "system.name"),
                    preset=SystemPreset(system_payload.get("preset", "custom")),
                    description=system_payload.get("description", ""),
                    tags=tuple(system_payload.get("tags", ())),
                    report_visible=bool(system_payload.get("report_visible", True)),
                    config_status=ValidationStatus(system_payload.get("config_status", "unverified")),
                )
            )
        for equipment_payload in payload.get("equipment", ()):
            model.add_equipment(
                Equipment(
                    id=_require_text(equipment_payload.get("id"), "equipment.id"),
                    system_id=_require_text(equipment_payload.get("system_id"), "equipment.system_id"),
                    name=_require_text(equipment_payload.get("name"), "equipment.name"),
                    kind=equipment_payload.get("kind", "generic"),
                    description=equipment_payload.get("description", ""),
                    tags=tuple(equipment_payload.get("tags", ())),
                    config_status=ValidationStatus(equipment_payload.get("config_status", "unverified")),
                )
            )
        for measurement_payload in payload.get("measurements", ()):
            source_payload = measurement_payload.get("source")
            composite_payload = measurement_payload.get("composite")
            composite = None
            if composite_payload:
                composite = Composite(
                    operation=CompositeOperation(composite_payload["operation"]),
                    operands=tuple(composite_payload.get("operands", ())),
                    factor=float(composite_payload.get("factor", 1.0)),
                )
            model.add_measurement(
                Measurement(
                    id=_require_text(measurement_payload.get("id"), "measurement.id"),
                    name=_require_text(measurement_payload.get("name"), "measurement.name"),
                    role=MeasurementRole(measurement_payload.get("role", "custom")),
                    unit=measurement_payload.get("unit"),
                    system_id=measurement_payload.get("system_id"),
                    equipment_id=measurement_payload.get("equipment_id"),
                    description=measurement_payload.get("description", ""),
                    tags=tuple(measurement_payload.get("tags", ())),
                    report_visible=bool(measurement_payload.get("report_visible", True)),
                    config_status=ValidationStatus(measurement_payload.get("config_status", "unverified")),
                    source=SourceRef.from_dict(source_payload) if source_payload else None,
                    composite=composite,
                )
            )
        for relationship_payload in payload.get("relationships", ()):
            model.add_relationship(
                Relationship(
                    id=_require_text(relationship_payload.get("id"), "relationship.id"),
                    type=RelationType(relationship_payload.get("type")),
                    source_id=_require_text(relationship_payload.get("source_id"), "relationship.source_id"),
                    target_id=_require_text(relationship_payload.get("target_id"), "relationship.target_id"),
                )
            )
        return model
