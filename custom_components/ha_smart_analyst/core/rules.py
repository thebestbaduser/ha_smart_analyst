"""Правила: AST, схема и валидация (RULE-01…RULE-10).

Правило описывается деревом условий, сериализуется в JSON и проверяется до
сохранения. Произвольный Python, ``eval`` и шаблоны пользователя не
исполняются (SEM-07, SEC-04): допустимы только перечисленные ниже узлы.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .errors import SchemaError, UnitError
from .model import HomeModel, Measurement
from .units import compatible, convert, find_unit, validate_number

MAX_DEPTH = 8
MAX_NODES = 200


class Severity(StrEnum):
    """Уровень критичности правила (RULE-05)."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class MissingDataPolicy(StrEnum):
    """Политика отсутствующих данных (RULE-06)."""

    UNKNOWN = "unknown"
    IGNORE = "ignore"
    TREAT_AS_FAILURE = "treat_as_failure"
    SEPARATE_DATA_QUALITY_INCIDENT = "separate_data_quality_incident"


class CompareOp(StrEnum):
    """Операторы сравнения (RULE-02)."""

    GT = "gt"
    GE = "ge"
    LT = "lt"
    LE = "le"
    EQ = "eq"
    NE = "ne"


# -- операнды ---------------------------------------------------------
@dataclass(frozen=True, slots=True)
class MeasurementRef:
    measurement_id: str


@dataclass(frozen=True, slots=True)
class Literal:
    value: float | str | bool
    unit: str | None = None


@dataclass(frozen=True, slots=True)
class Derived:
    """Разность или абсолютное отклонение двух операндов (RULE-02)."""

    kind: str
    left: Operand
    right: Operand


type Operand = MeasurementRef | Literal | Derived


# -- условия ----------------------------------------------------------
@dataclass(frozen=True, slots=True)
class All:
    children: tuple[Condition, ...]


@dataclass(frozen=True, slots=True)
class AnyOf:
    children: tuple[Condition, ...]


@dataclass(frozen=True, slots=True)
class Not:
    child: Condition


@dataclass(frozen=True, slots=True)
class Compare:
    op: CompareOp
    left: Operand
    right: Operand


@dataclass(frozen=True, slots=True)
class Between:
    measurement_id: str
    low: Literal
    high: Literal
    invert: bool = False


@dataclass(frozen=True, slots=True)
class StateIs:
    measurement_id: str
    state: str


@dataclass(frozen=True, slots=True)
class StateChanged:
    measurement_id: str
    state: str
    direction: str  # "to" | "from"
    within_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class IsUnavailable:
    measurement_id: str


@dataclass(frozen=True, slots=True)
class IsStale:
    measurement_id: str
    max_age_seconds: int


@dataclass(frozen=True, slots=True)
class DeltaAbove:
    measurement_id: str
    window_seconds: int
    threshold: float
    unit: str | None
    absolute: bool = True


@dataclass(frozen=True, slots=True)
class RateAbove:
    measurement_id: str
    window_seconds: int
    threshold_per_second: float
    unit: str | None


type Condition = (
    All | AnyOf | Not | Compare | Between | StateIs | StateChanged | IsUnavailable | IsStale | DeltaAbove | RateAbove
)


@dataclass(frozen=True, slots=True)
class Recovery:
    """Условия восстановления и гистерезис (RULE-04)."""

    clear_below: Literal | None = None
    clear_above: Literal | None = None
    stable_seconds: int = 0
    measurement_id: str | None = None

    def __post_init__(self) -> None:
        if self.clear_below is None and self.clear_above is None:
            raise SchemaError("recovery требует clear_below или clear_above")


@dataclass(frozen=True, slots=True)
class Rule:
    """Правило целиком (RULE-07)."""

    id: str
    name: str
    expression: Condition
    description: str = ""
    version: int = 1
    scope_id: str | None = None
    enabled: bool = True
    severity: Severity = Severity.WARNING
    timezone: str | None = None
    for_seconds: int = 0
    recovery: Recovery | None = None
    missing_data_policy: MissingDataPolicy = MissingDataPolicy.UNKNOWN
    repeat_seconds: int | None = None
    on_recovery: bool = True
    debounce_seconds: int = 0
    cooldown_seconds: int = 0
    recipients: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()

    @property
    def dedup_key(self) -> str:
        """Ключ дедупликации инцидента: правило + область (INC-02)."""
        return f"{self.id}:{self.scope_id or '-'}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "scope_id": self.scope_id,
            "enabled": self.enabled,
            "severity": self.severity.value,
            "timezone": self.timezone,
            "expression": _condition_to_dict(self.expression),
            "for_seconds": self.for_seconds,
            "recovery": _recovery_to_dict(self.recovery),
            "missing_data_policy": self.missing_data_policy.value,
            "repeat_seconds": self.repeat_seconds,
            "on_recovery": self.on_recovery,
            "debounce_seconds": self.debounce_seconds,
            "cooldown_seconds": self.cooldown_seconds,
            "recipients": list(self.recipients),
            "actions": list(self.actions),
        }


