"""Регистрация панели Home Assistant (UI-01, UI-02).

Панель — необязательная часть: если frontend или panel_custom недоступны
(headless-контейнер, тестовое окружение), интеграция продолжает работать без
панели, а в журнале остаётся предупреждение. Поэтому эти компоненты не
объявлены жёсткими зависимостями.
"""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant

from .const import DOMAIN, PANEL_ICON, PANEL_MODULE, PANEL_URL

_LOGGER = logging.getLogger(__name__)

STATIC_URL = "/ha_smart_analyst_static"
FRONTEND_DIR = Path(__file__).parent / "frontend"


async def async_register_panel(hass: HomeAssistant) -> bool:
    """Зарегистрировать статический путь и панель, если frontend доступен."""
    if hass.data.get(f"{DOMAIN}_panel"):
        return True
    try:
        from homeassistant.components import panel_custom

        await hass.http.async_register_static_paths(
            [StaticPathConfig(STATIC_URL, str(FRONTEND_DIR), False)]
        )
        await panel_custom.async_register_panel(
            hass,
            webcomponent_name="ha-smart-analyst-panel",
            frontend_url_path=PANEL_URL,
            module_url=PANEL_MODULE,
            sidebar_title="Smart Analyst",
            sidebar_icon=PANEL_ICON,
            require_admin=True,
            config={},
            embed_iframe=False,
        )
    except Exception as err:  # noqa: BLE001 - панель не должна ломать интеграцию
        _LOGGER.warning("Панель HA Smart Analyst недоступна: %s", err)
        return False
    hass.data[f"{DOMAIN}_panel"] = True
    return True
