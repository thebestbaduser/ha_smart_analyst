"""Тесты вычислителя правил, таймеров и инцидентов."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from core.engine import (
    EvaluationContext,
    RuleEventKind,
    RuleRuntime,
    RuntimeState,
    evaluate,
    evaluate_rule,
)
from core.incidents import (
    IncidentManager,
    IncidentState,
    ResolutionKind,
)
from core.model import (
    HomeModel,
    Measurement,
    MeasurementRole,
    Site,
    SourceRef,
    System,
    SystemPreset,
)
from core.observations import Observation, Quality, ValueType
from core.rules import MissingDataPolicy, Severity, parse_rule

NOW = datetime(2026, 10, 30, 20, 0, tzinfo=UTC)


def build_model() -> HomeModel:
    model = HomeModel()
    model.set_site(Site(id="site", name="Дом"))
    model.add_system(System(id="power", site_id="site", name="Электрика", preset=SystemPreset.ELECTRICITY))
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
            id="leak",
            name="Протечка",
            role=MeasurementRole.LEAK,
            system_id="power",
            source=SourceRef("binary_sensor.leak"),
        )
    )
    return model


def voltage_rule(payload_overrides: dict | None = None):
    payload = {
        "id": "rule_voltage_l1_high",
        "name": "Перенапряжение L1",
        "severity": "warning",
        "expression": {
            "all": [
                {"op": "gt", "left": {"measurement_id": "voltage_l1"}, "right": {"value": 245, "unit": "V"}},
                {"op": "duration_true", "seconds": 30},
            ]
        },
        "recovery": {
            "clear_below": {"value": 242, "unit": "V"},
            "stable_seconds": 60,
            "measurement_id": "voltage_l1",
        },
        "missing_data_policy": "unknown",
        "cooldown_seconds": 0,
        "repeat_seconds": 3600,
    }
    if payload_overrides:
        payload.update(payload_overrides)
    return parse_rule(payload, build_model())


def numeric(value: float | None, unit: str = "V", quality: Quality = Quality.OK) -> Observation:
    return Observation(
        source_id="m",
        entity_id="sensor.l1_voltage",
        value=value,
        value_type=ValueType.NUMBER if value is not None else ValueType.UNKNOWN,
        unit=unit,
        observed_at=NOW,
        quality=quality,
    )


def context(value: Observation, now: datetime = NOW) -> EvaluationContext:
    return EvaluationContext(model=build_model(), observations={"voltage_l1": value}, now=now)


def test_duration_true_delays_activation() -> None:
    rule = voltage_rule()
    runtime = RuleRuntime(rule.id)
    start = NOW

    assert runtime.update(rule, evaluate_rule(rule, context(numeric(250.0), start)), start) == []
    assert runtime.state is RuntimeState.PENDING

    later = start + timedelta(seconds=10)
    assert runtime.update(rule, evaluate_rule(rule, context(numeric(250.0), later)), later) == []
    assert runtime.state is RuntimeState.PENDING

    ready = start + timedelta(seconds=31)
    events = runtime.update(rule, evaluate_rule(rule, context(numeric(250.0), ready)), ready)
    assert [event.kind for event in events] == [RuleEventKind.ACTIVATED]
    assert runtime.state is RuntimeState.ACTIVE
    assert events[0].peak_deviation == pytest.approx(5.0)


def test_unknown_state_does_not_count_towards_duration() -> None:
    rule = voltage_rule()
    runtime = RuleRuntime(rule.id)
    start = NOW
    runtime.update(rule, evaluate_rule(rule, context(numeric(250.0), start)), start)
    unknown = start + timedelta(seconds=15)
    runtime.update(rule, evaluate_rule(rule, context(numeric(None, quality=Quality.UNKNOWN), unknown)), unknown)
    assert runtime.state is RuntimeState.IDLE
    assert runtime.pending_since is None


def test_oscillation_does_not_flood_notifications() -> None:
    """AC-06: сто колебаний около порога не дают шквала срабатываний."""
    payload = {
        "id": "chatter",
        "name": "Дребезг",
        "severity": "warning",
        "expression": {"op": "gt", "left": {"measurement_id": "voltage_l1"}, "right": {"value": 245, "unit": "V"}},
        "recovery": {"clear_below": {"value": 242, "unit": "V"}, "stable_seconds": 60, "measurement_id": "voltage_l1"},
        "debounce_seconds": 120,
    }
    rule = parse_rule(payload, build_model())
    runtime = RuleRuntime(rule.id)
    activations = 0
    now = NOW
    for index in range(100):
        now = NOW + timedelta(seconds=index)
        value = 246.0 if index % 2 == 0 else 241.0
        events = runtime.update(rule, evaluate_rule(rule, context(numeric(value), now)), now)
        activations += sum(1 for event in events if event.kind is RuleEventKind.ACTIVATED)
    assert activations == 1
    assert runtime.occurrences == 1


def test_hysteresis_and_stable_recovery() -> None:
    rule = voltage_rule()
    runtime = RuleRuntime(rule.id)
    start = NOW
    runtime.update(rule, evaluate_rule(rule, context(numeric(250.0), start)), start)
    activate_at = start + timedelta(seconds=31)
    events = runtime.update(rule, evaluate_rule(rule, context(numeric(250.0), activate_at)), activate_at)
    assert [event.kind for event in events] == [RuleEventKind.ACTIVATED]

    recovering_at = activate_at + timedelta(seconds=5)
    events = runtime.update(rule, evaluate_rule(rule, context(numeric(240.0), recovering_at)), recovering_at)
    assert events == []
    assert runtime.state is RuntimeState.RECOVERING

    resolved_at = recovering_at + timedelta(seconds=61)
    events = runtime.update(rule, evaluate_rule(rule, context(numeric(240.0), resolved_at)), resolved_at)
    assert [event.kind for event in events] == [RuleEventKind.RECOVERED]
    assert events[0].duration_seconds == pytest.approx((resolved_at - activate_at).total_seconds())
    assert runtime.state is RuntimeState.IDLE


def test_recovery_resets_when_value_returns_above_threshold() -> None:
    rule = voltage_rule()
    runtime = RuleRuntime(rule.id)
    start = NOW
    runtime.update(rule, evaluate_rule(rule, context(numeric(250.0), start)), start)
    at = start + timedelta(seconds=31)
    runtime.update(rule, evaluate_rule(rule, context(numeric(250.0), at)), at)
    recovering_at = at + timedelta(seconds=10)
    runtime.update(rule, evaluate_rule(rule, context(numeric(240.0), recovering_at)), recovering_at)
    back_at = recovering_at + timedelta(seconds=30)
    events = runtime.update(rule, evaluate_rule(rule, context(numeric(250.0), back_at)), back_at)
    assert events == []
    assert runtime.state is RuntimeState.ACTIVE


def test_missing_data_policies() -> None:
    unknown_rule = voltage_rule({"for_seconds": 0, "expression": {
        "op": "gt", "left": {"measurement_id": "voltage_l1"}, "right": {"value": 245, "unit": "V"}
    }})
    unknown_eval = evaluate_rule(unknown_rule, context(numeric(None, quality=Quality.UNKNOWN)))
    assert unknown_eval.value is None

    failure_rule = voltage_rule({
        "for_seconds": 0,
        "missing_data_policy": "treat_as_failure",
        "expression": {
            "op": "gt", "left": {"measurement_id": "voltage_l1"}, "right": {"value": 245, "unit": "V"}
        },
        "recovery": None,
    })
    assert evaluate_rule(failure_rule, context(numeric(None, quality=Quality.UNKNOWN))).value is True

    quality_rule = voltage_rule({
        "for_seconds": 0,
        "missing_data_policy": "separate_data_quality_incident",
        "expression": {
            "op": "gt", "left": {"measurement_id": "voltage_l1"}, "right": {"value": 245, "unit": "V"}
        },
        "recovery": None,
    })
    runtime = RuleRuntime(quality_rule.id)
    quality_eval = evaluate_rule(quality_rule, context(numeric(None, quality=Quality.STALE)))
    events = runtime.update(quality_rule, quality_eval, NOW)
    assert [event.kind for event in events] == [RuleEventKind.DATA_QUALITY]
    assert runtime.state is RuntimeState.IDLE


def test_ignore_policy_combines_known_values() -> None:
    model = build_model()
    payload = {
        "id": "ignored",
        "name": "Игнор неизвестного",
        "missing_data_policy": "ignore",
        "expression": {
            "all": [
                {"op": "gt", "left": {"measurement_id": "voltage_l1"}, "right": {"value": 200, "unit": "V"}},
                {"op": "gt", "left": {"measurement_id": "voltage_l1"}, "right": {"value": 300, "unit": "V"}},
            ]
        },
    }
    rule = parse_rule(payload, model, "rule")
    observations = {"voltage_l1": numeric(250.0)}
    ctx = EvaluationContext(model=model, observations=observations, now=NOW)
    evaluation = evaluate(rule.expression, ctx, MissingDataPolicy.IGNORE)
    assert evaluation.value is False

    ctx_unknown = EvaluationContext(
        model=model, observations={"voltage_l1": numeric(None, quality=Quality.UNKNOWN)}, now=NOW
    )
    assert evaluate(rule.expression, ctx_unknown, MissingDataPolicy.UNKNOWN).value is None
    assert evaluate(rule.expression, ctx_unknown, MissingDataPolicy.IGNORE).value is None


def test_reminder_and_repeat_interval() -> None:
    rule = voltage_rule({"for_seconds": 0, "repeat_seconds": 60, "expression": {
        "op": "gt", "left": {"measurement_id": "voltage_l1"}, "right": {"value": 245, "unit": "V"}
    }})
    runtime = RuleRuntime(rule.id)
    start = NOW
    events = runtime.update(rule, evaluate_rule(rule, context(numeric(250.0), start)), start)
    assert [event.kind for event in events] == [RuleEventKind.ACTIVATED]
    soon = start + timedelta(seconds=30)
    assert runtime.update(rule, evaluate_rule(rule, context(numeric(250.0), soon)), soon) == []
    later = start + timedelta(seconds=61)
    events = runtime.update(rule, evaluate_rule(rule, context(numeric(250.0), later)), later)
    assert [event.kind for event in events] == [RuleEventKind.REMINDER]


def test_cooldown_blocks_immediate_reactivation() -> None:
    rule = voltage_rule({
        "for_seconds": 0,
        "cooldown_seconds": 300,
        "recovery": None,
        "expression": {"op": "gt", "left": {"measurement_id": "voltage_l1"}, "right": {"value": 245, "unit": "V"}},
    })
    runtime = RuleRuntime(rule.id)
    start = NOW
    runtime.update(rule, evaluate_rule(rule, context(numeric(250.0), start)), start)
    down = start + timedelta(seconds=10)
    events = runtime.update(rule, evaluate_rule(rule, context(numeric(240.0), down)), down)
    assert [event.kind for event in events] == [RuleEventKind.RECOVERED]

    again = down + timedelta(seconds=30)
    assert runtime.update(rule, evaluate_rule(rule, context(numeric(250.0), again)), again) == []
    after = down + timedelta(seconds=301)
    events = runtime.update(rule, evaluate_rule(rule, context(numeric(250.0), after)), after)
    assert [event.kind for event in events] == [RuleEventKind.ACTIVATED]


def test_restart_keeps_active_rule_without_duplicate_activation() -> None:
    """AC-14: перезапуск не создаёт второй инцидент."""
    rule = voltage_rule({"for_seconds": 0, "expression": {
        "op": "gt", "left": {"measurement_id": "voltage_l1"}, "right": {"value": 245, "unit": "V"}
    }})
    runtime = RuleRuntime(rule.id)
    start = NOW
    runtime.update(rule, evaluate_rule(rule, context(numeric(250.0), start)), start)
    snapshot = runtime.to_dict()

    restored = RuleRuntime.from_dict(snapshot, start + timedelta(hours=1))
    assert restored.state is RuntimeState.ACTIVE
    resumed = start + timedelta(hours=1)
    events = restored.update(rule, evaluate_rule(rule, context(numeric(250.0), resumed)), resumed)
    assert [event for event in events if event.kind is RuleEventKind.ACTIVATED] == []


def test_restart_recomputes_pending_timer() -> None:
    rule = voltage_rule()
    runtime = RuleRuntime(rule.id)
    start = NOW
    runtime.update(rule, evaluate_rule(rule, context(numeric(250.0), start)), start)
    restored = RuleRuntime.from_dict(runtime.to_dict(), start + timedelta(seconds=40))
    resumed = start + timedelta(seconds=40)
    events = restored.update(rule, evaluate_rule(rule, context(numeric(250.0), resumed)), resumed)
    assert [event.kind for event in events] == [RuleEventKind.ACTIVATED]


def test_incident_deduplication_and_acknowledgement() -> None:
    from core.engine import RuleEvent

    rule = voltage_rule({"for_seconds": 0, "expression": {
        "op": "gt", "left": {"measurement_id": "voltage_l1"}, "right": {"value": 245, "unit": "V"}
    }})
    runtime = RuleRuntime(rule.id)
    manager = IncidentManager()

    start = NOW
    events = runtime.update(rule, evaluate_rule(rule, context(numeric(250.0), start)), start)
    first = manager.register(events[0], system_id="power", unit="V")
    assert first.state is IncidentState.OPEN
    assert first.severity is Severity.WARNING
    assert first.peak_deviation == pytest.approx(5.0)

    repeated = manager.register(events[0], system_id="power", unit="V")
    assert repeated.incident_id == first.incident_id
    assert repeated.occurrences == 2

    later = start + timedelta(seconds=20)
    higher_peak = RuleEvent(
        RuleEventKind.ACTIVATED, rule.id, later, Severity.WARNING, peak_deviation=15.0
    )
    second = manager.register(higher_peak, system_id="power", unit="V")
    assert second.incident_id == first.incident_id
    assert second.occurrences == 3
    assert second.peak_deviation == pytest.approx(15.0)

    acknowledged = manager.acknowledge(first.incident_id, "owner", later)
    assert acknowledged.state is IncidentState.ACKNOWLEDGED
    assert acknowledged.is_active

    resolved = manager.resolve(first.incident_id, later + timedelta(minutes=5))
    assert resolved.state is IncidentState.RESOLVED
    assert resolved.duration_seconds == pytest.approx(320)
    assert resolved.resolution is ResolutionKind.AUTOMATIC


def test_incident_manual_resolution_and_escalation() -> None:
    rule = voltage_rule({"for_seconds": 0, "expression": {
        "op": "gt", "left": {"measurement_id": "voltage_l1"}, "right": {"value": 245, "unit": "V"}
    }})
    runtime = RuleRuntime(rule.id)
    events = runtime.update(rule, evaluate_rule(rule, context(numeric(250.0), NOW)), NOW)
    manager = IncidentManager()
    incident = manager.register(events[0], system_id="power")

    escalated = manager.escalate(incident.incident_id, NOW + timedelta(minutes=1), "нет ответа 10 минут")
    assert escalated.state is IncidentState.ESCALATED
    assert len(escalated.escalations) == 1

    manual = manager.resolve_manually(
        incident.incident_id, "admin", NOW + timedelta(minutes=2), "проверено вручную"
    )
    assert manual.resolution is ResolutionKind.MANUAL
    assert manual.reasons[-1].code == "manual_resolution"


def test_incident_prune_and_round_trip() -> None:
    rule = voltage_rule({"for_seconds": 0, "expression": {
        "op": "gt", "left": {"measurement_id": "voltage_l1"}, "right": {"value": 245, "unit": "V"}
    }})
    runtime = RuleRuntime(rule.id)
    events = runtime.update(rule, evaluate_rule(rule, context(numeric(250.0), NOW)), NOW)
    manager = IncidentManager()
    incident = manager.register(events[0], system_id="power")
    manager.resolve(incident.incident_id, NOW + timedelta(minutes=1))

    restored = IncidentManager.from_list(manager.to_list(), NOW + timedelta(days=1))
    assert restored.get(incident.incident_id) is not None

    removed = restored.prune(NOW + timedelta(days=400), retention_days=365)
    assert removed == 1
    assert restored.get(incident.incident_id) is None


def test_automatic_state_change_and_staleness_conditions() -> None:
    model = build_model()
    payload = {
        "name": "Протечка",
        "severity": "critical",
        "expression": {"op": "state_is", "measurement_id": "leak", "state": "on"},
    }
    rule = parse_rule(payload, model)
    leak = Observation(
        source_id="l",
        entity_id="binary_sensor.leak",
        value=True,
        value_type=ValueType.BOOLEAN,
        unit=None,
        observed_at=NOW,
        quality=Quality.OK,
    )
    ctx = EvaluationContext(model=model, observations={"leak": leak}, now=NOW)
    assert evaluate_rule(rule, ctx).value is True

    stale_payload = {
        "name": "Тихий датчик",
        "expression": {"op": "is_stale", "measurement_id": "leak", "max_age_seconds": 600},
    }
    stale_rule = parse_rule(stale_payload, model)
    stale_leak = Observation(
        source_id="l",
        entity_id="binary_sensor.leak",
        value=False,
        value_type=ValueType.BOOLEAN,
        unit=None,
        observed_at=NOW,
        quality=Quality.STALE,
        last_updated=NOW - timedelta(hours=2),
    )
    ctx = EvaluationContext(model=model, observations={"leak": stale_leak}, now=NOW)
    assert evaluate_rule(stale_rule, ctx).value is True
