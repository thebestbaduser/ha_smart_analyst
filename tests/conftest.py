"""Корневое окружение тестов.

``core`` импортируется как самостоятельный пакет, поэтому тесты предметной
логики не требуют установленного Home Assistant. Если же установлен
pytest-homeassistant-custom-component, подключается его плагин и собираются
интеграционные тесты ``tests/ha``.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

COMPONENT_DIR = Path(__file__).parents[1] / "custom_components" / "ha_smart_analyst"
PROJECT_ROOT = Path(__file__).parents[1]
for path in (PROJECT_ROOT, COMPONENT_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

try:
    import pytest_homeassistant_custom_component  # noqa: F401
except ImportError:  # быстрый контур без HA
    pytest_plugins: list[str] = []
    collect_ignore = ["ha"]
else:
    pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture(scope="session", autouse=True)
def _force_threaded_resolver():
    """На Windows aiodns/pycares оставляет служебный поток и требует Selector loop.

    Тестовому WebSocket-клиенту не нужен DNS: подменяем резолвер на потоковый,
    чтобы не тащить pycares в тестовый контур.
    """
    if sys.platform == "win32":
        try:
            import aiohttp.connector
            import aiohttp.resolver
        except ImportError:  # быстрый контур без aiohttp
            pass
        else:
            aiohttp.connector.DefaultResolver = aiohttp.resolver.ThreadedResolver
            aiohttp.resolver.DefaultResolver = aiohttp.resolver.ThreadedResolver
    yield


@pytest.fixture
def event_loop():
    """Создать event loop при разрешённых сокетах (ограничение Windows).

    pytest-homeassistant-custom-component блокирует сокеты перед каждым тестом,
    а ProactorEventLoop на Windows создаёт self-pipe через socketpair.
    """
    if sys.platform == "win32":
        try:
            import pytest_socket

            pytest_socket.enable_socket()
        except ImportError:  # pragma: no cover - плагин есть в requirements_test
            pass
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield loop
    loop.close()
    asyncio.set_event_loop(None)
