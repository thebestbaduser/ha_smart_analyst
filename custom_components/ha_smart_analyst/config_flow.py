"""Config Flow и Options Flow (CFG-02…CFG-05, UI-01).

Мастер не требует YAML и допускает установку без ключа ИИ: в этом режиме
работают локальные правила и шаблонные отчёты.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_AI_API_KEY,
    CONF_AI_BASE_URL,
    CONF_AI_DAILY_TOKEN_BUDGET,
    CONF_AI_ENABLED,
    CONF_AI_MODEL,
    CONF_AI_PROVIDER,
    CONF_HISTORY_TIMEOUT,
    CONF_INCIDENT_RETENTION_DAYS,
    CONF_INSTANCE_NAME,
    CONF_LANGUAGE,
    CONF_QUIET_END,
    CONF_QUIET_START,
    CONF_TELEGRAM_RECIPIENTS,
    CONF_TELEGRAM_SERVICE,
    CONF_TIMEZONE,
    DEFAULT_AI_BASE_URL,
    DEFAULT_AI_MODEL,
    DEFAULT_AI_PROVIDER,
    DEFAULT_HISTORY_TIMEOUT,
    DEFAULT_INCIDENT_RETENTION_DAYS,
    DEFAULT_LANGUAGE,
    DEFAULT_QUIET_END,
    DEFAULT_QUIET_START,
    DOMAIN,
)
from .provider import DeepSeekProvider, ProviderConfig, ProviderError

_LOGGER = logging.getLogger(__name__)


def _text(multiline: bool = False) -> TextSelector:
    return TextSelector(TextSelectorConfig(multiline=multiline, type=TextSelectorType.TEXT))


class SmartAnalystConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Мастер первого подключения: экземпляр → ИИ → Telegram → язык."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            name = str(user_input.get(CONF_INSTANCE_NAME, "")).strip()
            if not name:
                errors["base"] = "name_required"
            else:
                self._data.update(user_input)
                self._data[CONF_INSTANCE_NAME] = name
                return await self.async_step_ai()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_INSTANCE_NAME, default="Дом"): _text(),
                    vol.Optional(CONF_TIMEZONE): _text(),
                    vol.Optional(CONF_LANGUAGE, default=DEFAULT_LANGUAGE): SelectSelector(
                        SelectSelectorConfig(
                            options=["ru", "en"], mode=SelectSelectorMode.DROPDOWN
                        )
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_ai(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            enabled = bool(user_input.get(CONF_AI_ENABLED, True))
            self._data.update(user_input)
            if enabled:
                api_key = str(user_input.get(CONF_AI_API_KEY, "")).strip()
                if not api_key:
                    errors["base"] = "api_key_required"
                else:
                    error = await self._async_test_provider(
                        api_key,
                        str(user_input.get(CONF_AI_BASE_URL, DEFAULT_AI_BASE_URL)),
                        str(user_input.get(CONF_AI_MODEL, DEFAULT_AI_MODEL)),
                    )
                    if error:
                        errors["base"] = error
            if not errors:
                return await self.async_step_telegram()
        return self.async_show_form(
            step_id="ai",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_AI_ENABLED, default=True): BooleanSelector(),
                    vol.Optional(CONF_AI_PROVIDER, default=DEFAULT_AI_PROVIDER): SelectSelector(
                        SelectSelectorConfig(options=["deepseek"], mode=SelectSelectorMode.DROPDOWN)
                    ),
                    vol.Optional(CONF_AI_API_KEY): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                    vol.Optional(CONF_AI_BASE_URL, default=DEFAULT_AI_BASE_URL): _text(),
                    vol.Optional(CONF_AI_MODEL, default=DEFAULT_AI_MODEL): _text(),
                    vol.Optional(CONF_AI_DAILY_TOKEN_BUDGET, default=0): NumberSelector(
                        NumberSelectorConfig(min=0, max=10_000_000, step=1000, mode="box")
                    ),
                }
            ),
            errors=errors,
            description_placeholders={"docs": "https://api-docs.deepseek.com"},
        )

    async def _async_test_provider(self, api_key: str, base_url: str, model: str) -> str | None:
        provider = DeepSeekProvider(
            async_get_clientsession(self.hass),
            ProviderConfig(api_key=api_key, base_url=base_url, model=model),
        )
        try:
            await provider.async_test_connection()
        except ProviderError as err:
            _LOGGER.warning("Проверка DeepSeek не прошла: %s", err)
            if err.status in {401, 403}:
                return "invalid_auth"
            if err.status == 402:
                return "insufficient_balance"
            return "cannot_connect"
        return None

    async def async_step_telegram(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            recipients = [
                item.strip()
                for item in str(user_input.get(CONF_TELEGRAM_RECIPIENTS, "")).split(",")
                if item.strip()
            ]
            self._data[CONF_TELEGRAM_RECIPIENTS] = recipients
            self._data[CONF_TELEGRAM_SERVICE] = str(
                user_input.get(CONF_TELEGRAM_SERVICE, "")
            ).strip() or None
            return self.async_create_entry(title=self._data[CONF_INSTANCE_NAME], data=self._data)
        return self.async_show_form(
            step_id="telegram",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_TELEGRAM_SERVICE): EntitySelector(
                        EntitySelectorConfig(domain="notify")
                    ),
                    vol.Optional(CONF_TELEGRAM_RECIPIENTS): _text(),
                }
            ),
            description_placeholders={
                "notify_hint": "Настройки → Устройства и службы → Сущности → фильтр notify."
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> config_entries.OptionsFlow:
        return SmartAnalystOptionsFlow()


class SmartAnalystOptionsFlow(config_entries.OptionsFlow):
    """Базовые параметры после установки (UI-01)."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            recipients = [
                item.strip()
                for item in str(user_input.get(CONF_TELEGRAM_RECIPIENTS, "")).split(",")
                if item.strip()
            ]
            data = dict(user_input)
            data[CONF_TELEGRAM_RECIPIENTS] = recipients
            data[CONF_TELEGRAM_SERVICE] = str(data.get(CONF_TELEGRAM_SERVICE, "")).strip() or None
            return self.async_create_entry(title="", data=data)

        current = dict(self.config_entry.options) or dict(self.config_entry.data)
        current_service = str(current.get(CONF_TELEGRAM_SERVICE) or "")
        service_field: Any = vol.Optional(CONF_TELEGRAM_SERVICE)
        if current_service.startswith("notify.") and self.hass.states.get(current_service):
            service_field = vol.Optional(
                CONF_TELEGRAM_SERVICE, description={"suggested_value": current_service}
            )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_AI_ENABLED, default=bool(current.get(CONF_AI_ENABLED, True))
                    ): BooleanSelector(),
                    vol.Optional(CONF_AI_MODEL, default=current.get(CONF_AI_MODEL, DEFAULT_AI_MODEL)): _text(),
                    service_field: EntitySelector(EntitySelectorConfig(domain="notify")),
                    vol.Optional(
                        CONF_TELEGRAM_RECIPIENTS,
                        default=", ".join(current.get(CONF_TELEGRAM_RECIPIENTS) or []),
                    ): _text(),
                    vol.Optional(
                        CONF_QUIET_START, default=current.get(CONF_QUIET_START, DEFAULT_QUIET_START)
                    ): _text(),
                    vol.Optional(
                        CONF_QUIET_END, default=current.get(CONF_QUIET_END, DEFAULT_QUIET_END)
                    ): _text(),
                    vol.Optional(
                        CONF_INCIDENT_RETENTION_DAYS,
                        default=int(current.get(CONF_INCIDENT_RETENTION_DAYS, DEFAULT_INCIDENT_RETENTION_DAYS)),
                    ): NumberSelector(NumberSelectorConfig(min=30, max=365, step=30, mode="box")),
                    vol.Optional(
                        CONF_HISTORY_TIMEOUT,
                        default=int(current.get(CONF_HISTORY_TIMEOUT, DEFAULT_HISTORY_TIMEOUT)),
                    ): NumberSelector(NumberSelectorConfig(min=1, max=60, step=1, mode="box")),
                }
            ),
        )
