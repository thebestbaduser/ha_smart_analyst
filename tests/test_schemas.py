"""Тесты машинных контрактов этапа 0: схемы, фикстуры и парсеры ядра."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jsonschema
from core.ai import (
    Fact,
    MeasurementFact,
    SystemSection,
    build_package,
    validate_response,
)
from core.model import HomeModel, MeasurementRole
from core.observations import Observation, Quality, ValueType
from core.rules import parse_rule
from core.storage import validate_state

ROOT = Path(__file__).parents[1]
SCHEMAS = ROOT / "docs" / "schemas"
FIXTURES = ROOT / "docs" / "fixtures"
NOW = datetime(2026, 10, 30, 20, 0, tzinfo=UTC)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def schema(name: str) -> dict:
    return load_json(SCHEMAS / name)


def test_rule_fixture_matches_schema() -> None:
    jsonschema.validate(load_json(FIXTURES / "rule_voltage_l1_high.json"), schema("rule.schema.json"))


def test_ai_package_fixture_matches_schema() -> None:
    jsonschema.validate(
        load_json(FIXTURES / "ai_package_example.json"), schema("ai-package.schema.json")
    )


def test_ai_response_fixture_matches_schema() -> None:
    jsonschema.validate(
        load_json(FIXTURES / "ai_response_example.json"), schema("ai-response.schema.json")
    )


def test_model_fixture_parses_into_home_model() -> None:
    model = HomeModel.from_dict(load_json(FIXTURES / "home_model_example.json"))
    assert model.site is not None
    assert model.site.id == "site_main"
    assert model.require_measurement("voltage_l1").unit == "V"
    assert model.require_measurement("boiler_supply").role is MeasurementRole.SUPPLY


def test_rule_fixture_parses_with_notify_block() -> None:
    model = HomeModel.from_dict(load_json(FIXTURES / "home_model_example.json"))
    rule = parse_rule(load_json(FIXTURES / "rule_voltage_l1_high.json"), model)
    assert rule.for_seconds == 30
    assert rule.repeat_seconds == 3600
    assert rule.on_recovery is True
    assert rule.scope_id == "system_power"
    assert rule.recovery is not None
    assert rule.recovery.stable_seconds == 60


def test_configuration_fixture_validates() -> None:
    model = HomeModel.from_dict(load_json(FIXTURES / "home_model_example.json"))
    rule = load_json(FIXTURES / "rule_voltage_l1_high.json")
    configuration = {
        "schema_version": 1,
        "model": model.to_dict(),
        "rules": [rule],
        "report_profiles": [],
        "runtime": [],
        "incidents": [],
        "notifications": [],
        "settings": {"locale": "ru", "timezone": "Europe/Samara", "incident_retention_days": 90},
    }
    jsonschema.validate(configuration, schema("configuration.schema.json"))
    state = validate_state(configuration)
    assert state.model.measurements
    assert state.rules[0]["id"] == "rule_voltage_l1_high"


def build_package_from_fixture():
    option = Observation(
        source_id="boiler_supply",
        entity_id="sensor.boiler_supply",
        value=57.8,
        value_type=ValueType.NUMBER,
        unit="°C",
        observed_at=NOW,
        quality=Quality.OK,
    )
    setpoint = Observation(
        source_id="boiler_target",
        entity_id="number.boiler_target",
        value=60.0,
        value_type=ValueType.NUMBER,
        unit="°C",
        observed_at=NOW,
        quality=Quality.OK,
    )
    section = SystemSection(
        id="system_heating",
        name="Отопление",
        measurements=(
            MeasurementFact.from_observation("boiler_supply", "Подача котла", MeasurementRole.SUPPLY, option),
            MeasurementFact.from_observation("boiler_target", "Цель котла", MeasurementRole.SETPOINT, setpoint),
        ),
        facts=(Fact("fact_target_gap", "difference", -2.2, "°C", "Подача котла − Цель котла"),),
        coverage=0.0,
        trend=None,
    )
    return build_package(
        site_id="site_main",
        now=NOW,
        window_start=NOW - timedelta(minutes=30),
        window_end=NOW,
        systems=(section,),
    )


def test_package_builder_matches_fixture_contract() -> None:
    package = build_package_from_fixture()
    payload = json.loads(package.to_json())
    expected = load_json(FIXTURES / "ai_package_example.json")
    jsonschema.validate(payload, schema("ai-package.schema.json"))
    assert payload["systems"][0]["measurements"] == expected["systems"][0]["measurements"]
    assert payload["systems"][0]["facts"] == expected["systems"][0]["facts"]
    assert payload["constraints"] == expected["constraints"]


def test_response_fixture_passes_core_validation() -> None:
    response = load_json(FIXTURES / "ai_response_example.json")
    result = validate_response(response, build_package_from_fixture())
    assert result.ok, result.error_text
    assert result.response is not None
    assert result.response.unverified_numbers == ()
