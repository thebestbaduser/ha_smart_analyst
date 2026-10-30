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
if str(COMPONENT_DIR) not in sys.path:
    sys.path.insert(0, str(COMPONENT_DIR))

try:
    import pytest_homeassistant_custom_component  # noqa: F401
except ImportError:  # быстрый контур без HA
    pytest_plugins: list[str] = []
    collect_ignore = ["ha"]
else:
    pytest_plugins = ["pytest_homeassistant_custom_component"]


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
