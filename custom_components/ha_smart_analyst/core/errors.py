"""Исключения предметного ядра.

Ядро не зависит от Home Assistant: его можно запускать и тестировать как
обычный Python-пакет. Все ошибки конфигурации и данных наследуются от
``SmartAnalystError``, чтобы слой интеграции мог преобразовать их в
``vol.Invalid``/сообщения UI, не разбирая текст исключения.
"""

from __future__ import annotations


class SmartAnalystError(Exception):
    """Базовая ошибка интеграции."""


class SchemaError(SmartAnalystError):
    """Некорректная структура правила, профиля или пакета данных."""

    def __init__(self, message: str, path: str = "") -> None:
        self.path = path
        super().__init__(f"{path}: {message}" if path else message)


class UnitError(SmartAnalystError):
    """Несовместимые или неизвестные единицы измерения."""


class InvalidValueError(SmartAnalystError):
    """Значение не является допустимым числом (NaN, inf, строка и т. п.)."""


class ModelError(SmartAnalystError):
    """Нарушение семантической модели дома."""


class HistoryError(SmartAnalystError):
    """История недоступна или недостаточна для запрошенной операции."""


class StorageError(SmartAnalystError):
    """Повреждённое или несовместимое хранилище конфигурации."""
