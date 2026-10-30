"""Отчёты: профили, локальные факты и отдельный блок ИИ (REP-01…REP-07).

Ключевое правило: числовые факты рендерятся локально и не зависят от ответа
модели. Блок ИИ явно отделён, а его непроверенные числа помечаются.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from .ai import AIResponse, AnalysisPackage, SystemSection
from .engine import Severity
from .incidents import IncidentDigest
from .observations import Quality


class DetailLevel(StrEnum):
    """Степени детализации (REP-04)."""

    COMPACT = "compact"
    STANDARD = "standard"
    DETAILED = "detailed"
    ADAPTIVE = "adaptive"


class ReportType(StrEnum):
    """Типы отчётов (REP-02)."""

    SCHEDULED = "scheduled"
    ON_DEMAND = "on_demand"
    EVENT = "event"
    INCIDENT_RESOLUTION = "incident_resolution"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    COMPARISON = "comparison"
    DIAGNOSTIC = "diagnostic"


class Locale(StrEnum):
    """Язык отчёта."""

    RU = "ru"
    EN = "en"


@dataclass(frozen=True, slots=True)
class ReportProfile:
    """Профиль отчёта (REP-01)."""

    id: str
    name: str
    detail: DetailLevel = DetailLevel.ADAPTIVE
    locale: Locale = Locale.RU
    timezone: str = "UTC"
    report_type: ReportType = ReportType.SCHEDULED
    enabled: bool = True
    system_ids: tuple[str, ...] = ()
    history_seconds: int = 1_800
    include_warnings: bool = True
    recipients: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "detail": self.detail.value,
            "locale": self.locale.value,
            "timezone": self.timezone,
            "report_type": self.report_type.value,
            "enabled": self.enabled,
            "system_ids": list(self.system_ids),
            "history_seconds": self.history_seconds,
            "include_warnings": self.include_warnings,
            "recipients": list(self.recipients),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> ReportProfile:
        return cls(
            id=str(payload.get("id") or uuid.uuid4()),
            name=str(payload.get("name") or "Отчёт"),
            detail=DetailLevel(payload.get("detail", "adaptive")),
            locale=Locale(payload.get("locale", "ru")),
            timezone=str(payload.get("timezone", "UTC")),
            report_type=ReportType(payload.get("report_type", "scheduled")),
            enabled=bool(payload.get("enabled", True)),
            system_ids=tuple(payload.get("system_ids", ())),  # type: ignore[arg-type]
            history_seconds=int(payload.get("history_seconds", 1_800)),
            include_warnings=bool(payload.get("include_warnings", True)),
            recipients=tuple(payload.get("recipients", ())),  # type: ignore[arg-type]
        )


class ReportState(StrEnum):
    """Доставка отчёта (REP-05)."""

    SENT = "sent"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class PhaseSpread:
    """Разброс фазных напряжений — не нормативный коэффициент несимметрии (REP-06)."""

    minimum: float
    maximum: float
    spread: float
    relative_percent: float
    unit: str

    @property
    def label_ru(self) -> str:
        return "разброс фазных напряжений"

    @property
    def label_en(self) -> str:
        return "phase voltage spread"


def phase_spread(values: Sequence[float], unit: str = "V") -> PhaseSpread:
    """Посчитать разброс фаз и относительный процент от среднего."""
    if len(values) < 2:
        raise ValueError("нужно минимум два значения")
    minimum = min(values)
    maximum = max(values)
    mean = sum(values) / len(values)
    if mean == 0:
        raise ValueError("среднее значение равно нулю")
    return PhaseSpread(minimum, maximum, maximum - minimum, (maximum - minimum) / mean * 100.0, unit)


def format_number(value: float, unit: str | None = None, locale: Locale = Locale.RU, digits: int = 1) -> str:
    """Число с локальным разделителем и единицей."""
    text = f"{value:.{digits}f}"
    if locale is Locale.RU:
        text = text.replace(".", ",")
    return f"{text} {unit}" if unit else text


def format_duration(seconds: float | None, locale: Locale = Locale.RU) -> str:
    """Длительность в человекочитаемом виде."""
    if seconds is None:
        return "—" if locale is Locale.RU else "—"
    total = int(round(seconds))
    hours, remainder = divmod(total, 3_600)
    minutes, secs = divmod(remainder, 60)
    if locale is Locale.RU:
        if hours:
            return f"{hours} ч {minutes} мин"
        if minutes:
            return f"{minutes} мин {secs} с"
        return f"{secs} с"
    if hours:
        return f"{hours} h {minutes} min"
    if minutes:
        return f"{minutes} min {secs} s"
    return f"{secs} s"


def local_moment(value: datetime, timezone_name: str) -> datetime:
    """Показать время в часовом поясе HA (DATA-04)."""
    try:
        from zoneinfo import ZoneInfo

        target = ZoneInfo(timezone_name)
    except Exception:  # noqa: BLE001 - неизвестный пояс не должен ломать отчёт
        target = UTC
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(target)


def choose_detail(
    profile: ReportProfile,
    incidents: Sequence[IncidentDigest],
    data_quality_issue: bool = False,
) -> DetailLevel:
    """Адаптивная детализация по фактам, а не по «настроению» модели (AI-06)."""
    if profile.detail is not DetailLevel.ADAPTIVE:
        return profile.detail
    if any(item.severity is Severity.CRITICAL for item in incidents):
        return DetailLevel.DETAILED
    if incidents or data_quality_issue:
        return DetailLevel.STANDARD
    return DetailLevel.COMPACT


_SEVERITY_RU = {Severity.INFO: "информация", Severity.WARNING: "предупреждение", Severity.CRITICAL: "критично"}
_SEVERITY_EN = {Severity.INFO: "info", Severity.WARNING: "warning", Severity.CRITICAL: "critical"}
_QUALITY_RU = {
    Quality.OK: "ок",
    Quality.STALE: "устарело",
    Quality.UNKNOWN: "неизвестно",
    Quality.UNAVAILABLE: "недоступно",
    Quality.INVALID: "некорректно",
    Quality.UNCONFIGURED: "не настроено",
    Quality.HISTORY_MISSING: "нет истории",
}
_QUALITY_EN = {
    Quality.OK: "ok",
    Quality.STALE: "stale",
    Quality.UNKNOWN: "unknown",
    Quality.UNAVAILABLE: "unavailable",
    Quality.INVALID: "invalid",
    Quality.UNCONFIGURED: "not configured",
    Quality.HISTORY_MISSING: "no history",
}


@dataclass(frozen=True, slots=True)
class RenderedReport:
    """Готовый текст отчёта и метаданные доставки."""

    text: str
    detail: DetailLevel
    used_ai: bool
    attention: Severity
    unverified_numbers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


def _header(profile: ReportProfile, package: AnalysisPackage, now: datetime) -> list[str]:
    moment = local_moment(now, profile.timezone)
    if profile.locale is Locale.RU:
        return [
            f"**{profile.name}**",
            f"{moment:%d.%m.%Y %H:%M} ({profile.timezone})",
            f"Период: {local_moment(package.window_start, profile.timezone):%d.%m %H:%M} — "
            f"{local_moment(package.window_end, profile.timezone):%d.%m %H:%M}",
        ]
    return [
        f"**{profile.name}**",
        f"{moment:%Y-%m-%d %H:%M} ({profile.timezone})",
        f"Period: {local_moment(package.window_start, profile.timezone):%m-%d %H:%M} — "
        f"{local_moment(package.window_end, profile.timezone):%m-%d %H:%M}",
    ]


def _system_lines(system: SystemSection, locale: Locale, detailed: bool) -> list[str]:
    lines = [f"• {system.name}"]
    for measurement in system.measurements:
        quality = (_QUALITY_RU if locale is Locale.RU else _QUALITY_EN)[measurement.quality]
        value = measurement.value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            rendered = format_number(float(value), measurement.unit, locale)
        else:
            rendered = "—" if value is None else str(value)
        suffix = "" if measurement.quality is Quality.OK else f" [{quality}]"
        lines.append(f"  – {measurement.name}: {rendered}{suffix}")
    if detailed:
        for fact in system.facts:
            if isinstance(fact.value, (int, float)) and not isinstance(fact.value, bool):
                rendered = format_number(float(fact.value), fact.unit, locale)
            else:
                rendered = str(fact.value)
            lines.append(f"  · {fact.label or fact.id}: {rendered}")
        coverage = system.coverage or 0.0
        trend = system.trend or ("нет данных" if locale is Locale.RU else "no data")
        label = "покрытие истории" if locale is Locale.RU else "history coverage"
        lines.append(f"  · {label}: {coverage:.0%}; {trend}")
    return lines


def _incident_lines(incidents: Sequence[IncidentDigest], locale: Locale) -> list[str]:
    if not incidents:
        return []
    labels = _SEVERITY_RU if locale is Locale.RU else _SEVERITY_EN
    lines = ["", "**Инциденты**" if locale is Locale.RU else "**Incidents**"]
    for item in incidents:
        peak = (
            format_number(item.peak_deviation, item.peak_unit, locale)
            if item.peak_deviation is not None
            else "—"
        )
        ack = ""
        if item.acknowledged:
            ack = " (принято)" if locale is Locale.RU else " (acknowledged)"
        lines.append(
            f"• {labels[item.severity]}: {item.rule_name}{ack}; "
            f"{format_duration(item.duration_seconds, locale)}; "
            f"{'пик' if locale is Locale.RU else 'peak'} {peak}"
        )
    return lines


def _fallback_lines(package: AnalysisPackage, locale: Locale) -> list[str]:
    """Шаблонный отчёт без ИИ (AI-07, AC-11)."""
    if locale is Locale.RU:
        lines = [
            "",
            "**Аналитическое заключение недоступно**",
            "ИИ не ответил или ответ не прошёл проверку. Ниже — локальные факты.",
        ]
        degraded = any(
            measurement.quality is not Quality.OK
            for system in package.systems
            for measurement in system.measurements
        )
        if degraded:
            lines.append("Часть источников имеет ограниченное качество данных.")
        return lines
    return [
        "",
        "**AI summary unavailable**",
        "The model did not answer or its response failed validation. Local facts follow.",
    ]


def _ai_lines(
    response: AIResponse, locale: Locale, attention: Severity
) -> list[str]:
    lines = ["", "**Вывод ИИ (не факты)**" if locale is Locale.RU else "**AI summary (not facts)**"]
    lines.append(response.summary)
    for item in response.observations:
        lines.append(f"• {item['text']}")
    if response.hypotheses:
        lines.append("Гипотезы:" if locale is Locale.RU else "Hypotheses:")
        lines.extend(f"• {item['text']}" for item in response.hypotheses)
    if response.recommendations:
        lines.append("Рекомендации:" if locale is Locale.RU else "Recommendations:")
        lines.extend(f"• {item['text']}" for item in response.recommendations)
    if response.uncertainties:
        lines.append("Ограничения:" if locale is Locale.RU else "Limitations:")
        lines.extend(f"• {item}" for item in response.uncertainties)
    labels = _SEVERITY_RU if locale is Locale.RU else _SEVERITY_EN
    lines.append(("Уровень внимания: " if locale is Locale.RU else "Attention: ") + labels[attention])
    if response.unverified_numbers:
        numbers = ", ".join(response.unverified_numbers)
        lines.append(
            
                f"Числа без подтверждения в пакете: {numbers}"
                if locale is Locale.RU
                else f"Numbers not backed by the package: {numbers}"
            
        )
    return lines


def render_report(
    profile: ReportProfile,
    package: AnalysisPackage,
    now: datetime,
    incidents: Sequence[IncidentDigest] = (),
    response: AIResponse | None = None,
    local_severity: Severity = Severity.INFO,
    detail: DetailLevel | None = None,
) -> RenderedReport:
    """Собрать текст отчёта: факты локально, ИИ — отдельным блоком."""
    data_quality_issue = any(
        measurement.quality not in {Quality.OK}
        for system in package.systems
        for measurement in system.measurements
    )
    chosen = detail or choose_detail(profile, incidents, data_quality_issue)
    locale = profile.locale
    detailed = chosen in {DetailLevel.STANDARD, DetailLevel.DETAILED}
    lines = _header(profile, package, now)

    if chosen is not DetailLevel.COMPACT:
        lines.append("")
        lines.append("**Системы**" if locale is Locale.RU else "**Systems**")
        for system in package.systems:
            if profile.system_ids and system.id not in profile.system_ids:
                continue
            lines.extend(_system_lines(system, locale, detailed))

    lines.extend(_incident_lines(incidents, locale))

    attention = local_severity
    if response is not None:
        from .ai import apply_attention_floor

        attention = apply_attention_floor(local_severity, response)
        lines.extend(_ai_lines(response, locale, attention))
    else:
        lines.extend(_fallback_lines(package, locale))

    warnings: list[str] = []
    unverified = response.unverified_numbers if response else ()
    if unverified:
        warnings.append(
            "unverified_numbers_present" if locale is Locale.EN else "есть неподтверждённые числа"
        )
    if data_quality_issue:
        warnings.append("data_quality_degraded" if locale is Locale.EN else "качество данных снижено")

    return RenderedReport(
        text="\n".join(lines),
        detail=chosen,
        used_ai=response is not None,
        attention=attention,
        unverified_numbers=unverified,
        warnings=tuple(warnings),
    )


def critical_alert(profile: ReportProfile, digest: IncidentDigest, now: datetime) -> str:
    """Экстренное сообщение независимо от планового отчёта (REP-04)."""
    moment = local_moment(now, profile.timezone)
    peak = (
        format_number(digest.peak_deviation, digest.peak_unit, profile.locale)
        if digest.peak_deviation is not None
        else "—"
    )
    if profile.locale is Locale.RU:
        return (
            f"🚨 {digest.rule_name}\n"
            f"{moment:%d.%m.%Y %H:%M} ({profile.timezone})\n"
            f"Длительность: {format_duration(digest.duration_seconds, profile.locale)}\n"
            f"Пиковое отклонение: {peak}"
        )
    return (
        f"🚨 {digest.rule_name}\n"
        f"{moment:%Y-%m-%d %H:%M} ({profile.timezone})\n"
        f"Duration: {format_duration(digest.duration_seconds, profile.locale)}\n"
        f"Peak deviation: {peak}"
    )


@dataclass(slots=True)
class ReportSchedule:
    """Расписание отчёта (REP-05)."""

    profile_id: str
    at: tuple[int, int] | None = None
    weekdays: tuple[int, ...] = ()
    interval: timedelta | None = None
    missed_policy: str = "skip"
    last_run: datetime | None = None
    next_run: datetime | None = None

    def due(self, now: datetime) -> bool:
        return self.next_run is not None and now >= self.next_run

    def mark_run(self, now: datetime, success: bool = True) -> None:
        self.last_run = now
        if self.interval is not None:
            self.next_run = now + self.interval
        elif self.next_run is not None:
            self.next_run = self.next_run + timedelta(days=1)
