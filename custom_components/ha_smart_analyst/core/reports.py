"""Отчёты: профили, локальные факты и отдельный блок ИИ (REP-01…REP-07).

Ключевое правило: числовые факты рендерятся локально и не зависят от ответа
модели. Блок ИИ явно отделён, а его непроверенные числа помечаются.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta, tzinfo
from enum import StrEnum
from zoneinfo import ZoneInfo

from .ai import AIResponse, AnalysisPackage, Fact, MeasurementFact, SystemSection
from .engine import Severity
from .history import Sample, Window, counter_total, summarize
from .incidents import IncidentDigest
from .model import Measurement, MeasurementRole
from .observations import Observation, Quality
from .units import compatible, convert

#: Дни недели в порядке Python: 0 — понедельник, 6 — воскресенье.
WEEKDAY_NAMES_RU = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")


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
    """Профиль отчёта (REP-01).

    ``schedule_times`` — локальные часы вида ``"08:00"``; пусто — отчёт только
    вручную. ``schedule_weekdays`` — дни Python (0 = Пн); пусто — каждый день.
    """

    id: str
    name: str
    detail: DetailLevel = DetailLevel.ADAPTIVE
    locale: Locale = Locale.RU
    timezone: str = "UTC"
    report_type: ReportType = ReportType.ON_DEMAND
    enabled: bool = True
    system_ids: tuple[str, ...] = ()
    history_seconds: int = 1_800
    include_warnings: bool = True
    recipients: tuple[str, ...] = ()
    schedule_times: tuple[str, ...] = ()
    schedule_weekdays: tuple[int, ...] = ()
    required_measurement_ids: tuple[str, ...] = ()
    optional_measurement_ids: tuple[str, ...] = ()
    measurement_order: tuple[str, ...] = ()
    max_optional: int = 8
    group_by_system: bool = True
    show_states: bool = True

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
            "schedule_times": list(self.schedule_times),
            "schedule_weekdays": list(self.schedule_weekdays),
            "required_measurement_ids": list(self.required_measurement_ids),
            "optional_measurement_ids": list(self.optional_measurement_ids),
            "measurement_order": list(self.measurement_order),
            "max_optional": self.max_optional,
            "group_by_system": self.group_by_system,
            "show_states": self.show_states,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> ReportProfile:
        schedule_times = tuple(
            slot.strftime("%H:%M") for slot in parse_schedule_times(payload.get("schedule_times", ()))
        )
        weekdays = tuple(sorted({int(item) for item in payload.get("schedule_weekdays", ())}))  # type: ignore[union-attr]
        if any(day < 0 or day > 6 for day in weekdays):
            raise ValueError("дни недели задаются числами 0…6 (0 — понедельник)")
        raw_type = payload.get("report_type")
        if raw_type:
            report_type = ReportType(raw_type)
            if report_type is ReportType.SCHEDULED and not schedule_times:
                report_type = ReportType.ON_DEMAND
        else:
            report_type = ReportType.SCHEDULED if schedule_times else ReportType.ON_DEMAND
        return cls(
            id=str(payload.get("id") or uuid.uuid4()),
            name=str(payload.get("name") or "Отчёт"),
            detail=DetailLevel(payload.get("detail", "adaptive")),
            locale=Locale(payload.get("locale", "ru")),
            timezone=str(payload.get("timezone", "UTC")),
            report_type=report_type,
            enabled=bool(payload.get("enabled", True)),
            system_ids=tuple(payload.get("system_ids", ())),  # type: ignore[arg-type]
            history_seconds=int(payload.get("history_seconds", 1_800)),
            include_warnings=bool(payload.get("include_warnings", True)),
            recipients=tuple(payload.get("recipients", ())),  # type: ignore[arg-type]
            schedule_times=schedule_times,
            schedule_weekdays=weekdays,
            required_measurement_ids=tuple(
                str(item) for item in payload.get("required_measurement_ids", ())  # type: ignore[union-attr]
            ),
            optional_measurement_ids=tuple(
                str(item) for item in payload.get("optional_measurement_ids", ())  # type: ignore[union-attr]
            ),
            measurement_order=tuple(
                str(item) for item in payload.get("measurement_order", ())  # type: ignore[union-attr]
            ),
            max_optional=int(payload.get("max_optional", 8)),
            group_by_system=bool(payload.get("group_by_system", True)),
            show_states=bool(payload.get("show_states", True)),
        )


def parse_schedule_times(values: object) -> tuple[time, ...]:
    """Разобрать список ``"HH:MM"``; неверный формат — понятная ошибка."""
    if values in (None, "", ()):
        return ()
    if isinstance(values, str):
        candidates: Sequence[object] = [item for item in values.split(",")]
    elif isinstance(values, (list, tuple)):
        candidates = values
    else:
        raise ValueError("время отчёта задаётся строкой или списком строк HH:MM")
    parsed: list[time] = []
    for raw in candidates:
        text = str(raw).strip()
        if not text:
            continue
        try:
            hour_text, minute_text = text.split(":", 1)
            parsed.append(time(int(hour_text), int(minute_text)))
        except (ValueError, TypeError) as err:
            raise ValueError(f"неверное время {text!r}; ожидается HH:MM") from err
    return tuple(sorted(set(parsed)))


def profile_zone(profile: ReportProfile) -> tzinfo:
    """Часовой пояс профиля; неизвестный пояс не ломает планировщик."""
    try:
        return ZoneInfo(profile.timezone or "UTC")
    except Exception:  # noqa: BLE001 - нет tzdata или неизвестная зона
        return UTC


def next_run_at(profile: ReportProfile, now: datetime) -> datetime | None:
    """Ближайший запуск профиля в UTC или ``None`` для ручного отчёта."""
    slots = parse_schedule_times(profile.schedule_times)
    if not profile.enabled or not slots:
        return None
    zone = profile_zone(profile)
    moment = now if now.tzinfo else now.replace(tzinfo=UTC)
    local_now = moment.astimezone(zone)
    weekdays = set(profile.schedule_weekdays)
    for offset in range(0, 8):
        day = (local_now + timedelta(days=offset)).date()
        if weekdays and day.weekday() not in weekdays:
            continue
        for slot in slots:
            candidate = datetime.combine(day, slot, tzinfo=zone)
            if candidate > local_now:
                return candidate.astimezone(UTC)
    return None


def describe_schedule(profile: ReportProfile) -> str:
    """Короткое описание расписания для ответов API и панели."""
    slots = parse_schedule_times(profile.schedule_times)
    if not slots:
        return "вручную"
    times = ", ".join(slot.strftime("%H:%M") for slot in slots)
    weekdays = set(profile.schedule_weekdays)
    if not weekdays:
        days = "ежедневно"
    elif weekdays == set(range(5)):
        days = "по будням"
    elif weekdays == {5, 6}:
        days = "по выходным"
    else:
        days = ", ".join(WEEKDAY_NAMES_RU[day] for day in sorted(weekdays))
    suffix = "" if profile.enabled else " (выключен)"
    return f"{days}, {times}{suffix}"


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


#: Порядок важности показателей в компактном отчёте.
_ROLE_PRIORITY = {
    MeasurementRole.LEAK: 0,
    MeasurementRole.FAULT: 0,
    MeasurementRole.ACTUAL: 1,
    MeasurementRole.SUPPLY: 2,
    MeasurementRole.RETURN: 3,
    MeasurementRole.SETPOINT: 4,
    MeasurementRole.TEMPERATURE_INDOOR: 5,
    MeasurementRole.TEMPERATURE_OUTDOOR: 6,
    MeasurementRole.POWER: 7,
    MeasurementRole.ENERGY: 8,
    MeasurementRole.VOLTAGE_PHASE: 9,
    MeasurementRole.PRESSURE: 10,
    MeasurementRole.FLOW: 11,
    MeasurementRole.RUNNING: 12,
    MeasurementRole.MODE: 13,
    MeasurementRole.OCCUPANCY: 14,
    MeasurementRole.CUSTOM: 15,
}


def compact_omitted(systems: Sequence[SystemSection], profile: ReportProfile) -> int:
    """Сколько показаний не поместилось в компактный отчёт (признак неполноты)."""
    visible = {
        measurement.id
        for system in systems
        for measurement in system.measurements
        if measurement.required or measurement_is_visible(profile, measurement)
    }
    return max(0, len(visible) - len(compact_measurements(systems, profile)))


def measurement_is_visible(profile: ReportProfile, measurement: MeasurementFact) -> bool:
    """Показывать ли необязательное показание: числа всегда, состояния — по настройке."""
    if isinstance(measurement.value, (int, float)) and not isinstance(measurement.value, bool):
        return True
    return profile.show_states and measurement.value is not None


def measurement_rank(
    profile: ReportProfile, measurement: MeasurementFact
) -> tuple[int, int, int]:
    """Порядок вывода: явный список, затем обязательные, дополнительные, остальные."""
    try:
        explicit = profile.measurement_order.index(measurement.id)
    except ValueError:
        explicit = len(profile.measurement_order)
    if measurement.required or measurement.id in profile.required_measurement_ids:
        group = 0
    elif measurement.id in profile.optional_measurement_ids:
        group = 1
    else:
        group = 2
    return (explicit, group, _ROLE_PRIORITY.get(measurement.role, 99))


def compact_measurements(
    systems: Sequence[SystemSection], profile: ReportProfile
) -> list[tuple[str, MeasurementFact]]:
    """Ключевые показатели для короткого отчёта.

    Обязательные показатели профиля не отсекаются никогда; лимит действует
    только на дополнительные и автоматически подобранные значения.
    """
    required: list[tuple[tuple[int, int, int], str, MeasurementFact]] = []
    optional: list[tuple[tuple[int, int, int], str, MeasurementFact]] = []
    seen: set[str] = set()
    for system in systems:
        for measurement in system.measurements:
            if measurement.id in seen:
                continue
            rank = measurement_rank(profile, measurement)
            if measurement.required:
                required.append((rank, system.name, measurement))
                seen.add(measurement.id)
                continue
            if not measurement_is_visible(profile, measurement):
                continue
            optional.append((rank, system.name, measurement))
    required.sort(key=lambda item: item[0])
    optional.sort(key=lambda item: item[0])
    limit = max(0, profile.max_optional)
    selected = required + optional[:limit]
    return [(system_name, measurement) for _, system_name, measurement in selected]


def system_coverage(coverages: Sequence[float]) -> float | None:
    """Покрытие системы — среднее по датчикам, а не лучший датчик.

    Максимум одного источника нельзя выдавать за качество истории всей системы.
    Пустой список означает «покрытие неизвестно», а не ноль.
    """
    values = [value for value in coverages if value is not None]
    return sum(values) / len(values) if values else None


def pair_facts(
    measurements: Sequence[Measurement], observations: dict[str, Observation]
) -> tuple[Fact, ...]:
    """Отклонение «факт − уставка» только внутри одного оборудования (SEM-04).

    Если оборудование не задано и пара неоднозначна, факт не выдумывается.
    """
    groups: dict[str | None, list[Measurement]] = {}
    for measurement in measurements:
        groups.setdefault(measurement.equipment_id, []).append(measurement)
    facts: list[Fact] = []
    for equipment_id, group in groups.items():
        actuals = [item for item in group if item.role is MeasurementRole.ACTUAL]
        setpoints = [item for item in group if item.role is MeasurementRole.SETPOINT]
        if equipment_id is None and (len(actuals) != 1 or len(setpoints) != 1):
            continue
        if not actuals or not setpoints:
            continue
        actual, setpoint = actuals[0], setpoints[0]
        actual_observation = observations.get(actual.id)
        setpoint_observation = observations.get(setpoint.id)
        if (
            actual_observation is None
            or setpoint_observation is None
            or actual_observation.numeric_value is None
            or setpoint_observation.numeric_value is None
        ):
            continue
        actual_unit = actual_observation.unit or actual.unit
        setpoint_unit = setpoint_observation.unit or setpoint.unit
        setpoint_value = setpoint_observation.numeric_value
        if setpoint_unit and actual_unit and setpoint_unit != actual_unit:
            if not compatible(setpoint_unit, actual_unit):
                continue
            setpoint_value = convert(setpoint_value, setpoint_unit, actual_unit)
        facts.append(
            Fact(
                id=f"fact_gap_{actual.id}",
                kind="difference",
                value=round(actual_observation.numeric_value - setpoint_value, 3),
                unit=actual_unit,
                label=f"{actual.name} − {setpoint.name}",
                source=actual.id,
            )
        )
    return tuple(facts)


def history_facts(
    measurement_id: str,
    name: str,
    samples: Sequence[Sample],
    window: Window,
    unit: str | None = None,
    *,
    counter: bool = False,
) -> tuple[Fact, ...]:
    """Локально рассчитанные факты истории с идентификаторами для ссылок ИИ.

    ``counter=True`` — накопительный счётчик: изменение считается с учётом
    сбросов, а не как разность крайних значений.
    """
    aggregate = summarize(samples, window)
    prefix = f"fact_{measurement_id}"
    facts: list[Fact] = []

    def add(suffix: str, kind: str, value: float | int, unit_: str | None, label: str) -> None:
        facts.append(
            Fact(
                f"{prefix}_{suffix}",
                kind,
                value,
                unit_,
                f"{name}: {label}",
                source=measurement_id,
            )
        )

    if not aggregate.usable:
        add(
            "coverage",
            "coverage",
            round(aggregate.coverage * 100, 1),
            "%",
            "покрытие истории (недостаточно данных)",
        )
        return tuple(facts)
    if counter:
        total, resets = counter_total(samples, unit)
        if total is not None:
            add("total", "counter_total", round(total, 3), unit, "прирост за период")
        if resets:
            add("resets", "counter_resets", resets, None, "сбросов счётчика")
    elif aggregate.delta is not None:
        add("delta", "delta", round(aggregate.delta, 3), unit, "изменение за период")
    if aggregate.first is not None:
        add("start", "start", round(aggregate.first, 3), unit, "в начале")
    if aggregate.last is not None:
        add("end", "end", round(aggregate.last, 3), unit, "в конце")
    if aggregate.mean is not None:
        add("mean", "mean", round(aggregate.mean, 3), unit, "среднее")
    if aggregate.minimum is not None:
        add("min", "min", round(aggregate.minimum, 3), unit, "минимум")
    if aggregate.maximum is not None:
        add("max", "max", round(aggregate.maximum, 3), unit, "максимум")
    add("count", "count", aggregate.count, None, "пригодных измерений")
    add("coverage", "coverage", round(aggregate.coverage * 100, 1), "%", "покрытие истории")
    return tuple(facts)


def history_trend(
    samples: Sequence[Sample], window: Window, unit: str | None, locale: Locale = Locale.RU
) -> tuple[float | None, str | None]:
    """Покрытие и текст тренда; при недостатке данных — ``None``."""
    aggregate = summarize(samples, window)
    if not aggregate.usable or aggregate.delta is None:
        return aggregate.coverage, None
    if abs(aggregate.delta) < 1e-9:
        return aggregate.coverage, ("без изменений" if locale is Locale.RU else "stable")
    amount = format_number(abs(aggregate.delta), unit, locale)
    if locale is Locale.RU:
        return aggregate.coverage, ("рост на " if aggregate.delta > 0 else "снижение на ") + amount
    return aggregate.coverage, ("up by " if aggregate.delta > 0 else "down by ") + amount


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


def _format_value(value: object, unit: str | None, locale: Locale) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return format_number(float(value), unit, locale)
    if isinstance(value, bool):
        if locale is Locale.RU:
            return "включено" if value else "выключено"
        return "on" if value else "off"
    return "—" if value is None else str(value)


def _system_lines(system: SystemSection, locale: Locale, detailed: bool) -> list[str]:
    lines = [f"• {system.name}"]
    for measurement in system.measurements:
        quality = (_QUALITY_RU if locale is Locale.RU else _QUALITY_EN)[measurement.quality]
        rendered = _format_value(measurement.value, measurement.unit, locale)
        suffix = "" if measurement.quality is Quality.OK else f" [{quality}]"
        lines.append(f"  – {measurement.name}: {rendered}{suffix}")
    if detailed:
        for fact in system.facts:
            rendered = _format_value(fact.value, fact.unit, locale)
            lines.append(f"  · {fact.label or fact.id}: {rendered}")
        trend = system.trend or ("нет данных" if locale is Locale.RU else "no data")
        label = "покрытие истории" if locale is Locale.RU else "history coverage"
        if system.coverage is None:
            lines.append(f"  · {label}: — ({trend})")
        else:
            lines.append(f"  · {label}: {system.coverage:.0%}; {trend}")
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


def _fallback_lines(
    package: AnalysisPackage, locale: Locale, ai_error: str | None = None
) -> list[str]:
    """Шаблонный отчёт без ИИ (AI-07, AC-11)."""
    reason = f" Причина: {ai_error}." if ai_error else ""
    if locale is Locale.RU:
        lines = [
            "",
            "**Аналитическое заключение недоступно**",
            f"ИИ не ответил или ответ не прошёл проверку.{reason} Ниже — локальные факты.",
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
        f"The model did not answer or its response failed validation.{reason} Local facts follow.",
    ]


def _compact_lines(package: AnalysisPackage, profile: ReportProfile) -> list[str]:
    """Короткая сводка с ключевыми цифрами — показывается всегда (REP-04)."""
    locale = profile.locale
    selected = compact_measurements(package.systems, profile)
    if locale is Locale.RU:
        lines = ["", "**Ключевые показатели**"]
        if not selected:
            lines.append("• Числовых показаний нет — проверьте, что датчики добавлены и доступны.")
            return lines
        for system_name, measurement in selected:
            quality = (
                ""
                if measurement.quality is Quality.OK
                else f" [{_QUALITY_RU[measurement.quality]}]"
            )
            lines.append(
                f"• {system_name}: {measurement.name} — "
                f"{_format_value(measurement.value, measurement.unit, locale)}{quality}"
            )
        omitted = compact_omitted(package.systems, profile)
        if omitted:
            lines.append(
                f"• Не показано из-за лимита: {omitted}. "
                "Увеличьте «Лимит дополнительных» в профиле."
            )
        return lines
    lines = ["", "**Key readings**"]
    if not selected:
        lines.append("• No numeric readings — check that sensors are added and available.")
        return lines
    for system_name, measurement in selected:
        quality = (
            "" if measurement.quality is Quality.OK else f" [{_QUALITY_EN[measurement.quality]}]"
        )
        lines.append(
            f"• {system_name}: {measurement.name} — "
            f"{_format_value(measurement.value, measurement.unit, locale)}{quality}"
        )
    omitted = compact_omitted(package.systems, profile)
    if omitted:
        lines.append(
            f"• Hidden by the limit: {omitted}. Increase max optional in the profile."
        )
    return lines


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
    ai_error: str | None = None,
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

    if chosen is DetailLevel.COMPACT:
        lines.extend(_compact_lines(package, profile))
    else:
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
        lines.extend(_fallback_lines(package, locale, ai_error))

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
