"""Хранение конфигурации: версии, миграции и экспорт без секретов (STO-01…STO-04, CFG-06).

Версионированный слой: перед миграцией сохраняется резервная копия старого
содержимого, повреждённый JSON не «исправляется» догадками, а отклоняется с
понятной ошибкой.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .errors import SchemaError, StorageError
from .incidents import IncidentManager
from .model import HomeModel
from .observations import ensure_utc
from .reports import ReportProfile

SCHEMA_VERSION = 1
_SECRET_KEYS = frozenset({"api_key", "token", "password", "secret", "bot_token"})
DEFAULT_SETTINGS: dict[str, Any] = {
    "locale": "ru",
    "timezone": "UTC",
    "telegram_recipients": [],
    "quiet_hours": None,
    "incident_retention_days": 90,
    "notification_retention_days": 7,
    "ai_enabled": True,
    "ai_model": "deepseek-flash",
    "ai_base_url": "https://api.deepseek.com",
}


@dataclass
class ConfigState:
    """Полный снимок конфигурации интеграции."""

    model: HomeModel = field(default_factory=HomeModel)
    rules: list[dict[str, Any]] = field(default_factory=list)
    report_profiles: list[dict[str, Any]] = field(default_factory=list)
    runtime: list[dict[str, Any]] = field(default_factory=list)
    incidents: list[dict[str, Any]] = field(default_factory=list)
    notifications: list[dict[str, Any]] = field(default_factory=list)
    settings: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_SETTINGS))
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "model": self.model.to_dict(),
            "rules": list(self.rules),
            "report_profiles": list(self.report_profiles),
            "runtime": list(self.runtime),
            "incidents": list(self.incidents),
            "notifications": list(self.notifications),
            "settings": dict(self.settings),
        }


def _require_mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SchemaError("ожидался объект", path)
    return value


def _migrate_v0(payload: dict[str, Any]) -> dict[str, Any]:
    """Формат без ``schema_version``: добавить версию и настройки по умолчанию."""
    migrated = dict(payload)
    settings = dict(DEFAULT_SETTINGS)
    settings.update(_require_mapping(migrated.get("settings", {}), "settings"))
    migrated["settings"] = settings
    migrated["schema_version"] = 1
    migrated.setdefault("rules", [])
    migrated.setdefault("report_profiles", [])
    migrated.setdefault("runtime", [])
    migrated.setdefault("incidents", [])
    migrated.setdefault("notifications", [])
    migrated.setdefault("model", HomeModel().to_dict())
    return migrated


_MIGRATIONS = {0: _migrate_v0}


def migrate(payload: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Привести payload к текущей версии; вернуть (новый payload, резервная копия)."""
    if not isinstance(payload, Mapping):
        raise StorageError("конфигурация должна быть объектом JSON")
    current = dict(payload)
    version = int(current.get("schema_version", 0))
    if version > SCHEMA_VERSION:
        raise StorageError(
            f"конфигурация версии {version} новее поддерживаемой {SCHEMA_VERSION}; "
            "обновите интеграцию"
        )
    backup: dict[str, Any] | None = None
    while version < SCHEMA_VERSION:
        migration = _MIGRATIONS.get(version)
        if migration is None:
            raise StorageError(f"нет миграции с версии {version}")
        if backup is None:
            backup = json.loads(json.dumps(payload, ensure_ascii=False))
        current = migration(current)
        version = int(current["schema_version"])
    return current, backup


def validate_state(payload: Mapping[str, Any]) -> ConfigState:
    """Строгая загрузка: неизвестные версии и битые поля отклоняются (STO-02)."""
    data, _ = migrate(payload)
    state = ConfigState()
    state.schema_version = SCHEMA_VERSION
    model_payload = _require_mapping(data.get("model", {}), "model")
    state.model = HomeModel.from_dict(dict(model_payload))

    rules = data.get("rules", [])
    if not isinstance(rules, list) or not all(isinstance(item, dict) for item in rules):
        raise SchemaError("rules должен быть списком объектов", "rules")
    state.rules = [dict(item) for item in rules]

    profiles = data.get("report_profiles", [])
    if not isinstance(profiles, list) or not all(isinstance(item, dict) for item in profiles):
        raise SchemaError("report_profiles должен быть списком объектов", "report_profiles")
    state.report_profiles = [dict(item) for item in profiles]
    for profile in state.report_profiles:
        ReportProfile.from_dict(profile)

    for key in ("runtime", "incidents", "notifications"):
        items = data.get(key, [])
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise SchemaError(f"{key} должен быть списком объектов", key)
        setattr(state, key, [dict(item) for item in items])

    settings = dict(DEFAULT_SETTINGS)
    settings.update(_require_mapping(data.get("settings", {}), "settings"))
    state.settings = settings
    return state


def load_config(raw: str | bytes, now: datetime) -> tuple[ConfigState, dict[str, Any] | None]:
    """Разобрать сохранённый JSON; вернуть состояние и резервную копию при миграции."""
    if isinstance(raw, bytes):
        try:
            raw_text = raw.decode("utf-8")
        except UnicodeDecodeError as err:
            raise StorageError("конфигурация не является UTF-8") from err
    else:
        raw_text = raw
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as err:
        raise StorageError(f"повреждённый JSON конфигурации: {err.msg} (позиция {err.pos})") from err
    if not isinstance(payload, dict):
        raise StorageError("конфигурация должна быть объектом JSON")
    migrated, backup = migrate(payload)
    return validate_state(migrated), backup


def strip_secrets(value: Any) -> Any:
    """Рекурсивно убрать секреты из экспорта (CFG-04, STO-01).

    Отсекаются ключи, оканчивающиеся на известный секретный маркер:
    ``ai_api_key``, ``bot_token``, ``password`` и т. п. Полезные поля вроде
    ``ai_daily_token_budget`` остаются.
    """

    def is_secret(key: str) -> bool:
        lowered = key.lower()
        return any(lowered == token or lowered.endswith(f"_{token}") for token in _SECRET_KEYS)

    if isinstance(value, Mapping):
        return {key: strip_secrets(item) for key, item in value.items() if not is_secret(key)}
    if isinstance(value, list):
        return [strip_secrets(item) for item in value]
    return value


def export_configuration(state: ConfigState, now: datetime) -> str:
    """Экспорт настроек без секретов в JSON (CFG-04)."""
    payload = state.to_dict()
    payload["exported_at"] = ensure_utc(now, now).isoformat()
    return json.dumps(strip_secrets(payload), ensure_ascii=False, indent=2, sort_keys=True)


def import_configuration(raw: str) -> ConfigState:
    """Импорт настроек с валидацией; секреты не принимаются."""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as err:
        raise StorageError(f"повреждённый JSON: {err.msg}") from err
    if not isinstance(payload, dict):
        raise StorageError("экспорт должен быть объектом JSON")
    payload.pop("exported_at", None)
    return validate_state(payload)


def degraded_incident_state(state: ConfigState, now: datetime) -> IncidentManager:
    """Восстановить журнал инцидентов из конфигурации (INC-05)."""
    return IncidentManager.from_list(state.incidents, now)


def atomic_write(path: Path, content: str) -> None:
    """Атомарная запись файла экспорта (STO-02)."""
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)
