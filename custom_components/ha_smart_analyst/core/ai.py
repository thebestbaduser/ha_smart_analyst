"""Пакет для ИИ и валидация ответа (AI-01…AI-08, §17.2, §17.3).

Нейросеть не формирует исходные числа, пороги и факты: она получает готовый
пакет фактов и может только ссылаться на их идентификаторы. Неверный или
неполный ответ не публикуется — используется шаблонный отчёт.
"""

from __future__ import annotations

import json
import math
import random
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from .engine import Severity
from .errors import SchemaError
from .incidents import Incident
from .model import MeasurementRole
from .observations import Observation, Quality

SCHEMA_VERSION = "1.0"
#: Системные инструкции неизменяемы и не отключаются пользователем (AI-05).
SYSTEM_PROMPT = (
    "Ты аналитик показаний дома. Используй только факты из переданного JSON-пакета. "
    "Не придумывай значения, пороги, аварии и время событий. "
    "Не ссылайся на факты, которых нет в пакете. "
    "Числа в тексте разрешены только те, что есть в пакете. "
    "Гипотезы помечай как гипотезы, рекомендации — как рекомендации. "
    "Не понижай критичность локальных инцидентов. "
    "Если данных недостаточно, прямо укажи это в uncertainties. "
    "Отвечай строго JSON-объектом по схеме: summary, observations, hypotheses, "
    "recommendations, attention_level, uncertainties, evidence_refs."
)

DEFAULT_CONSTRAINTS: tuple[str, ...] = (
    "Do not claim a trend without history",
    "Use only supplied facts",
    "Do not lower local incident severity",
    "Do not invent numbers, thresholds or events",
)

ATTENTION_LEVELS = frozenset({"info", "warning", "critical"})
_ATTENTION_TO_SEVERITY = {
    "info": Severity.INFO,
    "warning": Severity.WARNING,
    "critical": Severity.CRITICAL,
}
_RESPONSE_KEYS = {
    "summary",
    "observations",
    "hypotheses",
    "recommendations",
    "attention_level",
    "uncertainties",
    "evidence_refs",
}
_NUMBER_RE = re.compile(r"-?\d+(?:[.,]\d+)?")


@dataclass(frozen=True, slots=True)
class Fact:
    """Факт пакета: число, вычисленное локально, с идентификатором."""

    id: str
    kind: str
    value: float | str | bool | None
    unit: str | None = None
    label: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "value": self.value, "unit": self.unit, "label": self.label}


@dataclass(frozen=True, slots=True)
class MeasurementFact:
    """Измерение в пакете: только разрешённые поля (AI-08)."""

    id: str
    name: str
    role: MeasurementRole
    value: float | str | bool | None
    unit: str | None
    quality: Quality

    @classmethod
    def from_observation(cls, measurement_id: str, name: str, role: MeasurementRole, observation: Observation) -> MeasurementFact:
        return cls(
            id=measurement_id,
            name=name,
            role=role,
            value=observation.value,
            unit=observation.unit,
            quality=observation.quality,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "role": self.role.value,
            "value": self.value,
            "unit": self.unit,
            "quality": self.quality.value,
        }


@dataclass(frozen=True, slots=True)
class SystemSection:
    """Раздел системы в пакете."""

    id: str
    name: str
    measurements: tuple[MeasurementFact, ...]
    facts: tuple[Fact, ...] = ()
    coverage: float | None = None
    trend: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "measurements": [item.to_dict() for item in self.measurements],
            "facts": [item.to_dict() for item in self.facts],
            "history": {"coverage": self.coverage if self.coverage is not None else 0.0, "trend": self.trend},
        }


@dataclass(frozen=True, slots=True)
class AnalysisPackage:
    """Пакет, отправляемый провайдеру ИИ."""

    site_id: str
    generated_at: datetime
    window_start: datetime
    window_end: datetime
    systems: tuple[SystemSection, ...] = ()
    incidents: tuple[Mapping[str, Any], ...] = ()
    constraints: tuple[str, ...] = DEFAULT_CONSTRAINTS

    @property
    def fact_ids(self) -> frozenset[str]:
        return frozenset(fact.id for system in self.systems for fact in system.facts)

    @property
    def numbers(self) -> tuple[float, ...]:
        values: list[float] = []
        for system in self.systems:
            for measurement in system.measurements:
                if isinstance(measurement.value, (int, float)) and not isinstance(measurement.value, bool):
                    values.append(float(measurement.value))
            for fact in system.facts:
                if isinstance(fact.value, (int, float)) and not isinstance(fact.value, bool):
                    values.append(float(fact.value))
        for incident in self.incidents:
            peak = incident.get("peak_deviation")
            if isinstance(peak, (int, float)):
                values.append(float(peak))
        return tuple(values)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "site_id": self.site_id,
            "generated_at": self.generated_at.isoformat(),
            "report_window": {
                "start": self.window_start.isoformat(),
                "end": self.window_end.isoformat(),
            },
            "systems": [system.to_dict() for system in self.systems],
            "incidents": list(self.incidents),
            "constraints": list(self.constraints),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))