_RULE_KEYS = {
    "id",
    "name",
    "description",
    "version",
    "scope_id",
    "enabled",
    "severity",
    "timezone",
    "expression",
    "for_seconds",
    "recovery",
    "missing_data_policy",
    "repeat_seconds",
    "on_recovery",
    "debounce_seconds",
    "cooldown_seconds",
    "recipients",
    "actions",
    "notify",
}


def _condition_to_dict(condition: Condition) -> dict[str, Any]:
    if isinstance(condition, All):
        return {"all": [_condition_to_dict(child) for child in condition.children]}
    if isinstance(condition, AnyOf):
        return {"any": [_condition_to_dict(child) for child in condition.children]}
    if isinstance(condition, Not):
        return {"not": _condition_to_dict(condition.child)}
    if isinstance(condition, Compare):
        return {
            "op": condition.op.value,
            "left": _operand_to_dict(condition.left),
            "right": _operand_to_dict(condition.right),
        }
    if isinstance(condition, Between):
        return {
            "op": "between",
            "measurement_id": condition.measurement_id,
            "low": _operand_to_dict(condition.low),
            "high": _operand_to_dict(condition.high),
            "invert": condition.invert,
        }
    if isinstance(condition, StateIs):
        return {"op": "state_is", "measurement_id": condition.measurement_id, "state": condition.state}
    if isinstance(condition, StateChanged):
        payload: dict[str, Any] = {
            "op": f"state_changed_{condition.direction}",
            "measurement_id": condition.measurement_id,
            "state": condition.state,
        }
        if condition.within_seconds is not None:
            payload["within_seconds"] = condition.within_seconds
        return payload
    if isinstance(condition, IsUnavailable):
        return {"op": "is_unavailable", "measurement_id": condition.measurement_id}
    if isinstance(condition, IsStale):
        return {
            "op": "is_stale",
            "measurement_id": condition.measurement_id,
            "max_age_seconds": condition.max_age_seconds,
        }
    if isinstance(condition, DeltaAbove):
        return {
            "op": "delta_above",
            "measurement_id": condition.measurement_id,
            "window_seconds": condition.window_seconds,
            "threshold": {"value": condition.threshold, "unit": condition.unit},
            "absolute": condition.absolute,
        }
    if isinstance(condition, RateAbove):
        return {
            "op": "rate_above",
            "measurement_id": condition.measurement_id,
            "window_seconds": condition.window_seconds,
            "threshold": {"value": condition.threshold_per_second, "unit": condition.unit},
        }
    raise SchemaError(f"неизвестный узел условия {type(condition).__name__}")


def _operand_to_dict(operand: Operand) -> dict[str, Any]:
    if isinstance(operand, MeasurementRef):
        return {"measurement_id": operand.measurement_id}
    if isinstance(operand, Literal):
        payload: dict[str, Any] = {"value": operand.value}
        if operand.unit is not None:
            payload["unit"] = operand.unit
        return payload
    return {
        operand.kind: [_operand_to_dict(operand.left), _operand_to_dict(operand.right)]
    }


def _recovery_to_dict(recovery: Recovery | None) -> dict[str, Any] | None:
    if recovery is None:
        return None
    payload: dict[str, Any] = {"stable_seconds": recovery.stable_seconds}
    if recovery.clear_below is not None:
        payload["clear_below"] = _operand_to_dict(recovery.clear_below)
    if recovery.clear_above is not None:
        payload["clear_above"] = _operand_to_dict(recovery.clear_above)
    if recovery.measurement_id is not None:
        payload["measurement_id"] = recovery.measurement_id
    return payload


# -- разбор -----------------------------------------------------------
def _need(payload: dict[str, Any], key: str, path: str) -> Any:
    if key not in payload:
        raise SchemaError(f"отсутствует обязательное поле {key!r}", path)
    return payload[key]


