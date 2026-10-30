"""Профиль отчёта, обязательные показатели, покрытие и возраст данных."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from core.ai import Fact, MeasurementFact, SystemSection, build_package, trim_package
from core.history import Sample, Window
from core.model import (
    HomeModel,
    Measurement,
    MeasurementRole,
    Site,
    SourceRef,
    System,
    SystemPreset,
)
from core.observations import Quality, ValueType, build_observation
from core.reports import (
    DetailLevel,
    ReportProfile,
    compact_groups,
    compact_measurements,
    history_facts,
    render_report,
    system_coverage,
)
from core.storage import ConfigState, load_config

NOW = datetime(2026, 10, 30, 20, 0, tzinfo=UTC)


def fact(
    measurement_id: str,
    *,
    value: float | None = 20.0,
    required: bool = False,
    quality: Quality = Quality.OK,
    role: MeasurementRole = MeasurementRole.CUSTOM,
) -> MeasurementFact:
    return MeasurementFact(
        id=measurement_id,
        name=f"Датчик {measurement_id}",
        role=role,
        value=value,
        unit="°C",
        quality=quality,
        required=required,
    )


def test_profile_roundtrip_keeps_required_measurements() -> None:
    profile = ReportProfile(
        id="p",
        name="Отчёт",
        required_measurement_ids=("m1", "m2"),
        optional_measurement_ids=("m3",),
        measurement_order=("m2", "m1", "m3"),
        max_optional=3,
        group_by_system=False,
        show_states=False,
    )

    restored = ReportProfile.from_dict(profile.to_dict())

    assert restored == profile


def test_old_profile_without_new_fields_gets_defaults() -> None:
    restored = ReportProfile.from_dict({"id": "old", "name": "Старый"})

    assert restored.required_measurement_ids == ()
    assert restored.max_optional == 8
    assert restored.group_by_system is True


def test_compact_report_keeps_all_required_measurements_beyond_limit() -> None:
    required = [fact(f"m{index}", required=True) for index in range(12)]
    optional = [fact(f"extra{index}") for index in range(3)]
    section = SystemSection(id="s", name="Дом", measurements=tuple(required + optional))
    profile = ReportProfile(id="p", name="Короткий", max_optional=8)

    selected = compact_measurements([section], profile)

    assert len(selected) == 15
    assert {measurement.id for _, measurement in selected} >= {f"m{i}" for i in range(12)}


def test_compact_limit_applies_only_to_optional() -> None:
    required = [fact("req1", required=True), fact("req2", required=True)]
    optional = [fact(f"opt{index}") for index in range(10)]
    section = SystemSection(id="s", name="Дом", measurements=tuple(required + optional))
    profile = ReportProfile(id="p", name="Короткий", max_optional=3)

    selected = compact_measurements([section], profile)

    assert len(selected) == 5
    assert {measurement.id for _, measurement in selected} == {"req1", "req2", "opt0", "opt1", "opt2"}


def test_missing_required_measurement_is_shown_with_status() -> None:
    missing = fact("lost", value=None, required=True, quality=Quality.UNCONFIGURED)
    section = SystemSection(id="profile", name="Показатели профиля", measurements=(missing,))
    profile = ReportProfile(id="p", name="Отчёт", detail=DetailLevel.COMPACT, max_optional=0)
    package = build_package(
        site_id="site",
        now=NOW,
        window_start=NOW - timedelta(minutes=30),
        window_end=NOW,
        systems=[section],
    )

    rendered = render_report(profile, package, NOW)

    assert "Датчик lost" in rendered.text
    assert "не настроено" in rendered.text


def test_system_coverage_is_average_not_best_sensor() -> None:
    assert system_coverage([0.95, 0.2]) == pytest.approx(0.575)
    assert system_coverage([]) is None


def test_counter_history_uses_total_and_notes_resets() -> None:
    window = Window.last(NOW, 1_800)
    samples = [
        Sample(NOW - timedelta(minutes=20), 100.0, Quality.OK),
        Sample(NOW - timedelta(minutes=10), 150.0, Quality.OK),
        Sample(NOW, 30.0, Quality.OK),
    ]

    facts = history_facts("energy", "Энергия", samples, window, "кВт·ч", counter=True)
    ids = {item.id: item for item in facts}

    assert ids["fact_energy_total"].value == pytest.approx(80.0)
    assert ids["fact_energy_resets"].value == 1
    assert "fact_energy_delta" not in ids


def test_trim_package_keeps_required_measurement() -> None:
    required = fact("req", required=True)
    optional = [fact(f"opt{index}") for index in range(10)]
    section = SystemSection(
        id="s",
        name="Дом",
        measurements=tuple([required, *optional]),
        facts=(Fact("fact_req_delta", "delta", 1.0, "°C", "замер"),),
    )
    package = build_package(
        site_id="site",
        now=NOW,
        window_start=NOW - timedelta(minutes=30),
        window_end=NOW,
        systems=[section],
    )

    trimmed = trim_package(package, max_measurements=2, max_facts=0)

    assert [item.id for item in trimmed.systems[0].measurements] == ["req", "opt0"]
    assert trimmed.systems[0].facts == ()


def test_max_age_marks_stale_but_missing_timestamp_is_not_faked() -> None:
    stale = build_observation(
        source_id="m",
        entity_id="sensor.temp",
        raw_state="20",
        now=NOW,
        last_updated=NOW - timedelta(minutes=10),
        max_age=timedelta(seconds=300),
    )
    fresh = build_observation(
        source_id="m",
        entity_id="sensor.temp",
        raw_state="20",
        now=NOW,
        last_updated=NOW - timedelta(seconds=30),
        max_age=timedelta(seconds=300),
    )
    no_timestamp = build_observation(
        source_id="m",
        entity_id="sensor.temp",
        raw_state="20",
        now=NOW,
        max_age=timedelta(seconds=300),
    )

    assert stale.quality is Quality.STALE
    assert fresh.quality is Quality.OK
    assert no_timestamp.quality is Quality.OK
    assert no_timestamp.value_type is ValueType.NUMBER


def test_measurement_max_age_survives_serialization() -> None:
    model = HomeModel()
    model.set_site(Site(id="site", name="Дом"))
    model.add_system(System(id="s", site_id="site", name="Отопление", preset=SystemPreset.HEATING))
    model.add_measurement(
        Measurement(
            id="m",
            name="Подача",
            system_id="s",
            source=SourceRef(entity_id="sensor.temp"),
            max_age_seconds=300,
        )
    )

    restored = HomeModel.from_dict(model.to_dict())

    assert restored.measurements["m"].max_age_seconds == 300

def test_config_state_uses_runtime_key_and_migrates_legacy() -> None:
    payload = ConfigState(runtime=[{"rule_id": "r", "state": "active"}]).to_dict()
    assert payload["runtime"] == [{"rule_id": "r", "state": "active"}]
    assert "runtimes" not in payload

    payload["runtimes"] = payload.pop("runtime")
    state, _backup = load_config(json.dumps(payload, ensure_ascii=False), NOW)

    assert state.runtime == [{"rule_id": "r", "state": "active"}]

def test_compact_report_marks_hidden_optional_readings() -> None:
    section = SystemSection(
        id="s",
        name="Дом",
        measurements=(
            fact("req", required=True),
            fact("opt1"),
            fact("opt2"),
            fact("opt3"),
        ),
    )
    profile = ReportProfile(
        id="p", name="Короткий", detail=DetailLevel.COMPACT, max_optional=1
    )
    package = build_package(
        site_id="site",
        now=NOW,
        window_start=NOW - timedelta(minutes=30),
        window_end=NOW,
        systems=[section],
    )

    rendered = render_report(profile, package, NOW)

    assert "Не показано из-за лимита: 2" in rendered.text

def test_compact_groups_pair_equipment_and_can_be_disabled() -> None:
    actual = MeasurementFact(
        id="actual",
        name="Котёл",
        role=MeasurementRole.ACTUAL,
        value=57.8,
        unit="°C",
        quality=Quality.OK,
        required=True,
        equipment_name="Котёл",
        system_preset="heating",
    )
    setpoint = MeasurementFact(
        id="setpoint",
        name="Цель котла",
        role=MeasurementRole.SETPOINT,
        value=60.0,
        unit="°C",
        quality=Quality.OK,
        required=True,
        equipment_name="Котёл",
        system_preset="heating",
    )
    section = SystemSection(
        id="s", name="Отопление", measurements=(actual, setpoint)
    )

    grouped = compact_groups([section], ReportProfile(id="p", name="Отчёт"))
    single = compact_groups(
        [section], ReportProfile(id="p", name="Отчёт", compact_grouping=False)
    )

    assert len(grouped) == 1
    assert grouped[0][0] == "🔥"
    assert len(grouped[0][1]) == 2
    assert len(single) == 2