def incident_fact(incident: Incident, rule_name: str) -> dict[str, Any]:
    """Представление инцидента в пакете: только факты, без домыслов."""
    return {
        "id": incident.incident_id,
        "rule": rule_name,
        "severity": incident.severity.value,
        "state": incident.state.value,
        "opened_at": incident.opened_at.isoformat(),
        "resolved_at": incident.resolved_at.isoformat() if incident.resolved_at else None,
        "peak_deviation": incident.peak_deviation,
        "peak_unit": incident.peak_unit,
        "acknowledged": incident.ack_at is not None,
        "data_quality": incident.data_quality,
    }


def _point_in_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def build_package(
    *,
    site_id: str,
    now: datetime,
    window_start: datetime,
    window_end: datetime,
    systems: Sequence[SystemSection],
    incidents: Sequence[Mapping[str, Any]] = (),
    constraints: Sequence[str] = DEFAULT_CONSTRAINTS,
) -> AnalysisPackage:
    """Собрать пакет и проверить, что период и время корректны."""
    if window_end <= window_start:
        raise SchemaError("окно отчёта должно иметь положительную длительность")
    return AnalysisPackage(
        site_id=site_id,
        generated_at=now,
        window_start=window_start,
        window_end=window_end,
        systems=tuple(systems),
        incidents=tuple(incidents),
        constraints=tuple(constraints),
    )