def _non_negative_int(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SchemaError("ожидалось целое число ≥ 0", path)
    return value


def _positive_int(value: Any, path: str) -> int:
    number = _non_negative_int(value, path)
    if number == 0:
        raise SchemaError("значение должно быть больше нуля", path)
    return number


def _parse_operand(payload: Any, model: HomeModel, path: str) -> Operand:
    if not isinstance(payload, dict):
        raise SchemaError("операнд должен быть объектом", path)
    keys = set(payload)
    if "measurement_id" in keys:
        if keys - {"measurement_id"}:
            raise SchemaError(f"лишние поля операнда: {sorted(keys - {'measurement_id'})}", path)
        measurement_id = payload["measurement_id"]
        if not isinstance(measurement_id, str) or not measurement_id:
            raise SchemaError("measurement_id должен быть непустой строкой", path)
        if model.measurement(measurement_id) is None:
            raise SchemaError(f"неизвестное измерение {measurement_id!r}", path)
        return MeasurementRef(measurement_id)
    if "value" in keys:
        if keys - {"value", "unit"}:
            raise SchemaError(f"лишние поля литерала: {sorted(keys - {'value', 'unit'})}", path)
        value = validate_number(payload["value"], f"{path}.value")
        unit = payload.get("unit")
        if unit is not None and find_unit(unit) is None:
            raise UnitError(f"{path}.unit: неизвестная единица {unit!r}")
        return Literal(value, unit)
    for kind in ("difference", "abs_deviation"):
        if kind in keys:
            operands = payload[kind]
            if not isinstance(operands, list) or len(operands) != 2:
                raise SchemaError(f"{kind} требует ровно два операнда", path)
            return Derived(
                kind,
                _parse_operand(operands[0], model, f"{path}.{kind}[0]"),
                _parse_operand(operands[1], model, f"{path}.{kind}[1]"),
            )
    raise SchemaError("неизвестная форма операнда", path)


def _operand_unit(operand: Operand, model: HomeModel) -> str | None:
    if isinstance(operand, MeasurementRef):
        return model.require_measurement(operand.measurement_id).unit
    if isinstance(operand, Literal):
        return operand.unit
    left = _operand_unit(operand.left, model)
    right = _operand_unit(operand.right, model)
    if left and right and not compatible(left, right):
        raise UnitError(f"операнды {left!r} и {right!r} несопоставимы")
    return left or right


def _operand_is_numeric(operand: Operand, model: HomeModel) -> bool:
    if isinstance(operand, Literal):
        return isinstance(operand.value, (int, float)) and not isinstance(operand.value, bool)
    if isinstance(operand, MeasurementRef):
        measurement = model.require_measurement(operand.measurement_id)
        return measurement.kind in {"numeric", "composite"}
    return _operand_is_numeric(operand.left, model) and _operand_is_numeric(operand.right, model)


def _check_numeric_operand(operand: Operand, model: HomeModel, path: str) -> None:
    if not _operand_is_numeric(operand, model):
        raise SchemaError("операция требует числового измерения", path)


def _parse_threshold(payload: Any, model: HomeModel, path: str) -> Literal:
    operand = _parse_operand(payload, model, path)
    if not isinstance(operand, Literal):
        raise SchemaError("порог должен быть числовым значением", path)
    return operand


def _parse_condition(payload: Any, model: HomeModel, path: str, depth: int, counter: list[int]) -> Condition:
    if depth > MAX_DEPTH:
        raise SchemaError(f"глубина условий больше {MAX_DEPTH}", path)
    counter[0] += 1
    if counter[0] > MAX_NODES:
        raise SchemaError(f"условие содержит больше {MAX_NODES} узлов", path)
    if not isinstance(payload, dict):
        raise SchemaError("условие должно быть объектом", path)
    keys = set(payload)
    if "all" in keys or "any" in keys:
        keyword = "all" if "all" in keys else "any"
        if keys - {keyword}:
            raise SchemaError(f"лишние поля условия: {sorted(keys - {keyword})}", path)
        children = payload[keyword]
        if not isinstance(children, list) or not children:
            raise SchemaError(f"{keyword} требует непустой список условий", path)
        parsed = tuple(
            _parse_condition(child, model, f"{path}.{keyword}[{index}]", depth + 1, counter)
            for index, child in enumerate(children)
        )
        return All(parsed) if keyword == "all" else AnyOf(parsed)
    if "not" in keys:
        if keys - {"not"}:
            raise SchemaError(f"лишние поля условия: {sorted(keys - {'not'})}", path)
        return Not(_parse_condition(payload["not"], model, f"{path}.not", depth + 1, counter))

    op = payload.get("op")
    if op in {item.value for item in CompareOp}:
        unexpected = keys - {"op", "left", "right"}
        if unexpected:
            raise SchemaError(f"лишние поля условия: {sorted(unexpected)}", path)
        left = _parse_operand(_need(payload, "left", path), model, f"{path}.left")
        right = _parse_operand(_need(payload, "right", path), model, f"{path}.right")
        _check_numeric_operand(left, model, f"{path}.left")
        _check_numeric_operand(right, model, f"{path}.right")
        left_unit = _operand_unit(left, model)
        right_unit = _operand_unit(right, model)
        if left_unit and right_unit and not compatible(left_unit, right_unit):
            raise UnitError(
                f"{path}: сравнение несопоставимых единиц {left_unit!r} и {right_unit!r}"
            )
        if isinstance(right, Literal) and right.unit and left_unit and right.unit != left_unit:
            right = Literal(convert(right.value, right.unit, left_unit), left_unit)
        if isinstance(left, Literal) and left.unit and right_unit and left.unit != right_unit:
            left = Literal(convert(left.value, left.unit, right_unit), right_unit)
        return Compare(CompareOp(op), left, right)

    if op == "between":
        unexpected = keys - {"op", "measurement_id", "low", "high", "invert"}
        if unexpected:
            raise SchemaError(f"лишние поля условия: {sorted(unexpected)}", path)
        measurement_id = _require_measurement_ref(payload, model, path)
        low = _parse_threshold(_need(payload, "low", path), model, f"{path}.low")
        high = _parse_threshold(_need(payload, "high", path), model, f"{path}.high")
        _ensure_threshold_units(model.require_measurement(measurement_id), low, f"{path}.low")
        _ensure_threshold_units(model.require_measurement(measurement_id), high, f"{path}.high")
        return Between(measurement_id, low, high, bool(payload.get("invert", False)))

    if op == "state_is":
        unexpected = keys - {"op", "measurement_id", "state"}
        if unexpected:
            raise SchemaError(f"лишние поля условия: {sorted(unexpected)}", path)
        measurement_id = _require_measurement_ref(payload, model, path, allow_state=True)
        state = _need(payload, "state", path)
        if not isinstance(state, str) or not state:
            raise SchemaError("state должен быть непустой строкой", path)
        return StateIs(measurement_id, state)

    if op in {"state_changed_to", "state_changed_from"}:
        unexpected = keys - {"op", "measurement_id", "state", "within_seconds"}
        if unexpected:
            raise SchemaError(f"лишние поля условия: {sorted(unexpected)}", path)
        measurement_id = _require_measurement_ref(payload, model, path, allow_state=True)
        state = _need(payload, "state", path)
        if not isinstance(state, str) or not state:
            raise SchemaError("state должен быть непустой строкой", path)
        within = payload.get("within_seconds")
        return StateChanged(
            measurement_id,
            state,
            "to" if op == "state_changed_to" else "from",
            _positive_int(within, f"{path}.within_seconds") if within is not None else None,
        )

    if op == "is_unavailable":
        unexpected = keys - {"op", "measurement_id"}
        if unexpected:
            raise SchemaError(f"лишние поля условия: {sorted(unexpected)}", path)
        return IsUnavailable(_require_measurement_ref(payload, model, path, allow_state=True))

    if op == "is_stale":
        unexpected = keys - {"op", "measurement_id", "max_age_seconds"}
        if unexpected:
            raise SchemaError(f"лишние поля условия: {sorted(unexpected)}", path)
        measurement_id = _require_measurement_ref(payload, model, path, allow_state=True)
        return IsStale(measurement_id, _positive_int(_need(payload, "max_age_seconds", path), f"{path}.max_age_seconds"))

    if op in {"delta_above", "rate_above"}:
        unexpected = keys - {"op", "measurement_id", "window_seconds", "threshold", "absolute"}
        if unexpected:
            raise SchemaError(f"лишние поля условия: {sorted(unexpected)}", path)
        if model.require_measurement(_require_measurement_ref(payload, model, path)).kind not in {
            "numeric",
            "composite",
        }:
            raise SchemaError("условие требует числового измерения", path)
        measurement = model.require_measurement(payload["measurement_id"])
        window = _positive_int(_need(payload, "window_seconds", path), f"{path}.window_seconds")
        threshold = _parse_threshold(_need(payload, "threshold", path), model, f"{path}.threshold")
        _ensure_threshold_units(measurement, threshold, f"{path}.threshold")
        if op == "delta_above":
            return DeltaAbove(
                measurement.id,
                window,
                threshold.value,
                threshold.unit,
                bool(payload.get("absolute", True)),
            )
        return RateAbove(measurement.id, window, threshold.value, threshold.unit)

    if op == "duration_true":
        raise SchemaError(
            "duration_true допустим только как прямой потомок корневого all "
            "(или используйте поле for_seconds правила)",
            path,
        )
    raise SchemaError(f"неизвестный оператор {op!r}", path)


def _require_measurement_ref(
    payload: dict[str, Any], model: HomeModel, path: str, *, allow_state: bool = False
) -> str:
    measurement_id = _need(payload, "measurement_id", path)
    if not isinstance(measurement_id, str) or not measurement_id:
        raise SchemaError("measurement_id должен быть непустой строкой", path)
    measurement = model.measurement(measurement_id)
    if measurement is None:
        raise SchemaError(f"неизвестное измерение {measurement_id!r}", path)
    if not allow_state and measurement.kind not in {"numeric", "composite"}:
        raise SchemaError(f"измерение {measurement.name!r} не является числовым", path)
    return measurement_id


def _ensure_threshold_units(measurement: Measurement, threshold: Literal, path: str) -> None:
    if not measurement.unit or not threshold.unit:
        return
    if not compatible(measurement.unit, threshold.unit):
        raise UnitError(
            f"{path}: единица порога {threshold.unit!r} несовместима с {measurement.unit!r}"
        )


def _extract_for_seconds(payload: Any, rule_for_seconds: int, path: str) -> tuple[Any, int]:
    """Вынести ``duration_true`` из корневого all (семантика §17.1)."""
    if not isinstance(payload, dict) or "all" not in payload:
        return payload, rule_for_seconds
    children = payload["all"]
    if not isinstance(children, list):
        return payload, rule_for_seconds
    durations = [child for child in children if isinstance(child, dict) and child.get("op") == "duration_true"]
    if not durations:
        return payload, rule_for_seconds
    if len(durations) > 1:
        raise SchemaError("допустим только один duration_true на правило", path)
    if rule_for_seconds:
        raise SchemaError("for_seconds и duration_true заданы одновременно", path)
    duration = durations[0]
    if set(duration) - {"op", "seconds"}:
        raise SchemaError("лишние поля duration_true", f"{path}.all")
    seconds = _positive_int(duration.get("seconds"), f"{path}.all.duration_true.seconds")
    remaining = [child for child in children if child is not duration]
    if not remaining:
        raise SchemaError("duration_true не может быть единственным условием", path)
    return {"all": remaining}, seconds


def parse_rule(payload: dict[str, Any], model: HomeModel, path: str = "rule") -> Rule:
    """Разобрать и проверить правило из JSON.

    Неизвестные поля отклоняются: молчаливое игнорирование опечаток приводит к
    правилам, которые «сохранены, но не работают».
    """
    if not isinstance(payload, dict):
        raise SchemaError("правило должно быть объектом", path)
    unexpected = set(payload) - _RULE_KEYS
    if unexpected:
        raise SchemaError(f"лишние поля правила: {sorted(unexpected)}", path)

    rule_id = payload.get("id") or str(uuid.uuid4())
    name = _need(payload, "name", path)
    if not isinstance(name, str) or not name.strip():
        raise SchemaError("name должно быть непустой строкой", path)

    expression_payload = _need(payload, "expression", path)
    for_seconds = _non_negative_int(payload.get("for_seconds", 0), f"{path}.for_seconds")
    expression_payload, for_seconds = _extract_for_seconds(
        expression_payload, for_seconds, f"{path}.expression"
    )

    counter = [0]
    expression = _parse_condition(expression_payload, model, f"{path}.expression", 0, counter)

    recovery_payload = payload.get("recovery")
    recovery = None
    if recovery_payload is not None:
        if not isinstance(recovery_payload, dict):
            raise SchemaError("recovery должен быть объектом", f"{path}.recovery")
        unexpected = set(recovery_payload) - {
            "clear_below",
            "clear_above",
            "stable_seconds",
            "measurement_id",
        }
        if unexpected:
            raise SchemaError(f"лишние поля recovery: {sorted(unexpected)}", f"{path}.recovery")
        clear_below = (
            _parse_threshold(recovery_payload["clear_below"], model, f"{path}.recovery.clear_below")
            if "clear_below" in recovery_payload
            else None
        )
        clear_above = (
            _parse_threshold(recovery_payload["clear_above"], model, f"{path}.recovery.clear_above")
            if "clear_above" in recovery_payload
            else None
        )
        if clear_below is None and clear_above is None:
            raise SchemaError("recovery требует clear_below или clear_above", f"{path}.recovery")
        recovery_measurement = recovery_payload.get("measurement_id")
        if recovery_measurement is not None:
            _require_measurement_ref(
                {"measurement_id": recovery_measurement}, model, f"{path}.recovery", allow_state=False
            )
            measurement = model.require_measurement(recovery_measurement)
            if clear_below is not None:
                _ensure_threshold_units(measurement, clear_below, f"{path}.recovery.clear_below")
            if clear_above is not None:
                _ensure_threshold_units(measurement, clear_above, f"{path}.recovery.clear_above")
        recovery = Recovery(
            clear_below=clear_below,
            clear_above=clear_above,
            stable_seconds=_non_negative_int(
                recovery_payload.get("stable_seconds", 0), f"{path}.recovery.stable_seconds"
            ),
            measurement_id=recovery_measurement,
        )

    severity = Severity(payload.get("severity", "warning"))
    policy = MissingDataPolicy(payload.get("missing_data_policy", "unknown"))

    repeat = payload.get("repeat_seconds")
    on_recovery = bool(payload.get("on_recovery", True))
    notify = payload.get("notify")
    if notify is not None:
        if not isinstance(notify, dict):
            raise SchemaError("notify должен быть объектом", f"{path}.notify")
        unexpected = set(notify) - {"repeat_seconds", "on_recovery"}
        if unexpected:
            raise SchemaError(f"лишние поля notify: {sorted(unexpected)}", f"{path}.notify")
        if repeat is not None or "on_recovery" in payload:
            raise SchemaError(
                "повтор и восстановление заданы и в notify, и в правиле", f"{path}.notify"
            )
        repeat = notify.get("repeat_seconds")
        on_recovery = bool(notify.get("on_recovery", True))

    return Rule(
        id=str(rule_id),
        name=name.strip(),
        expression=expression,
        description=str(payload.get("description", "")),
        version=_positive_int(payload.get("version", 1), f"{path}.version"),
        scope_id=payload.get("scope_id"),
        enabled=bool(payload.get("enabled", True)),
        severity=severity,
        timezone=payload.get("timezone"),
        for_seconds=for_seconds,
        recovery=recovery,
        missing_data_policy=policy,
        repeat_seconds=_positive_int(repeat, f"{path}.repeat_seconds") if repeat is not None else None,
        on_recovery=on_recovery,
        debounce_seconds=_non_negative_int(payload.get("debounce_seconds", 0), f"{path}.debounce_seconds"),
        cooldown_seconds=_non_negative_int(payload.get("cooldown_seconds", 0), f"{path}.cooldown_seconds"),
        recipients=tuple(payload.get("recipients", ())),
        actions=tuple(payload.get("actions", ())),
    )


def referenced_measurements(condition: Condition) -> tuple[str, ...]:
    """Все измерения, на которые ссылается условие (для диагностики и тестов)."""
    found: list[str] = []

    def operand(operand_value: Operand) -> None:
        if isinstance(operand_value, MeasurementRef):
            found.append(operand_value.measurement_id)
        elif isinstance(operand_value, Derived):
            operand(operand_value.left)
            operand(operand_value.right)

    def walk(node: Condition) -> None:
        if isinstance(node, (All, AnyOf)):
            for child in node.children:
                walk(child)
        elif isinstance(node, Not):
            walk(node.child)
        elif isinstance(node, Compare):
            operand(node.left)
            operand(node.right)
        elif isinstance(node, (Between, DeltaAbove, RateAbove, StateIs, StateChanged, IsUnavailable, IsStale)):
            found.append(node.measurement_id)

    walk(condition)
    return tuple(dict.fromkeys(found))
