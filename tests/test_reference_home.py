"""Эталонный дом из 18 измерений: четыре уровня отчёта без HA и без DeepSeek."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from core.ai import AnalysisPackage, MeasurementFact, SystemSection, build_package
from core.history import Sample, Window
from core.model import (
    Equipment,
    HomeModel,
    Measurement,
    MeasurementRole,
    Site,
    SourceRef,
    System,
    SystemPreset,
)
from core.observations import Observation, Quality, build_observation
from core.reports import (
    DetailLevel,
    Locale,
    ReportProfile,
    history_facts,
    history_trend,
    pair_facts,
    render_report,
)

NOW = datetime(2026, 10, 30, 20, 0, tzinfo=UTC)
FIXTURE = Path(__file__).parent.parent / "docs" / "fixtures" / "reference_home_reports.json"

#: Эталонные значения дома из задания: ничего не выдумано.
VALUES: dict[str, tuple[str, float | bool, str | None, str]] = {
    "l1": ("Фаза L1", 240.0, "В", "electricity"),
    "l2": ("Фаза L2", 232.6, "В", "electricity"),
    "l3": ("Фаза L3", 227.2, "В", "electricity"),
    "energy_today": ("Электроэнергия сегодня", 108.7, "кВт·ч", "electricity"),
    "cost_today": ("Стоимость сегодня", 620.74, "₽", "electricity"),
    "outdoor": ("Улица", 0.6, "°C", "climate"),
    "floor1": ("Первый этаж", 24.3, "°C", "climate"),
    "floor2": ("Второй этаж", 23.5, "°C", "climate"),
    "boiler": ("Котёл", 57.8, "°C", "heating"),
    "boiler_target": ("Цель котла", 60.0, "°C", "heating"),
    "boiler_on": ("Котёл включён", True, None, "heating"),
    "supply": ("Подача", 59.2, "°C", "heating"),
    "return": ("Обратка", 57.8, "°C", "heating"),
    "dhw": ("ГВС", 42.6, "°C", "dhw"),
    "dhw_target": ("Цель ГВС", 45.0, "°C", "dhw"),
    "dhw_on": ("ГВС включена", True, None, "dhw"),
    "floor_heat": ("Тёплый пол", 24.3, "°C", "floor_heating"),
    "floor_target": ("Цель тёплого пола", 23.5, "°C", "floor_heating"),
    "floor_on": ("Тёплый пол включён", True, None, "floor_heating"),
    "floor_supply": ("Подача тёплого пола", 38.6, "°C", "floor_heating"),
    "radiators": ("Радиаторы", 23.5, "°C", "radiators"),
    "radiators_target": ("Цель радиаторов", 26.0, "°C", "radiators"),
    "radiators_on": ("Радиаторы включены", True, None, "radiators"),
    "radiators_return": ("Обратка радиаторов", 39.3, "°C", "radiators"),
}

ROLES: dict[str, MeasurementRole] = {
    "l1": MeasurementRole.VOLTAGE_PHASE,
    "l2": MeasurementRole.VOLTAGE_PHASE,
    "l3": MeasurementRole.VOLTAGE_PHASE,
    "energy_today": MeasurementRole.ENERGY,
    "cost_today": MeasurementRole.CUSTOM,
    "outdoor": MeasurementRole.TEMPERATURE_OUTDOOR,
    "floor1": MeasurementRole.TEMPERATURE_INDOOR,
    "floor2": MeasurementRole.TEMPERATURE_INDOOR,
    "boiler": MeasurementRole.ACTUAL,
    "boiler_target": MeasurementRole.SETPOINT,
    "boiler_on": MeasurementRole.RUNNING,
    "supply": MeasurementRole.SUPPLY,
    "return": MeasurementRole.RETURN,
    "dhw": MeasurementRole.ACTUAL,
    "dhw_target": MeasurementRole.SETPOINT,
    "dhw_on": MeasurementRole.RUNNING,
    "floor_heat": MeasurementRole.ACTUAL,
    "floor_target": MeasurementRole.SETPOINT,
    "floor_on": MeasurementRole.RUNNING,
    "floor_supply": MeasurementRole.SUPPLY,
    "radiators": MeasurementRole.ACTUAL,
    "radiators_target": MeasurementRole.SETPOINT,
    "radiators_on": MeasurementRole.RUNNING,
    "radiators_return": MeasurementRole.RETURN,
}

PRESETS = {
    "electricity": SystemPreset.ELECTRICITY,
    "climate": SystemPreset.CLIMATE,
    "heating": SystemPreset.HEATING,
    "dhw": SystemPreset.DHW,
    "floor_heating": SystemPreset.FLOOR_HEATING,
    "radiators": SystemPreset.RADIATORS,
}

SYSTEM_NAMES = {
    "electricity": "Электрика",
    "climate": "Климат",
    "heating": "Отопление",
    "dhw": "ГВС",
    "floor_heating": "Тёплый пол",
    "radiators": "Радиаторы",
}

EQUIPMENT = {
    "boiler": "Котёл",
    "supply": "Котёл",
    "return": "Котёл",
    "boiler_target": "Котёл",
    "boiler_on": "Котёл",
    "dhw": "Бойлер ГВС",
    "dhw_target": "Бойлер ГВС",
    "dhw_on": "Бойлер ГВС",
    "floor_heat": "Контур тёплого пола",
    "floor_target": "Контур тёплого пола",
    "floor_on": "Контур тёплого пола",
    "floor_supply": "Контур тёплого пола",
    "radiators": "Радиаторы",
    "radiators_target": "Радиаторы",
    "radiators_on": "Радиаторы",
    "radiators_return": "Радиаторы",
}


def build_reference_model() -> HomeModel:
    model = HomeModel()
    model.set_site(Site(id="site", name="Дом"))
    systems: dict[str, System] = {}
    for preset_name, preset in PRESETS.items():
        system = System(
            id=f"sys_{preset_name}",
            site_id="site",
            name=SYSTEM_NAMES[preset_name],
            preset=preset,
        )
        model.add_system(system)
        systems[preset_name] = system
    equipment: dict[str, Equipment] = {}
    for key, equipment_name in EQUIPMENT.items():
        if equipment_name in equipment:
            continue
        system_name = VALUES[key][3]
        equipment[equipment_name] = model.add_equipment(
            Equipment(
                id=f"eq_{len(equipment)}",
                system_id=systems[system_name].id,
                name=equipment_name,
            )
        )
    for key, (name, _value, unit, system_name) in VALUES.items():
        model.add_measurement(
            Measurement(
                id=key,
                name=name,
                role=ROLES[key],
                unit=unit,
                system_id=systems[system_name].id,
                equipment_id=equipment[EQUIPMENT[key]].id if key in EQUIPMENT else None,
                source=SourceRef(entity_id=f"sensor.reference_{key}"),
            )
        )
    return model


def build_reference_package(
    model: HomeModel,
    *,
    history: dict[str, list[Sample]] | None = None,
) -> tuple[AnalysisPackage, dict[str, Observation]]:
    observations = {}
    for key, (_name, value, unit, _system) in VALUES.items():
        raw = "on" if value is True else str(value)
        observations[key] = build_observation(
            source_id=key,
            entity_id=f"sensor.reference_{key}",
            raw_state=raw,
            now=NOW,
            unit=unit,
            expected_numeric=not isinstance(value, bool),
            last_reported=NOW,
        )
    window = Window.last(NOW, 1_800)
    sections: list[SystemSection] = []
    for system in model.systems.values():
        items = list(model.measurements_of(system.id))
        facts = list(pair_facts(items, observations))
        trend = None
        coverage_values: list[float] = []
        for measurement in items:
            if history is None or measurement.id not in history:
                continue
            samples = history[measurement.id]
            facts.extend(
                history_facts(
                    measurement.id,
                    measurement.name,
                    samples,
                    window,
                    measurement.unit,
                    counter=measurement.role is MeasurementRole.ENERGY,
                )
            )
            coverage, item_trend = history_trend(
                samples, window, measurement.unit, Locale.RU
            )
            if coverage is not None:
                coverage_values.append(coverage)
            if item_trend and trend is None:
                trend = item_trend
        measurement_facts = tuple(
            MeasurementFact.from_observation(
                measurement.id,
                measurement.name,
                measurement.role,
                observations[measurement.id],
                required=True,
            )
            for measurement in items
        )
        sections.append(
            SystemSection(
                id=system.id,
                name=system.name,
                measurements=measurement_facts,
                facts=tuple(facts),
                coverage=sum(coverage_values) / len(coverage_values)
                if coverage_values
                else None,
                trend=trend,
            )
        )
    package = build_package(
        site_id="site",
        now=NOW,
        window_start=NOW - timedelta(minutes=30),
        window_end=NOW,
        systems=sections,
    )
    return package, observations


def reference_profile(detail: DetailLevel) -> ReportProfile:
    return ReportProfile(
        id="reference",
        name="Дом",
        detail=detail,
        locale=Locale.RU,
        timezone="Europe/Samara",
        required_measurement_ids=tuple(VALUES),
        max_optional=0,
    )


def render_reference(detail: DetailLevel, *, history: dict[str, list[Sample]] | None = None):
    model = build_reference_model()
    package, _ = build_reference_package(model, history=history)
    return render_report(reference_profile(detail), package, NOW)


def _boiler_history() -> dict[str, list[Sample]]:
    start = NOW - timedelta(minutes=30)
    return {
        "supply": [
            Sample(start, 55.0, Quality.OK),
            Sample(start + timedelta(minutes=15), 57.0, Quality.OK),
            Sample(NOW, 59.2, Quality.OK),
        ]
    }


@pytest.mark.parametrize("detail", list(DetailLevel))
def test_reference_report_keeps_every_required_reading(detail: DetailLevel) -> None:
    rendered = render_reference(detail)
    compact = detail in {DetailLevel.COMPACT, DetailLevel.ADAPTIVE}

    for name, value, _unit, _system in VALUES.values():
        if isinstance(value, bool):
            assert name in rendered.text
        else:
            formatted = f"{value:.1f}".replace(".", ",")
            needle = f"{name} — {formatted}" if compact else f"{name}: {formatted}"
            assert needle in rendered.text, name


def test_reference_reports_do_not_invent_trends_without_history() -> None:
    for detail in DetailLevel:
        text = render_reference(detail).text
        assert "рост на" not in text
        assert "снижение на" not in text


def test_reference_detailed_report_uses_boiler_history() -> None:
    rendered = render_reference(DetailLevel.DETAILED, history=_boiler_history())

    assert "Подача: изменение за период: 4,2 °C" in rendered.text
    assert "рост на 4,2 °C" in rendered.text


def test_reference_adaptive_report_is_not_empty_without_incidents() -> None:
    rendered = render_reference(DetailLevel.ADAPTIVE)

    assert rendered.detail is DetailLevel.COMPACT
    assert "Ключевые показатели" in rendered.text
    assert "57,8" in rendered.text


def test_reference_systems_keep_their_own_values() -> None:
    text = render_reference(DetailLevel.DETAILED).text
    heating = text.split("• Отопление", 1)[1].split("•", 1)[0]
    dhw = text.split("• ГВС", 1)[1].split("•", 1)[0]

    assert "Котёл: 57,8" in heating
    assert "Подача: 59,2" in heating
    assert "Обратка: 57,8" in heating
    assert "Котёл − Цель котла: -2,2 °C" in heating
    assert "ГВС: 42,6" in dhw
    assert "ГВС − Цель ГВС: -2,4 °C" in dhw
    assert "ГВС: 42,6" not in heating


def test_reference_fixture_matches_current_reports() -> None:
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
    actual = {
        detail.value: render_reference(detail).text for detail in DetailLevel
    }

    assert actual == expected
