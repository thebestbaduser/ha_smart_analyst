"""Провайдер DeepSeek: сетевые вызовы, таймауты и классификация ошибок (AI-01, NFR-03, NFR-04).

Токен и endpoint приходят из Config Entry; в текстовых экспортах секрета нет.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import aiohttp

from .const import DEFAULT_AI_BASE_URL, DEFAULT_AI_MODEL
from .core.ai import SYSTEM_PROMPT, AnalysisPackage, RetryAction, classify_status

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = 45
CONNECT_TIMEOUT = 10


class ProviderError(Exception):
    """Ошибка провайдера с решением о повторе."""

    def __init__(self, message: str, status: int | None = None, action: RetryAction = RetryAction.FALLBACK) -> None:
        super().__init__(message)
        self.status = status
        self.action = action


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """Настройки провайдера без записи в репозиторий."""

    api_key: str
    base_url: str = DEFAULT_AI_BASE_URL
    model: str = DEFAULT_AI_MODEL

    @property
    def chat_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"

    @property
    def models_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/models"


class DeepSeekProvider:
    """Минимальный клиент DeepSeek с JSON-режимом."""

    def __init__(self, session: aiohttp.ClientSession, config: ProviderConfig) -> None:
        self._session = session
        self._config = config

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._config.api_key}",
            "Content-Type": "application/json",
        }

    async def async_test_connection(self) -> None:
        """Проверить ключ и доступность API понятной ошибкой (CFG-03)."""
        try:
            async with self._session.get(
                self._config.models_url,
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=CONNECT_TIMEOUT),
            ) as response:
                if response.status != 200:
                    body = await response.text()
                    raise ProviderError(
                        f"HTTP {response.status}: {body[:200]}",
                        response.status,
                        classify_status(response.status),
                    )
        except aiohttp.ClientError as err:
            raise ProviderError(f"сеть недоступна: {err}") from err

    async def async_analyze(self, package: AnalysisPackage) -> str:
        """Отправить пакет и вернуть сырой текст ответа (валидация — в ядре)."""
        payload: dict[str, Any] = {
            "model": self._config.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": package.to_json()},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
        }
        try:
            async with self._session.post(
                self._config.chat_url,
                headers=self._headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            ) as response:
                body = await response.text()
                if response.status != 200:
                    raise ProviderError(
                        f"HTTP {response.status}: {body[:300]}",
                        response.status,
                        classify_status(response.status),
                    )
        except TimeoutError as err:
            raise ProviderError("таймаут ответа модели", action=RetryAction.RETRY) from err
        except aiohttp.ClientError as err:
            raise ProviderError(f"сеть недоступна: {err}", action=RetryAction.RETRY) from err
        try:
            data = await _json(body)
        except ValueError as err:
            raise ProviderError(f"провайдер вернул не JSON: {err}") from err
        choices = data.get("choices") or []
        if not choices:
            raise ProviderError("провайдер вернул пустой ответ")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ProviderError("провайдер вернул пустое содержимое")
        return content


async def _json(text: str) -> dict[str, Any]:
    import json

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as err:
        raise ValueError(err.msg) from err
    if not isinstance(payload, dict):
        raise ValueError("ожидался объект JSON")
    return payload
