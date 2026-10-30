"""Тесты семантической модели и схемы правил."""

from __future__ import annotations

import pytest
from core.errors import ModelError, SchemaError, UnitError
from core.model import (
    HomeModel,
    Measurement,
    MeasurementRole,
    Relationship,
    RelationType,
    Site,
    SourceRef,
    System,
    SystemPreset,
)
from core.rules import (
    MissingDataPolicy,
    Severity,
    parse_rule,
    referenced_measurements,
)


def build_model() -> HomeModel:
    model = HomeModel()
    model.set_site(Site(id="site", name="Дом"))
    model.add_system(System(id="power", site_id="site", name="Электрика", preset=SystemPreset.ELECTRICITY))
    model.add_system(System(id="heating", site_id="site", name="Отопление", preset=SystemPreset.HEATING))
    model.add_measurement(
        Measurement(
            id="voltage_l1",
            name="Напряжение L1",
            role=MeasurementRole.VOLTAGE_PHASE,
            unit="V",
            system_id="power",
            source=SourceRef("sensor.l1_voltage"),
        )
    )
    model.add_measurement(
        Measurement(
            id="boiler_temp",
            name="Подача котла",
            role=MeasurementRole.SUPPLY,
            unit="°C",
            system_id="heating",
            source=SourceRef("sensor.boiler_supply"),
        )
    )
    model.add_measurement(
        Measurement(
            id="leak",
            name="Протечка",
            role=MeasurementRole.LEAK,
            system_id="heating",
            source=SourceRef("binary_sensor.leak"),
        )
    )
    return model


VOLTAGE_RULE = {
    "id": "rule_voltage_l1_high",
    "name": "Перенапряжение L1",
    "severity": "warning",
    "expression": {
        "all": [
            {"op": "gt", "left": {"measurement_id": "voltage_l1"}, "right": {"value": 245, "unit": "V"}},
            {"op": "duration_true", "seconds": 30},
        ]
    },
    "recovery": {"clear_below": {"value": 242, "unit": "V"}, "stable_seconds": 60, "measurement_id": "voltage_l1"},
    "missing_data_policy": "unknown",
    "notify": {"repeat_seconds": 3600, "on_recovery": True},
}


def test_repository_example_rule_parses() -> None:
    model = build_model()
    payload = dict(VOLTAGE_RULE)
    payload.pop("notify")
    rule = parse_rule(payload, model)
    assert rule.for_seconds == 30
    assert rule.severity is Severity.WARNING
    assert rule.recovery is not None
    assert rule.recovery.clear_below is not None
    assert rule.recovery.clear_below.value == pytest.approx(242)
    assert rule.missing_data_policy is MissingDataPolicy.UNKNOWN
    assert referenced_measurements(rule.expression) == ("voltage_l1",)


def test_rule_without_duration_defaults_to_zero() -> None:
    model = build_model()
    payload = {
        "name": "Протечка",
        "expression": {"op": "state_is", "measurement_id": "leak", "state": "on"},
        "severity": "critical",
    }
    rule = parse_rule(payload, model)
    assert rule.for_seconds == 0
    assert rule.severity is Severity.CRITICAL


def test_unknown_measurement_rejected() -> None:
    model = build_model()
    payload = {
        "name": "Битое правило",
        "expression": {"op": "gt", "left": {"measurement_id": "nope"}, "right": {"value": 1}},
    }
    with pytest.raises(SchemaError):
        parse_rule(payload, model)


def test_incompatible_units_rejected() -> None:
    model = build_model()
    payload = {
        "name": "Температура больше напряжения",
        "expression": {
            "op": "gt",
            "left": {"measurement_id": "boiler_temp"},
            "right": {"value": 240, "unit": "V"},
        },
    }
    with pytest.raises(UnitError):
        parse_rule(payload, model)


def test_extra_fields_rejected() -> None:
    model = build_model()
    payload = {
        "name": "Опечатка",
        "expresssion": {"op": "state_is", "measurement_id": "leak", "state": "on"},
    }
    with pytest.raises(SchemaError):
        parse_rule(payload, model)


def test_duration_true_only_inside_root_all() -> None:
    model = build_model()
    payload = {
        "name": "Неверное место duration",
        "expression": {
            "any": [
                {"op": "state_is", "measurement_id": "leak", "state": "on"},
                {"op": "duration_true", "seconds": 30},
            ]
        },
    }
    with pytest.raises(SchemaError):
        parse_rule(payload, model)


def test_two_duration_sources_rejected() -> None:
    model = build_model()
    payload = {
        "name": "Двойная длительность",
        "for_seconds": 30,
        "expression": {
            "all": [
                {"op": "state_is", "measurement_id": "leak", "state": "on"},
                {"op": "duration_true", "seconds": 30},
            ]
        },
    }
    with pytest.raises(SchemaError):
        parse_rule(payload, model)


def test_canonical_source_conflict() -> None:
    model = build_model()
    duplicate = Measurement(
        id="voltage_l1_copy",
        name="Дубль",
        unit="V",
        system_id="power",
        source=SourceRef("sensor.l1_voltage"),
    )
    with pytest.raises(ModelError):
        model.add_measurement(duplicate)


def test_composite_operands_must_be_compatible() -> None:
    from core.model import Composite, CompositeOperation

    model = build_model()
    with pytest.raises(UnitError):
        model.add_measurement(
            Measurement(
                id="bad_sum",
                name="Сумма напряжения и температуры",
                unit="V",
                system_id="power",
                composite=Composite(CompositeOperation.SUM, ("voltage_l1", "boiler_temp")),
            )
        )


def test_directed_cycle_rejected() -> None:
    model = build_model()
    model.add_relationship(Relationship("r1", RelationType.UPSTREAM_OF, "boiler_temp", "leak"))
    with pytest.raises(ModelError):
        model.add_relationship(Relationship("r2", RelationType.UPSTREAM_OF, "leak", "boiler_temp"))


def test_undirected_relationship_may_repeat() -> None:
    model = build_model()
    model.add_relationship(Relationship("r1", RelationType.CORRELATES_WITH, "boiler_temp", "leak"))
    model.add_relationship(Relationship("r2", RelationType.CORRELATES_WITH, "leak", "boiler_temp"))
    assert len(model.relationships) == 2


def test_model_round_trip() -> None:
    model = build_model()
    model.add_relationship(Relationship("r1", RelationType.MEASURES, "boiler_temp", "heating"))
    restored = HomeModel.from_dict(model.to_dict())
    assert set(restored.measurements) == set(model.measurements)
    assert len(restored.relationships) == 1
    assert restored.require_measurement("voltage_l1").unit == "V"


def test_broken_sources_detected() -> None:
    model = build_model()
    broken = model.broken_sources({"sensor.l1_voltage"})
    assert {item.id for item in broken} == {"boiler_temp", "leak"}