@dataclass
class AIResponse:
    """Проверенный ответ модели."""

    summary: str
    attention_level: str
    observations: tuple[dict[str, Any], ...] = ()
    hypotheses: tuple[dict[str, Any], ...] = ()
    recommendations: tuple[dict[str, Any], ...] = ()
    uncertainties: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    unverified_numbers: tuple[str, ...] = ()

    @property
    def severity(self) -> Severity:
        return _ATTENTION_TO_SEVERITY[self.attention_level]


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Итог проверки ответа модели."""

    ok: bool
    response: AIResponse | None = None
    errors: tuple[str, ...] = ()

    @property
    def error_text(self) -> str:
        return "; ".join(self.errors)


def parse_payload(raw: str) -> dict[str, Any]:
    """Разобрать JSON-ответ провайдера, не публикуя обрезанный текст."""
    text = (raw or "").strip()
    if not text:
        raise SchemaError("пустой ответ модели")
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as err:
        raise SchemaError(f"невалидный JSON: {err.msg} (позиция {err.pos})") from err
    if not isinstance(payload, dict):
        raise SchemaError("ответ модели должен быть объектом")
    return payload


def _text_item(value: Any, path: str) -> dict[str, Any]:
    if isinstance(value, str):
        return {"text": value, "evidence_refs": []}
    if not isinstance(value, dict):
        raise SchemaError("ожидался объект или строка", path)
    unexpected = set(value) - {"text", "evidence_refs"}
    if unexpected:
        raise SchemaError(f"лишние поля: {sorted(unexpected)}", path)
    text = value.get("text")
    if not isinstance(text, str) or not text.strip():
        raise SchemaError("text должен быть непустой строкой", path)
    refs = value.get("evidence_refs", [])
    if not isinstance(refs, list) or not all(isinstance(ref, str) for ref in refs):
        raise SchemaError("evidence_refs должен быть списком строк", path)
    return {"text": text.strip(), "evidence_refs": refs}


def _contains_number(text: str) -> Iterable[str]:
    return _NUMBER_RE.findall(text)


def _number_is_known(raw: str, known: Sequence[float]) -> bool:
    try:
        value = float(raw.replace(",", "."))
    except ValueError:
        return False
    for candidate in known:
        if math.isclose(value, candidate, rel_tol=0.01, abs_tol=0.05):
            return True
        if math.isclose(value, abs(candidate), rel_tol=0.01, abs_tol=0.05):
            return True
    return False


def validate_response(payload: Mapping[str, Any], package: AnalysisPackage) -> ValidationResult:
    """Строгая проверка ответа модели (AI-03, §17.3)."""
    errors: list[str] = []
    if not isinstance(payload, Mapping):
        return ValidationResult(False, errors=("ответ должен быть объектом",))
    unexpected = set(payload) - _RESPONSE_KEYS
    if unexpected:
        errors.append(f"лишние поля ответа: {sorted(unexpected)}")

    summary = payload.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        errors.append("summary обязателен")
    attention = payload.get("attention_level")
    if attention not in ATTENTION_LEVELS:
        errors.append(f"attention_level должен быть одним из {sorted(ATTENTION_LEVELS)}")

    observations: list[dict[str, Any]] = []
    hypotheses: list[dict[str, Any]] = []
    recommendations: list[dict[str, Any]] = []
    for key, target in (
        ("observations", observations),
        ("hypotheses", hypotheses),
        ("recommendations", recommendations),
    ):
        raw_items = payload.get(key, [])
        if not isinstance(raw_items, list):
            errors.append(f"{key} должен быть списком")
            continue
        for index, item in enumerate(raw_items):
            try:
                target.append(_text_item(item, f"{key}[{index}]"))
            except SchemaError as err:
                errors.append(str(err))

    uncertainties: list[str] = []
    raw_uncertainties = payload.get("uncertainties", [])
    if not isinstance(raw_uncertainties, list) or not all(
        isinstance(item, str) for item in raw_uncertainties
    ):
        errors.append("uncertainties должен быть списком строк")
    else:
        uncertainties = [item.strip() for item in raw_uncertainties if item.strip()]

    refs: list[str] = []
    raw_refs = payload.get("evidence_refs", [])
    if not isinstance(raw_refs, list) or not all(isinstance(ref, str) for ref in raw_refs):
        errors.append("evidence_refs должен быть списком строк")
    else:
        known_ids = package.fact_ids | {str(item.get("id")) for item in package.incidents}
        refs = list(raw_refs)
        unresolved = [ref for ref in refs if ref not in known_ids]
        if unresolved:
            errors.append(f"неразрешённые evidence_refs: {unresolved}")
        for index, item in enumerate((*observations, *hypotheses, *recommendations)):
            item_refs = item.get("evidence_refs", [])
            unresolved_item = [ref for ref in item_refs if ref not in known_ids]
            if unresolved_item:
                errors.append(f"элемент {index}: неразрешённые evidence_refs {unresolved_item}")

    if errors:
        return ValidationResult(False, errors=tuple(errors))

    known_numbers = package.numbers
    texts = [summary, *(item["text"] for item in (*observations, *hypotheses, *recommendations))]
    unverified: list[str] = []
    for text in texts:
        for raw_number in _contains_number(text):
            if not _number_is_known(raw_number, known_numbers):
                unverified.append(raw_number)
    return ValidationResult(
        True,
        AIResponse(
            summary=summary.strip(),
            attention_level=str(attention),
            observations=tuple(observations),
            hypotheses=tuple(hypotheses),
            recommendations=tuple(recommendations),
            uncertainties=tuple(uncertainties),
            evidence_refs=tuple(refs),
            unverified_numbers=tuple(dict.fromkeys(unverified)),
        ),
    )


def apply_attention_floor(local: Severity, response: AIResponse) -> Severity:
    """``attention_level`` не может понизить критичность инцидента (§17.3)."""
    proposed = response.severity
    order = {Severity.INFO: 0, Severity.WARNING: 1, Severity.CRITICAL: 2}
    return local if order[local] >= order[proposed] else proposed


class RetryAction(StrEnum):
    """Что делать с ошибкой провайдера (NFR-04)."""

    RETRY = "retry"
    FALLBACK = "fallback"
    STOP = "stop"


def classify_status(status: int) -> RetryAction:
    """Классифицировать HTTP-статус провайдера: без бесконечных повторов."""
    if status in {401, 402, 403}:
        return RetryAction.STOP
    if status == 429:
        return RetryAction.RETRY
    if 400 <= status < 500:
        return RetryAction.FALLBACK
    if status >= 500:
        return RetryAction.RETRY
    return RetryAction.FALLBACK


def retry_delay(attempt: int, base_seconds: float = 2.0, max_seconds: float = 120.0, jitter: float = 0.3) -> float:
    """Экспоненциальная пауза с джиттером (AI-07)."""
    delay = min(max_seconds, base_seconds * (2 ** max(0, attempt - 1)))
    return delay * (1.0 + random.uniform(-jitter, jitter))
