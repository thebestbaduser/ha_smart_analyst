"""Координатор интеграции: подписки, оценка правил, инциденты и диагностика.

События HA обрабатываются асинхронно, история читается в executor, а
вычислитель ядра получает готовый снимок. Координатор не управляет
устройствами: версия 1.0 только читает состояние (SEC-01).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    ASSESS_INTERVAL_SECONDS,
    CONF_AI_ENABLED,
    CONF_AI_MODEL,
    CONF_HISTORY_TIMEOUT,
    CONF_INCIDENT_RETENTION_DAYS,
    CONF_LANGUAGE,
    CONF_QUIET_END,
    CONF_QUIET_START,
    CONF_TELEGRAM_RECIPIENTS,
    CONF_TELEGRAM_SERVICE,
    DEFAULT_AI_BASE_URL,
    DEFAULT_AI_MODEL,
    DEFAULT_HISTORY_TIMEOUT,
    DOMAIN,
    EVENT_DATA_QUALITY,
    EVENT_INCIDENT_OPENED,
    EVENT_INCIDENT_RESOLVED,
    SAVE_DELAY_SECONDS,
    SCAN_INTERVAL,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from .core import (
    AIResponse,
    AnalysisPackage,
    ConfigState,
    EvaluationContext,
    Fact,
    HomeModel,
    Incident,
    IncidentManager,
    Locale,
    Measurement,
    MeasurementFact,
    MeasurementRole,
    MessagePriority,
    NotificationQueue,
    Observation,
    Quality,
    QuietHours,
    RenderedReport,
    ReportProfile,
    ReportType,
    Rule,
    RuleEvaluation,
    RuleEvent,
    RuleEventKind,
    RuleRuntime,
    Severity,
    Site,
    SmartAnalystError,
    SourceRef,
    StorageError,
    System,
    SystemPreset,
    SystemSection,
    ValidationStatus,
    ValueType,
    Window,
    build_observation,
    build_package,
    compute_composite,
    digest,
    evaluate_rule,
    export_configuration,
    history_facts,
    history_trend,
    load_config,
    measurement_rank,
    new_identifier,
    next_run_at,
    pair_facts,
    parse_payload,
    parse_rule,
    referenced_measurements,
    render_report,
    system_coverage,
    trim_package,
    validate_response,
)
from .core.observations import unconfigured
from .history_provider import CachedHistory, HistorySource
from .provider import DeepSeekProvider, ProviderConfig, ProviderError

_LOGGER = logging.getLogger(__name__)

HISTORY_DEFAULT_WINDOW = 3_600
DEFAULT_TEST_TEXT = "Проверка уведомлений HA Smart Analyst"
#: Повторный клик в этом окне возвращает тот же отчёт и не отправляет его снова.
REPORT_DEDUPE_SECONDS = 10


@dataclass
class AnalystSnapshot:
    """Данные для диагностических сущностей (NFR-05)."""

    generated_at: datetime
    rules_total: int = 0
    rules_active: int = 0
    active_incidents: int = 0
    critical_incidents: int = 0
    queue_pending: int = 0
    stale_sources: int = 0
    ai_available: bool = True
    ai_error: str | None = None
    last_report_at: datetime | None = None
    recorder_available: bool = True
    tokens_used: int = 0
    measurements_total: int = 0


class SmartAnalystCoordinator(DataUpdateCoordinator[AnalystSnapshot]):
    """Оценка правил по событиям HA и обслуживание инцидентов."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}:{entry.entry_id}",
            update_interval=timedelta(seconds=SCAN_INTERVAL),
            config_entry=entry,
        )
        self.entry = entry
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY}.{entry.entry_id}")
        self.config_state = ConfigState()
        self.model: HomeModel = self.config_state.model
        self.rules: dict[str, Rule] = {}
        self.runtimes: dict[str, RuleRuntime] = {}
        self.incidents = IncidentManager()
        self.queue = NotificationQueue()
        self._unsubs: list[CALLBACK_TYPE] = []
        self._history: CachedHistory | None = None
        self._evaluating = False
        self._pending = False
        self._tasks: set[asyncio.Task[Any]] = set()
        self._ai_provider: DeepSeekProvider | None = None
        self._ai_signature: tuple[str, str, str] | None = None
        self._ai_error: str | None = None
        self._last_report_at: datetime | None = None
        self._tokens_used = 0
        self.last_delivery: dict[str, Any] | None = None
        self._report_next: dict[str, datetime] = {}
        self._reports_in_flight: dict[tuple[str, bool, str], asyncio.Task[RenderedReport]] = {}
        self._report_cache: dict[tuple[str, bool, str], tuple[datetime, RenderedReport]] = {}

    # -- жизненный цикл -----------------------------------------------
    async def async_initialize(self) -> None:
        """Загрузить конфигурацию и подписаться на источники."""
        raw = await self._store.async_load()
        now = dt_util.utcnow()
        if raw:
            try:
                state, _backup = load_config(json.dumps(raw, ensure_ascii=False), now)
            except StorageError as err:
                raise UpdateFailed(f"конфигурация повреждена: {err}") from err
            self.config_state = state
            self.model = state.model
        self._apply_settings()
        self.incidents = IncidentManager.from_list(self.config_state.incidents, now)
        self.queue = NotificationQueue.from_list(self.config_state.notifications, now)
        self.queue.restore()
        self._parse_rules()
        self._restore_runtimes(now)
        self._refresh_report_schedule(now)
        self._subscribe()
        await self._async_evaluate()
        await self._async_flush_queue()

    @callback
    def _subscribe(self) -> None:
        entities = sorted(
            {
                measurement.source.entity_id
                for measurement in self.model.measurements.values()
                if measurement.source is not None
            }
        )
        if entities:
            self._unsubs.append(
                async_track_state_change_event(self.hass, entities, self._async_on_state_change)
            )
        self._unsubs.append(
            async_track_time_interval(
                self.hass, self._async_periodic, timedelta(seconds=ASSESS_INTERVAL_SECONDS)
            )
        )
        self._unsubs.append(
            async_track_time_interval(self.hass, self._async_report_tick, timedelta(minutes=1))
        )

    async def async_shutdown(self) -> None:
        for unsubscribe in self._unsubs:
            unsubscribe()
        self._unsubs.clear()
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()

    @callback
    def _async_on_state_change(self, event: Event) -> None:
        self.hass.async_create_task(self._async_evaluate())

    async def _async_periodic(self, now: datetime) -> None:
        await self._async_evaluate()

    # -- расписание отчётов (REP-05) ----------------------------------
    async def _async_report_tick(self, now: datetime) -> None:
        await self.async_run_due_reports(now)

    def _refresh_report_schedule(self, now: datetime | None = None) -> None:
        """Пересчитать ближайшие запуски профилей."""
        moment = now or dt_util.utcnow()
        upcoming: dict[str, datetime] = {}
        for payload in self.config_state.report_profiles:
            try:
                profile = ReportProfile.from_dict(payload)
            except (ValueError, TypeError) as err:
                _LOGGER.warning("Профиль отчёта пропущен: %s", err)
                continue
            when = next_run_at(profile, moment)
            if when is not None:
                upcoming[profile.id] = when
        self._report_next = upcoming

    def next_report_runs(self) -> dict[str, str]:
        """Ближайшие запуски для панели: id профиля → ISO-время UTC."""
        return {profile_id: when.isoformat() for profile_id, when in self._report_next.items()}

    async def async_run_due_reports(self, now: datetime | None = None) -> list[str]:
        """Сформировать отчёты, время которых наступило (REP-05).

        Пропущенные из-за выключенного HA запуски не догоняются пачкой:
        политика ``skip`` по умолчанию.
        """
        moment = now or dt_util.utcnow()
        due = [
            profile_id
            for profile_id, when in self._report_next.items()
            if when <= moment
        ]
        completed: list[str] = []
        for profile_id in due:
            try:
                await self.async_generate_report(profile_id)
            except Exception as err:  # noqa: BLE001 - один профиль не ломает остальные
                _LOGGER.warning("Отчёт %s не сформирован: %s", profile_id, err)
            else:
                completed.append(profile_id)
            self._report_next.pop(profile_id, None)
        if due:
            self._refresh_report_schedule(moment)
        return completed

    # -- настройки ----------------------------------------------------
    def _apply_settings(self) -> None:
        options = dict(self.entry.options)
        data = dict(self.entry.data)
        merged = {**data, **options}
        settings = self.config_state.settings
        settings["locale"] = merged.get(CONF_LANGUAGE, settings.get("locale", "ru"))
        settings["telegram_recipients"] = list(merged.get(CONF_TELEGRAM_RECIPIENTS, settings.get("telegram_recipients", [])))
        settings["telegram_service"] = merged.get(CONF_TELEGRAM_SERVICE, settings.get("telegram_service"))
        settings["quiet_hours"] = {
            "start": merged.get(CONF_QUIET_START, "22:00:00"),
            "end": merged.get(CONF_QUIET_END, "07:00:00"),
        }
        settings["ai_enabled"] = bool(merged.get(CONF_AI_ENABLED, settings.get("ai_enabled", True)))
        settings["ai_model"] = merged.get(CONF_AI_MODEL, settings.get("ai_model"))
        settings["incident_retention_days"] = int(
            merged.get(CONF_INCIDENT_RETENTION_DAYS, settings.get("incident_retention_days", 90))
        )
        try:
            timeout = float(
                merged.get(
                    CONF_HISTORY_TIMEOUT,
                    settings.get("history_timeout_seconds", DEFAULT_HISTORY_TIMEOUT),
                )
            )
        except (TypeError, ValueError):
            timeout = DEFAULT_HISTORY_TIMEOUT
        settings["history_timeout_seconds"] = timeout

    def _quiet_hours(self) -> QuietHours | None:
        raw = self.config_state.settings.get("quiet_hours") or {}
        try:
            return QuietHours(time.fromisoformat(raw["start"]), time.fromisoformat(raw["end"]))
        except (KeyError, ValueError):
            return None

    # -- конфигурация -------------------------------------------------
    def _parse_rules(self) -> None:
        self.rules = {}
        for payload in self.config_state.rules:
            try:
                rule = parse_rule(payload, self.model)
            except SmartAnalystError as err:
                _LOGGER.warning("Правило пропущено (%s): %s", payload.get("name"), err)
                continue
            self.rules[rule.id] = rule

    def _restore_runtimes(self, now: datetime) -> None:
        self.runtimes = {}
        for snapshot in self.config_state.runtime:
            try:
                runtime = RuleRuntime.from_dict(snapshot, now)
            except (KeyError, ValueError) as err:
                _LOGGER.warning("Состояние правила не восстановлено: %s", err)
                continue
            if runtime.rule_id in self.rules:
                self.runtimes[runtime.rule_id] = runtime
        for rule_id in self.rules:
            self.runtimes.setdefault(rule_id, RuleRuntime(rule_id))

    async def async_reload_configuration(self, state: ConfigState) -> None:
        """Применить новую конфигурацию без перезапуска HA (CFG-05)."""
        self.config_state = state
        self.model = state.model
        self._apply_settings()
        self._parse_rules()
        self._restore_runtimes(dt_util.utcnow())
        self._refresh_report_schedule()
        self._resubscribe()
        self._schedule_save()
        await self._async_evaluate()

    async def async_reload_options(self) -> None:
        """Перечитать Options Flow без потери накопленного состояния."""
        self._apply_settings()
        self._ai_provider = None
        self._ai_signature = None
        self._schedule_save()
        await self._async_evaluate()

    # -- редактирование модели (ENT-03, UI-04) ------------------------
    def _resubscribe(self) -> None:
        for unsubscribe in self._unsubs:
            unsubscribe()
        self._unsubs.clear()
        self._subscribe()

    def _ensure_site(self) -> Site:
        if self.model.site is None:
            self.model.set_site(
                Site(id=new_identifier(), name=self.entry.title or "Дом")
            )
        assert self.model.site is not None
        return self.model.site

    async def _after_model_change(self) -> None:
        self._parse_rules()
        self._resubscribe()
        self._schedule_save()
        await self._async_evaluate()

    async def async_create_system(self, name: str, preset: str = "custom") -> System:
        """Создать систему руками или по подтверждённой подсказке."""
        cleaned = name.strip()
        if not cleaned:
            raise SmartAnalystError("название системы не может быть пустым")
        try:
            preset_value = SystemPreset(preset)
        except ValueError as err:
            raise SmartAnalystError(f"неизвестный пресет системы {preset!r}") from err
        site = self._ensure_site()
        system = System(
            id=new_identifier(),
            site_id=site.id,
            name=cleaned,
            preset=preset_value,
            config_status=ValidationStatus.CONFIRMED,
        )
        self.model.add_system(system)
        await self._after_model_change()
        return system

    async def async_delete_system(self, system_id: str) -> None:
        """Удалить систему; правила, зависящие от её датчиков, блокируют действие."""
        measurement_ids = {item.id for item in self.model.measurements_of(system_id)}
        for rule in self.rules.values():
            if set(referenced_measurements(rule.expression)) & measurement_ids:
                raise SmartAnalystError(
                    f"правило {rule.name!r} использует датчики этой системы"
                )
        self.model.remove_system(system_id)
        await self._after_model_change()

    async def async_add_measurement(
        self,
        *,
        system_id: str,
        entity_id: str,
        name: str | None = None,
        role: str = "custom",
        unit: str | None = None,
        registry_entry_id: str | None = None,
        attribute: str | None = None,
        max_age_seconds: int | None = None,
    ) -> Measurement:
        """Добавить сущность в систему с подтверждённой ролью (ENT-02)."""
        if self.model.systems.get(system_id) is None:
            raise SmartAnalystError(f"неизвестная система {system_id!r}")
        state = self.hass.states.get(entity_id)
        if state is None:
            raise SmartAnalystError(f"сущность {entity_id!r} не найдена в Home Assistant")
        try:
            role_value = MeasurementRole(role)
        except ValueError as err:
            raise SmartAnalystError(f"неизвестная роль измерения {role!r}") from err
        measurement = Measurement(
            id=new_identifier(),
            name=(name or state.name or entity_id).strip(),
            role=role_value,
            unit=unit or state.attributes.get("unit_of_measurement"),
            system_id=system_id,
            source=SourceRef(
                entity_id=entity_id,
                registry_entry_id=registry_entry_id,
                attribute=attribute,
            ),
            config_status=ValidationStatus.CONFIRMED,
            max_age_seconds=max_age_seconds,
        )
        self.model.add_measurement(measurement)
        await self._after_model_change()
        return measurement

    async def async_delete_measurement(self, measurement_id: str) -> None:
        """Удалить измерение, если на него не ссылаются правила и составные источники."""
        for rule in self.rules.values():
            if measurement_id in referenced_measurements(rule.expression):
                raise SmartAnalystError(f"правило {rule.name!r} использует этот датчик")
        self.model.remove_measurement(measurement_id)
        await self._after_model_change()

    async def async_save_rule(self, payload: dict[str, Any]) -> Rule:
        """Проверить и сохранить правило (RULE-08, CFG-05)."""
        rule = parse_rule(payload, self.model)
        canonical = rule.to_dict()
        replaced = False
        for index, existing in enumerate(self.config_state.rules):
            if str(existing.get("id")) == rule.id:
                self.config_state.rules[index] = canonical
                replaced = True
                break
        if not replaced:
            self.config_state.rules.append(canonical)
            self.runtimes.setdefault(rule.id, RuleRuntime(rule.id))
        self._parse_rules()
        self._schedule_save()
        await self._async_evaluate()
        return rule

    async def async_delete_rule(self, rule_id: str) -> None:
        """Удалить правило; инциденты и журнал остаются историей."""
        before = len(self.config_state.rules)
        self.config_state.rules = [
            item for item in self.config_state.rules if str(item.get("id")) != rule_id
        ]
        if len(self.config_state.rules) == before:
            raise SmartAnalystError(f"правило {rule_id!r} не найдено")
        self.rules.pop(rule_id, None)
        self.runtimes.pop(rule_id, None)
        self._schedule_save()
        await self._async_evaluate()

    async def async_save_profile(self, payload: dict[str, Any]) -> ReportProfile:
        """Сохранить профиль отчёта (REP-01)."""
        profile = ReportProfile.from_dict(payload)
        stored = profile.to_dict()
        for index, existing in enumerate(self.config_state.report_profiles):
            if str(existing.get("id")) == profile.id:
                self.config_state.report_profiles[index] = stored
                break
        else:
            self.config_state.report_profiles.append(stored)
        self._invalidate_report_cache(profile.id)
        self._refresh_report_schedule()
        self._schedule_save()
        return profile

    async def async_delete_profile(self, profile_id: str) -> None:
        before = len(self.config_state.report_profiles)
        self.config_state.report_profiles = [
            item for item in self.config_state.report_profiles if str(item.get("id")) != profile_id
        ]
        if len(self.config_state.report_profiles) == before:
            raise SmartAnalystError(f"профиль {profile_id!r} не найден")
        self._report_next.pop(profile_id, None)
        self._invalidate_report_cache(profile_id)
        self._schedule_save()

    # -- оценка -------------------------------------------------------
    def build_observations(self, now: datetime) -> dict[str, Observation]:
        """Снимок источников: атрибуты, отсутствующие сущности и составные измерения."""
        observations: dict[str, Observation] = {}
        for measurement in self.model.measurements.values():
            if measurement.source is None:
                continue
            entity_id = measurement.source.entity_id
            attribute = measurement.source.attribute
            try:
                state = self.hass.states.get(entity_id)
                if state is None:
                    observations[measurement.id] = unconfigured(
                        measurement.id, entity_id, now, measurement.unit
                    )
                    continue
                raw: object = state.state
                if attribute:
                    if attribute not in state.attributes:
                        observations[measurement.id] = unconfigured(
                            measurement.id, entity_id, now, measurement.unit
                        )
                        continue
                    raw = state.attributes.get(attribute)
                observations[measurement.id] = build_observation(
                    source_id=measurement.id,
                    entity_id=entity_id,
                    raw_state=raw,
                    now=now,
                    unit=state.attributes.get("unit_of_measurement") or measurement.unit,
                    last_changed=state.last_changed,
                    last_updated=state.last_updated,
                    last_reported=getattr(state, "last_reported", None),
                    expected_numeric=measurement.kind in {"numeric", "composite"}
                    or attribute is not None,
                    max_age=(
                        timedelta(seconds=measurement.max_age_seconds)
                        if measurement.max_age_seconds
                        else None
                    ),
                )
            except Exception as err:  # noqa: BLE001 - один датчик не скрывает остальные
                _LOGGER.warning("Датчик %s не прочитан: %s", measurement.name, err)
                observations[measurement.id] = unconfigured(
                    measurement.id, entity_id, now, measurement.unit
                )

        pending = [item for item in self.model.measurements.values() if item.composite is not None]
        for _ in range(5):
            progressed = False
            for measurement in list(pending):
                computed = compute_composite(measurement, observations)
                if computed is None:
                    continue
                observations[measurement.id] = computed
                pending.remove(measurement)
                progressed = True
            if not progressed:
                break
        for measurement in pending:
            observations[measurement.id] = unconfigured(
                measurement.id, f"composite:{measurement.id}", now, measurement.unit
            )
        return observations

    def _history_window(self) -> int:
        windows = [HISTORY_DEFAULT_WINDOW]
        for rule in self.rules.values():
            for seconds in _history_seconds(rule):
                windows.append(seconds)
        return max(windows)

    def _history_sources(self) -> dict[str, HistorySource]:
        return {
            measurement.id: HistorySource(
                entity_id=measurement.source.entity_id,
                attribute=measurement.source.attribute,
            )
            for measurement in self.model.measurements.values()
            if measurement.source is not None
        }

    def _history_timeout(self) -> float:
        try:
            value = float(
                self.config_state.settings.get(
                    "history_timeout_seconds", DEFAULT_HISTORY_TIMEOUT
                )
            )
        except (TypeError, ValueError):
            value = DEFAULT_HISTORY_TIMEOUT
        return max(0.5, value)

    async def _async_load_history(self, now: datetime) -> CachedHistory | None:
        sources = self._history_sources()
        if not sources:
            return None
        window = Window.last(now, self._history_window())
        timeout = self._history_timeout()
        try:
            async with asyncio.timeout(timeout):
                return await CachedHistory.async_load(
                    self.hass, window, sources, timeout
                )
        except TimeoutError:
            _LOGGER.warning(
                "История не загружена за %s с — правила с историей пропущены", timeout
            )
            return None
        except Exception as err:  # noqa: BLE001 - Recorder не должен ронять интеграцию
            _LOGGER.debug("История недоступна: %s", err)
            return None

    async def _async_evaluate(self) -> None:
        if self._evaluating:
            self._pending = True
            return
        self._evaluating = True
        try:
            while True:
                self._pending = False
                now = dt_util.utcnow()
                observations = self.build_observations(now)
                fast_rules = [
                    rule
                    for rule in self.rules.values()
                    if rule.enabled and not _history_seconds(rule)
                ]
                slow_rules = [
                    rule
                    for rule in self.rules.values()
                    if rule.enabled and _history_seconds(rule)
                ]

                self._history = None
                for rule in fast_rules:
                    await self._evaluate_rule(rule, observations, now, history=None)
                if slow_rules:
                    self._history = await self._async_load_history(now)
                    for rule in slow_rules:
                        await self._evaluate_rule(rule, observations, now, history=self._history)

                self._publish_snapshot(now, observations)
                self._schedule_save()
                if not self._pending:
                    break
        finally:
            self._evaluating = False

    async def _evaluate_rule(
        self,
        rule: Rule,
        observations: dict[str, Observation],
        now: datetime,
        history: CachedHistory | None,
    ) -> None:
        """Оценить одно правило и обработать его события."""
        runtime = self.runtimes.setdefault(rule.id, RuleRuntime(rule.id))
        context = EvaluationContext(
            model=self.model,
            observations=observations,
            now=now,
            history=history,
        )
        try:
            evaluation = evaluate_rule(rule, context)
        except SmartAnalystError as err:
            _LOGGER.warning("Ошибка вычисления правила %s: %s", rule.name, err)
            return
        events = runtime.update(rule, evaluation, now)
        for event in events:
            await self._async_handle_event(rule, runtime, event, now)

    async def _async_handle_event(
        self, rule: Rule, runtime: RuleRuntime, event: RuleEvent, now: datetime
    ) -> None:
        system_id = rule.scope_id
        if event.kind is RuleEventKind.ACTIVATED:
            incident = self.incidents.register(event, system_id=system_id)
            self.hass.bus.async_fire(
                EVENT_INCIDENT_OPENED,
                {
                    "incident_id": incident.incident_id,
                    "rule_id": rule.id,
                    "severity": incident.severity.value,
                    "system_id": system_id,
                },
            )
            await self._async_notify(
                event_key=f"{incident.incident_id}:{event.at.isoformat()}",
                incident=incident,
                title=f"Тревога: {rule.name}",
                text=_incident_text(rule, incident, event, resolved=False),
                priority=(
                    MessagePriority.CRITICAL
                    if incident.severity is Severity.CRITICAL
                    else MessagePriority.WARNING
                ),
            )
        elif event.kind is RuleEventKind.RECOVERED:
            incident = self._active_incident(rule, system_id)
            if incident is None:
                return
            resolved = self.incidents.resolve(incident.incident_id, event.at)
            self.hass.bus.async_fire(
                EVENT_INCIDENT_RESOLVED,
                {"incident_id": resolved.incident_id, "rule_id": rule.id},
            )
            if rule.on_recovery:
                await self._async_notify(
                    event_key=f"{resolved.incident_id}:recovered:{event.at.isoformat()}",
                    incident=resolved,
                    title=f"Норма восстановлена: {rule.name}",
                    text=_incident_text(rule, resolved, event, resolved=True),
                    priority=MessagePriority.INFO,
                )
        elif event.kind is RuleEventKind.REMINDER:
            incident = self._active_incident(rule, system_id)
            if incident is None:
                return
            await self._async_notify(
                event_key=f"{incident.incident_id}:repeat:{event.at.isoformat()}",
                incident=incident,
                title=f"Напоминание: {rule.name}",
                text=_incident_text(rule, incident, event, resolved=False),
                priority=MessagePriority.WARNING,
            )
        else:
            incident = self._active_incident(rule, system_id)
            if incident is not None:
                self.incidents.observe(incident.incident_id, event.at, event.reasons)
            self.hass.bus.async_fire(
                EVENT_DATA_QUALITY,
                {"rule_id": rule.id, "reasons": [reason.message for reason in event.reasons]},
            )

    def _active_incident(self, rule: Rule, system_id: str | None) -> Incident | None:
        for incident in self.incidents.active():
            if incident.rule_id == rule.id and incident.system_id == system_id:
                return incident
        return None

    # -- доставка -----------------------------------------------------
    def notification_targets(self) -> list[str]:
        """Куда доставлять: notify-сущность, адреса legacy-службы или persistent.

        Современный Telegram в HA создаёт notify-сущность
        (``notify.telegram_bot_<bot>_<chat>``) — ей chat_id не нужен.
        Старый способ — служба ``notify.<name>`` плюс chat_id получателей.
        """
        service = str(self.config_state.settings.get("telegram_service") or "").strip()
        if service.startswith("notify.") and self.hass.states.get(service) is not None:
            return [service]
        recipients = [
            str(item).strip()
            for item in (self.config_state.settings.get("telegram_recipients") or [])
            if str(item).strip()
        ]
        if service:
            return recipients or ["default"]
        return ["persistent"]

    async def _async_send_via_notify(self, recipient: str, text: str) -> bool:
        """Отправить через notify-сущность или legacy-службу."""
        service = str(self.config_state.settings.get("telegram_service") or "").strip()
        title = "HA Smart Analyst"
        name = service.removeprefix("notify.") if service.startswith("notify.") else service
        if service.startswith("notify.") and self.hass.states.get(service) is not None:
            await self.hass.services.async_call(
                "notify",
                "send_message",
                {"entity_id": service, "message": text, "title": title},
                blocking=True,
            )
            return True
        if not service:
            return False
        if not self.hass.services.has_service("notify", name):
            raise SmartAnalystError(
                f"не найдена ни уведомляющая сущность {service!r}, ни служба notify.{name}"
            )
        data: dict[str, Any] = {"message": text, "title": title}
        if recipient not in {"default", ""}:
            data["target"] = recipient
            if "telegram" in name.lower():
                data["data"] = {"chat_id": recipient}
        await self.hass.services.async_call("notify", name, data, blocking=True)
        return True

    async def async_send_test_notification(
        self, text: str = DEFAULT_TEST_TEXT, recipient: str | None = None
    ) -> dict[str, Any]:
        """Отправить проверку и вернуть результат по каждому адресу.

        Результат показывается в панели, сервисе и сущности-кнопке: «принято
        службой notify» значит только, что HA принял вызов; доставку в Telegram
        контролирует интеграция Telegram.
        """
        now = dt_util.utcnow()
        targets = [recipient] if recipient else self.notification_targets()
        results: list[dict[str, Any]] = []
        for target in targets:
            if target == "persistent":
                persistent_notification.async_create(
                    self.hass,
                    text,
                    title="HA Smart Analyst: проверка",
                    notification_id=f"{DOMAIN}_test_{int(now.timestamp())}",
                )
                results.append(
                    {"target": target, "sent": True, "detail": "внутреннее уведомление HA"}
                )
                continue
            try:
                await self._async_send_via_notify(target, text)
            except Exception as err:  # noqa: BLE001 - ошибку показываем пользователю
                results.append({"target": target, "sent": False, "detail": str(err)})
            else:
                results.append(
                    {"target": target, "sent": True, "detail": "принято службой notify"}
                )
        payload = {
            "at": now.isoformat(),
            "ok": all(item["sent"] for item in results),
            "results": results,
        }
        self.last_delivery = payload
        external = [item for item in results if item["target"] != "persistent"]
        if external:
            lines = [
                ("✅ " if item["sent"] else "❌ ") + f"{item['target']}: {item['detail']}"
                for item in external
            ]
            persistent_notification.async_create(
                self.hass,
                "\n".join(lines),
                title="HA Smart Analyst: результат проверки",
                notification_id=f"{DOMAIN}_test_result",
            )
        return payload

    async def _async_notify(
        self,
        *,
        event_key: str,
        incident: Incident,
        title: str,
        text: str,
        priority: MessagePriority,
    ) -> None:
        for recipient in self.notification_targets():
            message = self.queue.enqueue(
                event_key=event_key,
                recipient=recipient,
                priority=priority,
                text=text,
                now=dt_util.utcnow(),
                incident_id=incident.incident_id,
            )
            self.incidents.attach_message(incident.incident_id, message.key)
        await self._async_flush_queue()

    async def _async_flush_queue(self) -> None:
        quiet = self._quiet_hours()
        while True:
            message = self.queue.next_message(dt_util.utcnow(), quiet)
            if message is None:
                return
            if message.recipient == "persistent":
                persistent_notification.async_create(
                    self.hass,
                    message.text,
                    title="HA Smart Analyst",
                    notification_id=f"{DOMAIN}_{message.key}",
                )
                self.queue.mark_sent(message.key)
                continue
            try:
                sent = await self._async_send_via_notify(message.recipient, message.text)
            except Exception as err:  # noqa: BLE001 - служба уведомлений может отсутствовать
                _LOGGER.warning("Не удалось отправить уведомление: %s", err)
                self.queue.mark_failed(message.key, str(err), dt_util.utcnow())
                continue
            if not sent:
                self.queue.suppress(message.key)
            else:
                self.queue.mark_sent(message.key)

    async def async_acknowledge(self, incident_id: str, by: str) -> Incident:
        incident = self.incidents.acknowledge(incident_id, by, dt_util.utcnow())
        self._schedule_save()
        return incident

    async def async_resolve_manually(self, incident_id: str, by: str, note: str) -> Incident:
        incident = self.incidents.resolve_manually(incident_id, by, dt_util.utcnow(), note)
        self._schedule_save()
        return incident

    async def async_run_rule_test(self, rule_id: str) -> RuleEvaluation:
        rule = self.rules.get(rule_id)
        if rule is None:
            raise SmartAnalystError(f"неизвестное правило {rule_id!r}")
        now = dt_util.utcnow()
        history = await self._async_load_history(now) if _history_seconds(rule) else None
        context = EvaluationContext(
            model=self.model, observations=self.build_observations(now), now=now, history=history
        )
        return evaluate_rule(rule, context)

    # -- отчёты и ИИ --------------------------------------------------
    async def async_generate_report(
        self, profile_id: str | None = None, use_ai: bool | None = None
    ) -> RenderedReport:
        """Сформировать отчёт; повторный клик не дублирует отправку.

        Пока отчёт собирается, второй вызов ждёт тот же результат; в течение
        10 секунд после готовности возвращается тот же отчёт без новой отправки.
        """
        profile = self._profile(profile_id)
        effective_ai = self._effective_ai(use_ai)
        key = self._report_cache_key(profile, effective_ai)
        cached = self._report_cache.get(key)
        if (
            cached is not None
            and (dt_util.utcnow() - cached[0]).total_seconds() < REPORT_DEDUPE_SECONDS
        ):
            return cached[1]
        existing = self._reports_in_flight.get(key)
        if existing is not None and not existing.done():
            return await asyncio.shield(existing)
        task = self.hass.async_create_task(
            self._async_generate_report(profile, effective_ai)
        )
        self._reports_in_flight[key] = task
        try:
            rendered = await asyncio.shield(task)
        finally:
            if self._reports_in_flight.get(key) is task:
                self._reports_in_flight.pop(key, None)
        self._report_cache[key] = (dt_util.utcnow(), rendered)
        return rendered

    def _effective_ai(self, use_ai: bool | None) -> bool:
        """Режим ИИ, который реально определит содержимое отчёта."""
        if use_ai is not None:
            return bool(use_ai)
        return bool(self.config_state.settings.get("ai_enabled", True))

    def _report_cache_key(
        self, profile: ReportProfile, use_ai: bool
    ) -> tuple[str, bool, str]:
        """Ключ кэша: профиль, режим ИИ и ревизия влияющих настроек."""
        ai: dict[str, str | None] = {"model": None, "base_url": None, "key": None}
        if use_ai:
            config = self._ai_config()
            if config is not None:
                ai = {
                    "model": config.model,
                    "base_url": config.base_url,
                    "key": hashlib.sha256(config.api_key.encode("utf-8")).hexdigest()[:16],
                }
        payload = json.dumps(
            {"profile": profile.to_dict(), "ai": ai},
            sort_keys=True,
            ensure_ascii=False,
        )
        return (
            profile.id,
            use_ai,
            hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        )

    def _invalidate_report_cache(self, profile_id: str) -> None:
        for key in [key for key in self._report_cache if key[0] == profile_id]:
            self._report_cache.pop(key, None)

    async def _async_generate_report(
        self, profile: ReportProfile, use_ai: bool
    ) -> RenderedReport:
        now = dt_util.utcnow()
        observations = self.build_observations(now)
        window = Window.last(now, profile.history_seconds)
        sections, history_note = await self._async_build_sections(
            profile, observations, window
        )
        if not sections:
            raise SmartAnalystError(
                "в отчёте нет ни одного показания: добавьте систему и датчики "
                "или проверьте, что сущности доступны"
            )
        incident_digests = tuple(
            digest(incident, {rule_id: rule.name for rule_id, rule in self.rules.items()}, now)
            for incident in self.incidents.active()
        )
        package = build_package(
            site_id=self.model.site.id if self.model.site else "site",
            now=now,
            window_start=window.start,
            window_end=window.end,
            systems=sections,
            incidents=[
                {
                    "id": incident.incident_id,
                    "rule": self.rules[incident.rule_id].name
                    if incident.rule_id in self.rules
                    else incident.rule_id,
                    "severity": incident.severity.value,
                    "state": incident.state.value,
                    "peak_deviation": incident.peak_deviation,
                    "peak_unit": incident.peak_unit,
                }
                for incident in self.incidents.active()
            ],
        )
        response: AIResponse | None = None
        if use_ai:
            response = await self._async_analyze(package)
        ai_error = None if response is not None or not use_ai else self._ai_error
        local_severity = _max_severity(incident_digests)
        rendered = render_report(
            profile,
            package,
            now,
            incidents=incident_digests,
            response=response,
            local_severity=local_severity,
            ai_error=ai_error,
            history_note=history_note,
        )
        recipients = list(profile.recipients or self.notification_targets())
        for recipient in recipients:
            self.queue.enqueue(
                event_key=f"report:{now.isoformat()}:{profile.id}",
                recipient=recipient,
                priority=MessagePriority.INFO,
                text=rendered.text,
                now=now,
            )
        await self._async_flush_queue()
        self._last_report_at = now
        self._schedule_save()
        return rendered

    async def _async_analyze(self, package: AnalysisPackage) -> AIResponse | None:
        provider = self._provider()
        if provider is None:
            self._ai_error = (
                "провайдер ИИ не настроен: добавьте ключ DeepSeek в настройках интеграции"
            )
            return None
        try:
            raw = await provider.async_analyze(trim_package(package))
        except ProviderError as err:
            self._ai_error = str(err)
            _LOGGER.warning("DeepSeek недоступен (%s): %s", err.kind, err)
            return None
        try:
            payload = parse_payload(raw)
        except SmartAnalystError as err:
            self._ai_error = f"ответ модели не разобран: {err}"
            return None
        result = validate_response(payload, package)
        if not result.ok or result.response is None:
            self._ai_error = f"ответ не прошёл проверку: {result.error_text}"
            return None
        self._ai_error = None
        self._tokens_used += max(1, len(raw) // 4)
        return result.response

    def _ai_config(self) -> ProviderConfig | None:
        """Настройки ИИ: Options Flow важнее исходных данных записи."""
        merged = {**dict(self.entry.data), **dict(self.entry.options)}
        api_key = str(merged.get("ai_api_key") or "").strip()
        if not api_key:
            return None
        settings = self.config_state.settings
        return ProviderConfig(
            api_key=api_key,
            base_url=str(merged.get("ai_base_url") or DEFAULT_AI_BASE_URL),
            model=str(
                merged.get("ai_model") or settings.get("ai_model") or DEFAULT_AI_MODEL
            ),
        )

    def _provider(self) -> DeepSeekProvider | None:
        """Провайдер пересоздаётся, когда меняются ключ, URL или модель."""
        config = self._ai_config()
        if config is None:
            self._ai_provider = None
            self._ai_signature = None
            return None
        signature = (config.api_key, config.base_url, config.model)
        if self._ai_provider is None or self._ai_signature != signature:
            self._ai_provider = DeepSeekProvider(
                async_get_clientsession(self.hass), config
            )
            self._ai_signature = signature
        return self._ai_provider

    def _profile(self, profile_id: str | None) -> ReportProfile:
        profiles = [ReportProfile.from_dict(item) for item in self.config_state.report_profiles]
        if profile_id:
            for profile in profiles:
                if profile.id == profile_id:
                    return profile
            raise SmartAnalystError(f"профиль отчёта {profile_id!r} не найден")
        if profiles:
            return profiles[0]
        return ReportProfile(
            id="default",
            name="Отчёт HA Smart Analyst",
            locale=Locale(self.config_state.settings.get("locale", "ru")),
            timezone=str(self.hass.config.time_zone or "UTC"),
            report_type=ReportType.ON_DEMAND,
        )

    async def _async_build_sections(
        self,
        profile: ReportProfile,
        observations: dict[str, Observation],
        window: Window,
    ) -> tuple[tuple[SystemSection, ...], str | None]:
        """Собрать разделы отчёта с историей, уставками и привязкой к оборудованию.

        Возвращает разделы и пометку, если история не успела загрузиться.
        """
        sources = self._history_sources()
        history: CachedHistory | None = None
        history_note: str | None = None
        timeout = self._history_timeout()
        if sources:
            try:
                async with asyncio.timeout(timeout):
                    history = await CachedHistory.async_load(
                        self.hass, window, sources, timeout
                    )
            except TimeoutError:
                _LOGGER.warning(
                    "История для отчёта не загружена за %s с — показания без истории",
                    timeout,
                )
                history_note = (
                    f"история не загружена за {timeout:g} с — отчёт по текущим показаниям"
                    if profile.locale is Locale.RU
                    else f"history was not loaded within {timeout:g}s; "
                    "the report uses current readings"
                )
            except Exception as err:  # noqa: BLE001 - без истории отчёт остаётся честным
                _LOGGER.debug("История для отчёта недоступна: %s", err)

        required = set(profile.required_measurement_ids)
        present: set[str] = set()

        def visible(measurement: Measurement) -> bool:
            return measurement.report_visible or measurement.id in required

        def included_system(system: System) -> bool:
            if not profile.system_ids or system.id in profile.system_ids:
                return True
            return any(
                measurement.id in required
                for measurement in self.model.measurements_of(system.id)
            )

        groups: list[tuple[str, str, list[Measurement], str]] = []
        for system in self.model.systems.values():
            if not included_system(system):
                continue
            items = [
                measurement
                for measurement in self.model.measurements_of(system.id)
                if measurement.id in observations and visible(measurement)
            ]
            if not items:
                continue
            groups.append((system.id, system.name, items, system.preset.value))
            present.update(measurement.id for measurement in items)

        if not profile.group_by_system:
            merged = [measurement for _, _, items, _ in groups for measurement in items]
            site_name = (
                self.model.site.name if self.model.site is not None else "Все показатели"
            )
            groups = [("all", site_name, merged, "")] if merged else []

        missing = [
            measurement_id
            for measurement_id in profile.required_measurement_ids
            if measurement_id not in present
        ]

        sections: list[SystemSection] = []
        for system_id, system_name, items, preset in groups:
            sections.append(
                self._build_section(
                    profile,
                    system_id,
                    system_name,
                    items,
                    observations,
                    history,
                    window,
                    required,
                    preset,
                )
            )
        if missing:
            missing_facts = tuple(
                self._missing_measurement_fact(measurement_id, required)
                for measurement_id in missing
            )
            sections.append(
                SystemSection(
                    id="profile",
                    name="Показатели профиля",
                    measurements=missing_facts,
                )
            )
        return tuple(sections), history_note

    def _build_section(
        self,
        profile: ReportProfile,
        section_id: str,
        section_name: str,
        items: list[Measurement],
        observations: dict[str, Observation],
        history: CachedHistory | None,
        window: Window,
        required: set[str],
        system_preset: str,
    ) -> SystemSection:
        facts: list[Fact] = list(pair_facts(items, observations))
        coverages: list[float] = []
        trend_text: str | None = None
        coverage_by_measurement: dict[str, float] = {}
        for measurement in items:
            if measurement.source is None:
                continue
            observation = observations[measurement.id]
            if observation.value_type is not ValueType.NUMBER:
                continue
            samples = history.samples(measurement.id, window) if history is not None else ()
            unit = measurement.unit or observations[measurement.id].unit
            if history is not None:
                counter = measurement.role is MeasurementRole.ENERGY
                facts.extend(
                    history_facts(
                        measurement.id,
                        measurement.name,
                        samples,
                        window,
                        unit,
                        counter=counter,
                    )
                )
                coverage, trend = history_trend(samples, window, unit, profile.locale)
                if coverage is not None:
                    coverages.append(coverage)
                    coverage_by_measurement[measurement.id] = coverage
                if trend and trend_text is None:
                    trend_text = trend
        measurement_facts = [
            MeasurementFact.from_observation(
                measurement.id,
                measurement.name,
                measurement.role,
                observations[measurement.id],
                required=measurement.id in required,
                coverage=coverage_by_measurement.get(measurement.id),
                equipment_name=(
                    self.model.equipment[measurement.equipment_id].name
                    if measurement.equipment_id
                    and measurement.equipment_id in self.model.equipment
                    else None
                ),
                system_preset=system_preset or None,
            )
            for measurement in items
        ]
        measurement_facts.sort(key=lambda fact: measurement_rank(profile, fact))
        return SystemSection(
            id=section_id,
            name=section_name,
            measurements=tuple(measurement_facts),
            facts=tuple(facts),
            coverage=system_coverage(coverages),
            trend=trend_text,
        )

    def _missing_measurement_fact(
        self, measurement_id: str, required: set[str]
    ) -> MeasurementFact:
        measurement = self.model.measurement(measurement_id)
        name = measurement.name if measurement is not None else measurement_id
        return MeasurementFact(
            id=measurement_id,
            name=name,
            role=measurement.role if measurement is not None else MeasurementRole.CUSTOM,
            value=None,
            unit=measurement.unit if measurement is not None else None,
            quality=Quality.UNCONFIGURED,
            required=measurement_id in required,
        )

    # -- диагностика --------------------------------------------------
    def _publish_snapshot(self, now: datetime, observations: dict[str, Observation]) -> None:
        active = self.incidents.active()
        snapshot = AnalystSnapshot(
            generated_at=now,
            rules_total=len(self.rules),
            rules_active=sum(1 for state in self.runtimes.values() if state.state.value == "active"),
            active_incidents=len(active),
            critical_incidents=sum(1 for item in active if item.severity is Severity.CRITICAL),
            queue_pending=len(self.queue.pending(now)),
            stale_sources=sum(
                1 for observation in observations.values() if observation.quality is Quality.STALE
            ),
            ai_available=self._ai_error is None,
            ai_error=self._ai_error,
            last_report_at=self._last_report_at,
            recorder_available=self._history is not None,
            tokens_used=self._tokens_used,
            measurements_total=len(self.model.measurements),
        )
        self.async_set_updated_data(snapshot)

    async def _async_update_data(self) -> AnalystSnapshot:
        if self.data is not None:
            return self.data
        await self._async_evaluate()
        return self.data or AnalystSnapshot(generated_at=dt_util.utcnow())

    # -- сохранение ---------------------------------------------------
    def _schedule_save(self) -> None:
        self._store.async_delay_save(self._serialize, SAVE_DELAY_SECONDS)

    def _serialize(self) -> dict[str, Any]:
        self.config_state.rules = [rule.to_dict() for rule in self.rules.values()]
        self.config_state.runtime = [runtime.to_dict() for runtime in self.runtimes.values()]
        self.config_state.incidents = self.incidents.to_list()
        self.config_state.notifications = self.queue.to_list()
        self.config_state.schema_version = STORAGE_VERSION
        return self.config_state.to_dict()

    def export_configuration(self) -> str:
        return export_configuration(self.config_state, dt_util.utcnow())


def _history_seconds(rule: Rule) -> list[int]:
    seconds: list[int] = []

    def walk(node: Any) -> None:
        if hasattr(node, "children"):
            for child in node.children:
                walk(child)
        elif hasattr(node, "child"):
            walk(node.child)
        window = getattr(node, "window_seconds", None)
        if isinstance(window, int):
            seconds.append(window)

    walk(rule.expression)
    return seconds


def _max_severity(digests: tuple[Any, ...]) -> Severity:
    order = {Severity.INFO: 0, Severity.WARNING: 1, Severity.CRITICAL: 2}
    result = Severity.INFO
    for item in digests:
        if order[item.severity] > order[result]:
            result = item.severity
    return result


def _incident_text(rule: Rule, incident: Incident, event: RuleEvent, *, resolved: bool) -> str:
    lines = [("✅ " if resolved else "⚠️ ") + rule.name]
    if resolved:
        if incident.duration_seconds is not None:
            lines.append(f"Длительность: {int(incident.duration_seconds)} с")
        if incident.peak_deviation is not None:
            lines.append(f"Пиковое отклонение: {incident.peak_deviation}")
    else:
        lines.append(f"Критичность: {incident.severity.value}")
        if incident.peak_deviation is not None:
            lines.append(f"Отклонение: {incident.peak_deviation} {incident.peak_unit or ''}".strip())
    for reason in event.reasons[:3]:
        lines.append(f"• {reason.message}")
    lines.append(f"Инцидент: {incident.incident_id}")
    return "\n".join(lines)
