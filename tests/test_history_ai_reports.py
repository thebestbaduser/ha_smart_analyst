"""Тесты истории, пакета ИИ и отчётов."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from core.ai import (
    Fact,
    MeasurementFact,
    RetryAction,
    SystemSection,
    apply_attention_floor,
    build_package,
    classify_status,
    parse_payload,
    validate_response,
)
from core.errors import SchemaError
from core.history import (
    Sample,
    Window,
    counter_total,
    integrate_energy,
    summarize,
    switch_count,
    time_above,
    trend,
)
from core.incidents import Incident, IncidentState, digest
from core.model import MeasurementRole
from core.observations import Observation, Quality, ValueType
from core.reports import (
    DetailLevel,
    Locale,
    ReportProfile,
    choose_detail,
    critical_alert,
    history_facts,
    history_trend,
    phase_spread,
    render_report,
)
from core.rules import Severity as RuleSeverity

NOW = datetime(2026, 10, 30, 20, 0, tzinfo=UTC)


def samples(values: list[float], start: datetime, step_seconds: int) -> list[Sample]:
    return [Sample(start + timedelta(seconds=index * step_seconds), value) for index, value in enumerate(values)]


def test_summarize_reports_insufficient_history() -> None:
    window = Window.last(NOW, 3_600)
    result = summarize(samples([20.0, 21.0], NOW - timedelta(seconds=60), 60), window)
    assert result.reason is not None
    assert result.delta == pytest.approx(1.0)
    assert result.coverage < 0.1


def test_trend_full_coverage() -> None:
    window = Window.last(NOW, 600)
    data = samples([20.0 + index for index in range(11)], NOW - timedelta(seconds=600), 60)
    result = trend(data, window)
    assert result.usable
    assert result.delta == pytest.approx(10.0)
    assert result.coverage.ratio == pytest.approx(1.0)


def test_counter_total_handles_resets() -> None:
    data = samples([100.0, 150.0, 5.0, 10.0], NOW - timedelta(hours=3), 3_600)
    total, resets = counter_total(data)
    assert total == pytest.approx(60.0)
    assert resets == 1


def test_energy_integration_requires_density() -> None:
    window = Window.last(NOW, 3_600)
    dense = samples([1000.0] * 7, NOW - timedelta(seconds=3_600), 600)
    assert integrate_energy(dense, window) == pytest.approx(1000.0)
    sparse = samples([1000.0, 1000.0], NOW - timedelta(seconds=3_600), 600)
    assert integrate_energy(sparse, window) is None


def test_time_above_and_switch_count() -> None:
    window = Window.last(NOW, 600)
    data = samples([10.0, 20.0, 10.0, 20.0, 20.0, 20.0, 20.0, 20.0, 20.0, 20.0, 20.0], NOW - timedelta(seconds=600), 60)
    assert switch_count(data, 15.0) == 3
    assert time_above(data, window, 15.0) == pytest.approx(480.0)


def build_section() -> SystemSection:
    observation = Observation(
        source_id="boiler_temp",
        entity_id="sensor.boiler_supply",
        value=57.8,
        value_type=ValueType.NUMBER,
        unit="°C",
        observed_at=NOW,
        quality=Quality.OK,
    )
    target = Observation(
        source_id="boiler_target",
        entity_id="number.boiler_target",
        value=60.0,
        value_type=ValueType.NUMBER,
        unit="°C",
        observed_at=NOW,
        quality=Quality.OK,
    )
    measurement = MeasurementFact.from_observation(
        "boiler_temp", "Подача котла", MeasurementRole.SUPPLY, observation
    )
    setpoint = MeasurementFact.from_observation(
        "boiler_target", "Цель котла", MeasurementRole.SETPOINT, target
    )
    return SystemSection(
        id="heating",
        name="Отопление",
        measurements=(measurement, setpoint),
        facts=(Fact("fact_target_gap", "difference", -2.2, "°C", "Отклонение от цели"),),
        coverage=0.0,
        trend=None,
    )


def build_analysis_package():
    return build_package(
        site_id="site_main",
        now=NOW,
        window_start=NOW - timedelta(minutes=30),
        window_end=NOW,
        systems=(build_section(),),
    )


GOOD_RESPONSE = {
    "summary": "Температура котла ниже заданной на 2,2 °C. Динамика нагрева недоступна.",
    "attention_level": "info",
    "observations": [
        {"text": "Цель 60,0 °C, текущее значение 57,8 °C", "evidence_refs": ["fact_target_gap"]}
    ],
    "hypotheses": [],
    "recommendations": [],
    "uncertainties": ["Исторические данные за отчётное окно отсутствуют"],
    "evidence_refs": ["fact_target_gap"],
}


def test_valid_ai_response_accepted() -> None:
    package = build_analysis_package()
    result = validate_response(GOOD_RESPONSE, package)
    assert result.ok, result.error_text
    assert result.response is not None
    assert result.response.attention_level == "info"
    assert result.response.unverified_numbers == ()


def test_unresolved_evidence_rejected() -> None:
    package = build_analysis_package()
    payload = dict(GOOD_RESPONSE)
    payload["evidence_refs"] = ["fact_missing"]
    result = validate_response(payload, package)
    assert not result.ok
    assert "неразрешённые evidence_refs" in result.error_text


def test_extra_response_fields_rejected() -> None:
    package = build_analysis_package()
    payload = dict(GOOD_RESPONSE)
    payload["debug"] = "лишнее"
    result = validate_response(payload, package)
    assert not result.ok


def test_unverified_numbers_are_marked_not_silently_kept() -> None:
    package = build_analysis_package()
    payload = dict(GOOD_RESPONSE)
    payload["summary"] = "Температура упала на 7,5 °C"
    result = validate_response(payload, package)
    assert result.ok
    assert result.response is not None
    assert "7,5" in result.response.unverified_numbers


def test_attention_cannot_lower_local_severity() -> None:
    package = build_analysis_package()
    result = validate_response(GOOD_RESPONSE, package)
    assert result.response is not None
    assert apply_attention_floor(RuleSeverity.CRITICAL, result.response) is RuleSeverity.CRITICAL
    assert apply_attention_floor(RuleSeverity.INFO, result.response) is RuleSeverity.INFO


def test_parse_payload_strips_fences_and_rejects_broken_json() -> None:
    assert parse_payload('```json\n{"summary": "ok"}\n```') == {"summary": "ok"}
    with pytest.raises(SchemaError):
        parse_payload('{"summary": "обрезанный"')


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, RetryAction.STOP),
        (402, RetryAction.STOP),
        (429, RetryAction.RETRY),
        (500, RetryAction.RETRY),
        (400, RetryAction.FALLBACK),
    ],
)
def test_provider_error_classification(status: int, expected: RetryAction) -> None:
    assert classify_status(status) is expected


def test_phase_spread_is_not_normative_coefficient() -> None:
    spread = phase_spread([240.0, 232.6, 227.2])
    assert spread.spread == pytest.approx(12.8, abs=0.01)
    assert spread.relative_percent == pytest.approx(5.47, abs=0.05)
    assert spread.label_ru == "разброс фазных напряжений"


def build_incident() -> Incident:
    return Incident(
        incident_id="inc-1",
        rule_id="rule_voltage_l1_high",
        system_id="power",
        severity=RuleSeverity.CRITICAL,
        opened_at=NOW - timedelta(minutes=10),
        last_observed_at=NOW,
        state=IncidentState.OPEN,
        peak_deviation=12.8,
        peak_unit="V",
    )


def test_report_without_ai_keeps_local_facts() -> None:
    profile = ReportProfile(id="evening", name="Вечерний отчёт", locale=Locale.RU, timezone="Europe/Samara")
    package = build_analysis_package()
    incident = build_incident()
    incident_digest = digest(incident, {"rule_voltage_l1_high": "Перенапряжение L1"}, NOW)
    rendered = render_report(profile, package, NOW, incidents=(incident_digest,))
    assert rendered.used_ai is False
    assert "Аналитическое заключение недоступно" in rendered.text
    assert "57,8 °C" in rendered.text
    assert "Перенапряжение L1" in rendered.text
    assert rendered.attention is RuleSeverity.INFO


def test_report_with_ai_separates_facts_from_model_text() -> None:
    profile = ReportProfile(id="evening", name="Вечерний отчёт", locale=Locale.RU, detail=DetailLevel.STANDARD)
    package = build_analysis_package()
    result = validate_response(GOOD_RESPONSE, package)
    assert result.response is not None
    rendered = render_report(profile, package, NOW, response=result.response)
    assert rendered.used_ai
    assert "Вывод ИИ (не факты)" in rendered.text
    assert "57,8 °C" in rendered.text


def test_adaptive_detail_by_facts() -> None:
    profile = ReportProfile(id="auto", name="Авто")
    assert choose_detail(profile, ()) is DetailLevel.COMPACT
    warning = digest(build_incident(), {}, NOW)
    assert choose_detail(profile, (warning,)) is DetailLevel.DETAILED
    assert choose_detail(profile, (warning,), data_quality_issue=True) is DetailLevel.DETAILED
    quiet = Incident(
        incident_id="inc-2",
        rule_id="r",
        system_id=None,
        severity=RuleSeverity.WARNING,
        opened_at=NOW,
        last_observed_at=NOW,
    )
    assert choose_detail(profile, (digest(quiet, {}, NOW),)) is DetailLevel.STANDARD


def test_critical_alert_is_standalone() -> None:
    profile = ReportProfile(id="alert", name="Тревога", locale=Locale.RU, timezone="Europe/Samara")
    text = critical_alert(profile, digest(build_incident(), {"rule_voltage_l1_high": "Перенапряжение L1"}, NOW), NOW)
    assert text.startswith("🚨")
    assert "Перенапряжение L1" in text

def test_compact_report_shows_key_readings() -> None:
    profile = ReportProfile(id="compact", name="Короткий", detail=DetailLevel.COMPACT)
    rendered = render_report(profile, build_analysis_package(), NOW)
    assert "Ключевые показатели" in rendered.text
    assert "57,8" in rendered.text


def test_compact_report_without_numbers_is_honest() -> None:
    measurement = MeasurementFact(
        id="m1",
        name="Неизвестно",
        role=MeasurementRole.CUSTOM,
        value=None,
        unit=None,
        quality=Quality.UNCONFIGURED,
    )
    section = SystemSection(id="s1", name="Пустая система", measurements=(measurement,))
    package = build_package(
        site_id="site",
        now=NOW,
        window_start=NOW - timedelta(minutes=30),
        window_end=NOW,
        systems=(section,),
    )
    profile = ReportProfile(id="compact", name="Короткий", detail=DetailLevel.COMPACT)
    rendered = render_report(profile, package, NOW)
    assert "Числовых показаний нет" in rendered.text


def test_fallback_reports_ai_error_reason() -> None:
    profile = ReportProfile(id="r", name="Отчёт", locale=Locale.RU)
    rendered = render_report(profile, build_analysis_package(), NOW, ai_error="HTTP 401: invalid key")
    assert "Причина: HTTP 401: invalid key" in rendered.text


def test_history_facts_and_trend() -> None:
    window = Window.last(NOW, 600)
    data = samples([20.0 + index for index in range(11)], NOW - timedelta(seconds=600), 60)
    facts = history_facts("boiler_temp", "Подача", data, window, "°C")
    ids = {fact.id for fact in facts}
    assert {
        "fact_boiler_temp_delta",
        "fact_boiler_temp_mean",
        "fact_boiler_temp_min",
        "fact_boiler_temp_max",
    } <= ids
    coverage, trend = history_trend(data, window, "°C")
    assert coverage == pytest.approx(1.0)
    assert trend == "рост на 10,0 °C"


def test_history_facts_absent_without_data() -> None:
    window = Window.last(NOW, 3_600)
    sparse = samples([20.0, 20.5], NOW - timedelta(seconds=60), 60)
    facts = history_facts("boiler_temp", "Подача", sparse, window, "°C")
    assert len(facts) == 1
    assert facts[0].id == "fact_boiler_temp_coverage"
    assert "недостаточно данных" in facts[0].label
    coverage, trend = history_trend(sparse, window, "°C")
    assert trend is None
    assert coverage < 0.1
