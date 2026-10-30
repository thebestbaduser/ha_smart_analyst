"""Окружение интеграционных тестов Home Assistant.

На Windows ProactorEventLoop создаёт self-pipe через ``socket.socketpair()``,
а pytest-socket из pytest-homeassistant-custom-component блокирует любые
сокеты. Поэтому на Windows защита сокетов для этих тестов снимается: тесты
HA и так работают офлайн, а без этого event loop не поднимается (ограничение
платформы, не интеграции).
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Разрешить загрузку custom_components в тестовом HA."""
    yield
