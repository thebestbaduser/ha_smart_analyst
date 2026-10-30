"""Тесты автообнаружения: подсказки, фильтры и метрики каталога."""

from __future__ import annotations

import pytest
from core.discovery import (
    CatalogFilter,
    EntityDescriptor,
    group_by_device,
    matches,
    search,
    suggest,
    unavailable_share,
)
from core.model import MeasurementRole, SystemPreset


def descriptor(**overrides) -> EntityDescriptor:
    payload = {
        "entity_id": "sensor.test",
        "name": "Тест",
        "unit": "°C",
        "available": True,
    }
    payload.update(overrides)
    return EntityDescriptor(**payload)


@pytest.mark.parametrize(
    ("overrides", "preset", "role"),
    [
        ({"entity_id": "binary_sensor.leak", "name": "Протечка", "unit": None}, SystemPreset.WATER, MeasurementRole.LEAK),
        (
            {"entity_id": "sensor.l1", "name": "Напряжение L1", "device_class": "voltage", "unit": "V"},
            SystemPreset.ELECTRICITY,
            MeasurementRole.VOLTAGE_PHASE,
        ),
        (
            {"entity_id": "sensor.boiler_supply", "name": "Подача котла", "device_class": "temperature"},
            SystemPreset.HEATING,
            MeasurementRole.SUPPLY,
        ),
        (
            {"entity_id": "sensor.boiler_return", "name": "Обратка котла", "device_class": "temperature"},
            SystemPreset.HEATING,
            MeasurementRole.RETURN,
        ),
        (
            {"entity_id": "sensor.outdoor", "name": "Улица", "device_class": "temperature"},
            SystemPreset.CLIMATE,
            MeasurementRole.TEMPERATURE_OUTDOOR,
        ),
        (
            {"entity_id": "number.boiler_target", "name": "Цель котла", "unit": "°C"},
            SystemPreset.HEATING,
            MeasurementRole.SETPOINT,
        ),
        (
            {"entity_id": "sensor.power", "name": "Мощность", "device_class": "power", "unit": "W"},
            SystemPreset.ELECTRICITY,
            MeasurementRole.POWER,
        ),
        (
            {"entity_id": "sensor.energy", "name": "Энергия", "device_class": "energy", "unit": "kWh"},
            SystemPreset.ENERGY,
            MeasurementRole.ENERGY,
        ),
        (
            {"entity_id": "switch.pump", "name": "Насос", "unit": None},
            SystemPreset.HEATING,
            MeasurementRole.RUNNING,
        ),
        (
            {"entity_id": "sensor.smoke", "name": "Дым", "device_class": "smoke", "unit": None},
            SystemPreset.SECURITY,
            MeasurementRole.FAULT,
        ),
        (
            {"entity_id": "climate.living", "name": "Гостиная", "unit": None},
            SystemPreset.CLIMATE,
            MeasurementRole.MODE,
        ),
    ],
)
def test_suggestions(overrides, preset, role) -> None:
    suggestion = suggest(descriptor(**overrides))
    assert suggestion.preset is preset
    assert suggestion.role is role
    assert 0.0 < suggestion.confidence < 1.0
    assert suggestion.needs_confirmation is True
    assert suggestion.reason


def test_unknown_entity_is_not_asserted() -> None:
    suggestion = suggest(descriptor(entity_id="sensor.mystery", name="Нечто", unit=""))
    assert suggestion.preset is SystemPreset.CUSTOM
    assert suggestion.role is MeasurementRole.CUSTOM
    assert suggestion.confidence <= 0.3
    assert suggestion.needs_confirmation is True


def test_unsupported_domain_flagged() -> None:
    suggestion = suggest(descriptor(entity_id="camera.yard", name="Двор", unit=None))
    assert "наблюдение" in suggestion.reason or "поддерживаемого" in suggestion.reason


def test_unavailable_flag_in_reason() -> None:
    suggestion = suggest(descriptor(available=False))
    assert "недоступна" in suggestion.reason


def test_filters() -> None:
    items = [
        descriptor(entity_id="sensor.a", name="Подача", device_class="temperature", area_id="boiler"),
        descriptor(entity_id="sensor.b", name="Обратка", device_class="temperature", area_id="boiler", available=False),
        descriptor(entity_id="sensor.c", name="Напряжение", device_class="voltage", unit="V"),
    ]
    assert [item.entity_id for item in search(items, CatalogFilter(query="подача"))] == ["sensor.a"]
    assert {item.entity_id for item in search(items, CatalogFilter(domains=("sensor",), area_id="boiler"))} == {
        "sensor.a",
        "sensor.b",
    }
    assert [item.entity_id for item in search(items, CatalogFilter(available=True))] == [
        "sensor.a",
        "sensor.c",
    ]
    assert [item.entity_id for item in search(items, CatalogFilter(unit="V"))] == ["sensor.c"]
    assert search(items, CatalogFilter(device_class="temperature"), limit=1)
    assert len(search(items, CatalogFilter(device_class="temperature"))) == 2


def test_search_puts_available_first_and_limits() -> None:
    items = [
        descriptor(entity_id="sensor.z", available=False),
        descriptor(entity_id="sensor.a"),
        descriptor(entity_id="sensor.b"),
    ]
    found = search(items, limit=2)
    assert [item.entity_id for item in found] == ["sensor.a", "sensor.b"]


def test_disabled_hidden_by_default() -> None:
    items = [descriptor(entity_id="sensor.off", disabled=True)]
    assert search(items) == []
    assert len(search(items, CatalogFilter(include_disabled=True))) == 1


def test_matches_requires_each_criterion() -> None:
    item = descriptor(entity_id="sensor.x", name="Подача", device_class="temperature", area_id="heating")
    assert matches(item, CatalogFilter(query="подача", device_class="temperature", area_id="heating"))
    assert not matches(item, CatalogFilter(area_id="power"))


def test_unavailable_share_and_groups() -> None:
    items = [
        descriptor(entity_id="sensor.a", device_id="dev1"),
        descriptor(entity_id="sensor.b", device_id="dev1", available=False),
        descriptor(entity_id="sensor.c"),
    ]
    assert unavailable_share(items) == pytest.approx(1 / 3)
    assert unavailable_share([]) == 0.0
    groups = group_by_device(items)
    assert len(groups["dev1"]) == 2
    assert len(groups["no-device"]) == 1
