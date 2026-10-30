"""Вычислитель условий и состояние правила (RULE-01…RULE-10, INC-05).

Трёхзначная логика: ``True``, ``False``, ``None`` (неизвестно). Неизвестное
никогда не приравнивается к норме автоматически: способ обработки задаёт
политика ``missing_data_policy`` правила.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from .errors import SchemaError
from .history import HistoryProvider, Sample, Window, trend
from .model import HomeModel
from .observations import Observation, Quality, ensure_utc
from .rules import (
    All,
    AnyOf,
    Between,
    Compare,
    CompareOp,
    Condition,
    DeltaAbove,
    IsStale,
    IsUnavailable,
    Literal,
    MeasurementRef,
    MissingDataPolicy,
    Not,
    Operand,
    RateAbove,
    Rule,
    Severity,
    StateChanged,
    StateIs,
    referenced_measurements,
)
from .units import compatible, convert

Tri = bool | None

#: Метки времени, которые не стоит считать свежими для контроля «датчик молчит».
_STATE_CHANGE_HORIZON = timedelta(days=7)


@dataclass(frozen=True, slots=True)
class Reason:
    """Причина оценки — для UI «почему true/false/unknown» (UI-03)."""

    code: str
    message: str
    measurement_id: str | None = None
    value: float | str | bool | None = None


@dataclass(frozen=True, slots=True)
class Evaluation:
    """Результат вычисления узла."""

    value: Tri
    reasons: tuple[Reason, ...] = ()
    data_quality: bool = False
    deviation: float | None = None

    @property
    def is_unknown(self) -> bool:
        return self.value is None


def _unknown(message: str, measurement_id: str | None = None, code: str = "unknown") -> Evaluation:
    return Evaluation(None, (Reason(code, message, measurement_id),))


@dataclass
class EvaluationContext:
    """Снимок, история и время, на которых считается правило."""

    model: HomeModel
    observations: Mapping[str, Observation]
    now: datetime
    history: HistoryProvider | None = None
    state_changes: Mapping[str, Sequence[tuple[datetime, str]]] = field(default_factory=dict)

    def observation(self, measurement_id: str) -> Observation | None:
        return self.observations.get(measurement_id)


def _state_text(observation: Observation) -> str | None:
    if observation.value is None:
        return None
    if isinstance(observation.value, bool):
        return "on" if observation.value else "off"
    return str(observation.value)


def _numeric(operand: Operand, ctx: EvaluationContext) -> tuple[Tri, str | None, Evaluation]:
    if isinstance(operand, Literal):
        return float(operand.value), operand.unit, Evaluation(True)
    if isinstance(operand, MeasurementRef):
        observation = ctx.observation(operand.measurement_id)
        if observation is None:
            return None, None, _unknown(
                "измерение отсутствует в снимке", operand.measurement_id, "missing_source"
            )
        if observation.quality is not Quality.OK:
            return None, observation.unit, _unknown(
                f"качество данных: {observation.quality.value}",
                operand.measurement_id,
                observation.quality.value,
            )
        value = observation.numeric_value
        if value is None:
            return None, observation.unit, _unknown(
                "значение не является числом", operand.measurement_id, "invalid"
            )
        return value, observation.unit, Evaluation(True)
    left_value, left_unit, left_eval = _numeric(operand.left, ctx)
    right_value, right_unit, right_eval = _numeric(operand.right, ctx)
    reasons = left_eval.reasons + right_eval.reasons
    if left_value is None or right_value is None:
        return None, left_unit or right_unit, Evaluation(None, reasons)
    if left_unit and right_unit and left_unit != right_unit:
        if not compatible(left_unit, right_unit):
            raise SchemaError(f"несопоставимые единицы {left_unit!r} и {right_unit!r}", operand.kind)
        right_value = convert(right_value, right_unit, left_unit)
    difference = left_value - right_value
    value = abs(difference) if operand.kind == "abs_deviation" else difference
    return value, left_unit or right_unit, Evaluation(True, reasons, deviation=difference)


def _leaf_policy(value: Tri, policy: MissingDataPolicy, evaluation: Evaluation) -> Evaluation:
    """Применить политику отсутствующих данных к листу правила (RULE-06)."""
    if value is not None:
        return evaluation
    if policy is MissingDataPolicy.TREAT_AS_FAILURE:
        return Evaluation(True, evaluation.reasons, evaluation.data_quality, evaluation.deviation)
    if policy is MissingDataPolicy.SEPARATE_DATA_QUALITY_INCIDENT:
        return Evaluation(False, evaluation.reasons, True, evaluation.deviation)
    return evaluation


def _combine_all(children: Sequence[Evaluation], ignore_unknown: bool) -> Evaluation:
    reasons = tuple(reason for child in children for reason in child.reasons)
    data_quality = any(child.data_quality for child in children)
    deviations = [child.deviation for child in children if child.deviation is not None]
    deviation = max(deviations, key=abs) if deviations else None
    if ignore_unknown:
        children = tuple(child for child in children if child.value is not None)
        if not children:
            return Evaluation(None, reasons, data_quality, deviation)
    if any(child.value is False for child in children):
        return Evaluation(False, reasons, data_quality, deviation)
    if any(child.value is None for child in children):
        return Evaluation(None, reasons, data_quality, deviation)
    return Evaluation(True, reasons, data_quality, deviation)


def _combine_any(children: Sequence[Evaluation], ignore_unknown: bool) -> Evaluation:
    reasons = tuple(reason for child in children for reason in child.reasons)
    data_quality = any(child.data_quality for child in children)
    deviations = [child.deviation for child in children if child.deviation is not None]
    deviation = max(deviations, key=abs) if deviations else None
    if ignore_unknown:
        children = tuple(child for child in children if child.value is not None)
        if not children:
            return Evaluation(None, reasons, data_quality, deviation)
    if any(child.value is True for child in children):
        return Evaluation(True, reasons, data_quality, deviation)
    if any(child.value is None for child in children):
        return Evaluation(None, reasons, data_quality, deviation)
    return Evaluation(False, reasons, data_quality, deviation)


_OPERATORS = {
    CompareOp.GT: lambda left, right: left > right,
    CompareOp.GE: lambda left, right: left >= right,
    CompareOp.LT: lambda left, right: left < right,
    CompareOp.LE: lambda left, right: left <= right,
    CompareOp.EQ: lambda left, right: left == right,
    CompareOp.NE: lambda left, right: left != right,
}


def evaluate(condition: Condition, ctx: EvaluationContext, policy: MissingDataPolicy = MissingDataPolicy.UNKNOWN) -> Evaluation:
    """Вычислить условие с учётом политики отсутствующих данных."""
    ignore_unknown = policy is MissingDataPolicy.IGNORE

    if isinstance(condition, All):
        return _combine_all([evaluate(child, ctx, policy) for child in condition.children], ignore_unknown)
    if isinstance(condition, AnyOf):
        return _combine_any([evaluate(child, ctx, policy) for child in condition.children], ignore_unknown)
    if isinstance(condition, Not):
        child = evaluate(condition.child, ctx, policy)
        return Evaluation(
            None if child.value is None else not child.value,
            child.reasons,
            child.data_quality,
            child.deviation,
        )
    if isinstance(condition, Compare):
        left_value, _, left_eval = _numeric(condition.left, ctx)
        right_value, _, right_eval = _numeric(condition.right, ctx)
        reasons = left_eval.reasons + right_eval.reasons
        data_quality = left_eval.data_quality or right_eval.data_quality
        if left_value is None or right_value is None:
            return _leaf_policy(None, policy, Evaluation(None, reasons, data_quality))
        result = _OPERATORS[condition.op](left_value, right_value)
        return Evaluation(result, reasons, data_quality, deviation=left_value - right_value)
    if isinstance(condition, Between):
        observation = ctx.observation(condition.measurement_id)
        if observation is None or observation.numeric_value is None:
            return _leaf_policy(None, policy, _unknown("нет числового значения", condition.measurement_id))
        if observation.quality is not Quality.OK:
            return _leaf_policy(
                None, policy, _unknown(f"качество данных: {observation.quality.value}", condition.measurement_id)
            )
        value = observation.numeric_value
        inside = condition.low.value <= value <= condition.high.value
        result = not inside if condition.invert else inside
        deviation = 0.0
        if value > condition.high.value:
            deviation = value - condition.high.value
        elif value < condition.low.value:
            deviation = condition.low.value - value
        return Evaluation(result, deviation=deviation)
    if isinstance(condition, StateIs):
        observation = ctx.observation(condition.measurement_id)
        text = _state_text(observation) if observation and observation.quality is Quality.OK else None
        if text is None:
            return _leaf_policy(
                None, policy, _unknown("состояние недоступно", condition.measurement_id)
            )
        return Evaluation(text.lower() == condition.state.lower())
    if isinstance(condition, StateChanged):
        changes = ctx.state_changes.get(condition.measurement_id)
        if changes is None:
            return _leaf_policy(
                None,
                policy,
                _unknown("переключения не отслеживаются", condition.measurement_id, "history_missing"),
            )
        now = ensure_utc(ctx.now, ctx.now)
        horizon = (
            timedelta(seconds=condition.within_seconds)
            if condition.within_seconds is not None
            else _STATE_CHANGE_HORIZON
        )
        matched = any(
            (now - ensure_utc(moment, now)) <= horizon and (state or "").lower() == condition.state.lower()
            for moment, state in changes
        )
        return Evaluation(matched)
    if isinstance(condition, IsUnavailable):
        observation = ctx.observation(condition.measurement_id)
        if observation is None:
            return _leaf_policy(None, policy, _unknown("источник отсутствует", condition.measurement_id))
        if observation.quality is Quality.UNAVAILABLE:
            return Evaluation(True)
        if observation.quality in {Quality.OK, Quality.STALE, Quality.INVALID}:
            return Evaluation(False)
        return _leaf_policy(None, policy, _unknown("состояние недоступно", condition.measurement_id))
    if isinstance(condition, IsStale):
        observation = ctx.observation(condition.measurement_id)
        if observation is None:
            return _leaf_policy(None, policy, _unknown("источник отсутствует", condition.measurement_id))
        if observation.quality is Quality.STALE:
            return Evaluation(True)
        age = observation.age_seconds(ctx.now)
        if observation.quality is Quality.OK and age is not None:
            return Evaluation(age > condition.max_age_seconds)
        return _leaf_policy(None, policy, _unknown("нет отметки времени", condition.measurement_id))
    if isinstance(condition, DeltaAbove):
        return _history_condition(condition, ctx, policy, rate=False)
    if isinstance(condition, RateAbove):
        return _history_condition(condition, ctx, policy, rate=True)
    raise SchemaError(f"неизвестный узел условия {type(condition).__name__}")


def _history_condition(
    condition: DeltaAbove | RateAbove, ctx: EvaluationContext, policy: MissingDataPolicy, *, rate: bool
) -> Evaluation:
    if ctx.history is None:
        return _leaf_policy(
            None, policy, _unknown("история недоступна", condition.measurement_id, "history_missing")
        )
    window = Window.last(ensure_utc(ctx.now, ctx.now), condition.window_seconds)
    samples: Sequence[Sample] = ctx.history.samples(condition.measurement_id, window)
    result = trend(samples, window)
    if not result.usable:
        reason = result.coverage.reason or "недостаточно истории"
        return _leaf_policy(None, policy, _unknown(reason, condition.measurement_id, "history_missing"))
    if rate:
        assert result.rate_per_second is not None
        compare_value = result.rate_per_second
        deviation = compare_value - condition.threshold_per_second
        result_value = compare_value > condition.threshold_per_second
    else:
        assert result.delta is not None
        compare_value = abs(result.delta) if condition.absolute else result.delta
        deviation = compare_value - condition.threshold
        result_value = compare_value > condition.threshold
    return Evaluation(result_value, deviation=deviation)


# -- состояние правила ------------------------------------------------
class RuntimeState(StrEnum):
    """Состояние выполнения правила."""

    IDLE = "idle"
    PENDING = "pending"
    ACTIVE = "active"
    RECOVERING = "recovering"


class RuleEventKind(StrEnum):
    """События правила для журнала и уведомлений."""

    ACTIVATED = "activated"
    RECOVERED = "recovered"
    REMINDER = "reminder"
    DATA_QUALITY = "data_quality"


@dataclass(frozen=True, slots=True)
class RuleEvent:
    kind: RuleEventKind
    rule_id: str
    at: datetime
    severity: Severity
    reasons: tuple[Reason, ...] = ()
    peak_deviation: float | None = None
    duration_seconds: float | None = None
    data_quality: bool = False


@dataclass(frozen=True, slots=True)
class RuleEvaluation:
    """Полный результат правила: основное условие и условие восстановления."""

    value: Tri
    recovery: Tri
    reasons: tuple[Reason, ...] = ()
    deviation: float | None = None
    data_quality: bool = False


def _recovery_check(rule: Rule, ctx: EvaluationContext, threshold: Literal, *, below: bool) -> Tri:
    recovery = rule.recovery
    assert recovery is not None
    measurement_id = recovery.measurement_id
    if measurement_id is None:
        candidates = referenced_measurements(rule.expression)
        measurement_id = candidates[0] if len(candidates) == 1 else None
    if measurement_id is None:
        raise SchemaError("recovery без явного measurement_id неоднозначен", rule.id)
    observation = ctx.observation(measurement_id)
    if observation is None or observation.numeric_value is None or observation.quality is not Quality.OK:
        return None
    value = observation.numeric_value
    if threshold.unit and observation.unit and threshold.unit != observation.unit:
        value = convert(value, observation.unit, threshold.unit)
    return value < threshold.value if below else value > threshold.value


def evaluate_rule(rule: Rule, ctx: EvaluationContext) -> RuleEvaluation:
    """Вычислить правило целиком."""
    evaluation = evaluate(rule.expression, ctx, rule.missing_data_policy)
    recovery: Tri
    if rule.recovery is None:
        recovery = None if evaluation.value is None else not evaluation.value
    else:
        checks: list[Tri] = []
        if rule.recovery.clear_below is not None:
            checks.append(_recovery_check(rule, ctx, rule.recovery.clear_below, below=True))
        if rule.recovery.clear_above is not None:
            checks.append(_recovery_check(rule, ctx, rule.recovery.clear_above, below=False))
        if any(check is False for check in checks):
            recovery = False
        elif any(check is None for check in checks):
            recovery = None
        else:
            recovery = True
    return RuleEvaluation(
        value=evaluation.value,
        recovery=recovery,
        reasons=evaluation.reasons,
        deviation=evaluation.deviation,
        data_quality=evaluation.data_quality,
    )


@dataclass
class RuleRuntime:
    """Состояние правила, переживающее перезапуск HA (INC-05, STO-04)."""

    rule_id: str
    state: RuntimeState = RuntimeState.IDLE
    pending_since: datetime | None = None
    active_since: datetime | None = None
    recovery_since: datetime | None = None
    last_notified_at: datetime | None = None
    last_resolved_at: datetime | None = None
    suppress_until: datetime | None = None
    occurrences: int = 0
    peak_deviation: float | None = None
    data_quality_since: datetime | None = None

    def update(self, rule: Rule, evaluation: RuleEvaluation, now: datetime) -> list[RuleEvent]:
        """Продвинуть состояние правила и вернуть события."""
        moment = ensure_utc(now, now)
        events: list[RuleEvent] = []
        events.extend(self._data_quality_events(rule, evaluation, moment))

        if self.state is RuntimeState.IDLE:
            if evaluation.value is True:
                if self.suppress_until is not None and moment < ensure_utc(self.suppress_until, moment):
                    self.pending_since = None
                    return events
                if rule.for_seconds > 0:
                    if self.pending_since is None:
                        self.pending_since = moment
                        self.state = RuntimeState.PENDING
                        return events
                    elapsed = (moment - ensure_utc(self.pending_since, moment)).total_seconds()
                    if elapsed < rule.for_seconds:
                        return events
                self._activate(rule, evaluation, moment, events)
            else:
                self.pending_since = None
            return events

        if self.state is RuntimeState.PENDING:
            if evaluation.value is True:
                assert self.pending_since is not None
                elapsed = (moment - ensure_utc(self.pending_since, moment)).total_seconds()
                if elapsed >= rule.for_seconds:
                    self._activate(rule, evaluation, moment, events)
            else:
                self.pending_since = None
                self.state = RuntimeState.IDLE
            return events

        if self.state is RuntimeState.ACTIVE:
            self._track_peak(evaluation)
            self._maybe_remind(rule, evaluation, moment, events)
            if evaluation.recovery is False:
                self.recovery_since = None
            elif evaluation.recovery is True:
                self._maybe_resolve(rule, evaluation, moment, events)
            return events

        if self.state is RuntimeState.RECOVERING:
            if evaluation.value is True:
                self.state = RuntimeState.ACTIVE
                self.recovery_since = None
                self._track_peak(evaluation)
            elif evaluation.recovery is True:
                self._maybe_resolve(rule, evaluation, moment, events)
            elif evaluation.recovery is False:
                self.state = RuntimeState.ACTIVE
                self.recovery_since = None
            return events

        return events

    def _activate(
        self, rule: Rule, evaluation: RuleEvaluation, now: datetime, events: list[RuleEvent]
    ) -> None:
        self.state = RuntimeState.ACTIVE
        self.active_since = now
        self.pending_since = None
        self.recovery_since = None
        self.last_notified_at = now
        self.occurrences += 1
        self.peak_deviation = abs(evaluation.deviation) if evaluation.deviation is not None else None
        events.append(
            RuleEvent(
                RuleEventKind.ACTIVATED,
                rule.id,
                now,
                rule.severity,
                evaluation.reasons,
                self.peak_deviation,
                data_quality=evaluation.data_quality,
            )
        )

    def _maybe_resolve(
        self, rule: Rule, evaluation: RuleEvaluation, now: datetime, events: list[RuleEvent]
    ) -> None:
        stable = rule.recovery.stable_seconds if rule.recovery else 0
        if stable > 0:
            if self.recovery_since is None:
                self.recovery_since = now
                self.state = RuntimeState.RECOVERING
                return
            elapsed = (now - ensure_utc(self.recovery_since, now)).total_seconds()
            if elapsed < stable:
                return
        duration = None
        if self.active_since is not None:
            duration = (now - ensure_utc(self.active_since, now)).total_seconds()
        self.state = RuntimeState.IDLE
        self.recovery_since = None
        self.active_since = None
        self.pending_since = None
        self.last_resolved_at = now
        self.suppress_until = now + timedelta(seconds=rule.cooldown_seconds + rule.debounce_seconds)
        events.append(
            RuleEvent(
                RuleEventKind.RECOVERED,
                rule.id,
                now,
                rule.severity,
                evaluation.reasons,
                self.peak_deviation,
                duration,
                evaluation.data_quality,
            )
        )
        self.peak_deviation = None

    def _maybe_remind(
        self, rule: Rule, evaluation: RuleEvaluation, now: datetime, events: list[RuleEvent]
    ) -> None:
        if not rule.repeat_seconds or self.last_notified_at is None:
            return
        elapsed = (now - ensure_utc(self.last_notified_at, now)).total_seconds()
        if elapsed >= rule.repeat_seconds:
            self.last_notified_at = now
            events.append(
                RuleEvent(
                    RuleEventKind.REMINDER,
                    rule.id,
                    now,
                    rule.severity,
                    evaluation.reasons,
                    self.peak_deviation,
                    data_quality=evaluation.data_quality,
                )
            )

    def _track_peak(self, evaluation: RuleEvaluation) -> None:
        if evaluation.deviation is None:
            return
        deviation = abs(evaluation.deviation)
        if self.peak_deviation is None or deviation > self.peak_deviation:
            self.peak_deviation = deviation

    def _data_quality_events(
        self, rule: Rule, evaluation: RuleEvaluation, now: datetime
    ) -> list[RuleEvent]:
        if evaluation.data_quality:
            if self.data_quality_since is None:
                self.data_quality_since = now
                return [
                    RuleEvent(
                        RuleEventKind.DATA_QUALITY,
                        rule.id,
                        now,
                        Severity.INFO,
                        evaluation.reasons,
                        data_quality=True,
                    )
                ]
            return []
        self.data_quality_since = None
        return []

    # -- сериализация -------------------------------------------------
    def to_dict(self) -> dict[str, object]:
        def iso(value: datetime | None) -> str | None:
            return value.isoformat() if value else None

        return {
            "rule_id": self.rule_id,
            "state": self.state.value,
            "pending_since": iso(self.pending_since),
            "active_since": iso(self.active_since),
            "recovery_since": iso(self.recovery_since),
            "last_notified_at": iso(self.last_notified_at),
            "last_resolved_at": iso(self.last_resolved_at),
            "suppress_until": iso(self.suppress_until),
            "occurrences": self.occurrences,
            "peak_deviation": self.peak_deviation,
            "data_quality_since": iso(self.data_quality_since),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object], now: datetime) -> RuleRuntime:
        moment = ensure_utc(now, now)

        def moment_of(key: str) -> datetime | None:
            raw = payload.get(key)
            if not raw:
                return None
            return ensure_utc(datetime.fromisoformat(str(raw)), moment)

        return cls(
            rule_id=str(payload["rule_id"]),
            state=RuntimeState(payload.get("state", "idle")),
            pending_since=moment_of("pending_since"),
            active_since=moment_of("active_since"),
            recovery_since=moment_of("recovery_since"),
            last_notified_at=moment_of("last_notified_at"),
            last_resolved_at=moment_of("last_resolved_at"),
            suppress_until=moment_of("suppress_until"),
            occurrences=int(payload.get("occurrences", 0)),
            peak_deviation=float(payload["peak_deviation"]) if payload.get("peak_deviation") is not None else None,
            data_quality_since=moment_of("data_quality_since"),
        )


def severity_floor(local: Severity, proposed: Severity) -> Severity:
    """ИИ не может понизить критичность локального факта (AI-04, §17.3)."""
    order = {Severity.INFO: 0, Severity.WARNING: 1, Severity.CRITICAL: 2}
    return local if order[local] >= order[proposed] else proposed
