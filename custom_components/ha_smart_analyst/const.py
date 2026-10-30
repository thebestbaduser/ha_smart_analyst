"""Константы интеграции HA Smart Analyst."""

from __future__ import annotations

from typing import Final

from homeassistant.const import Platform

DOMAIN: Final = "ha_smart_analyst"
NAME: Final = "HA Smart Analyst"
VERSION: Final = "0.3.3"

STORAGE_VERSION: Final = 1
STORAGE_KEY: Final = DOMAIN
SAVE_DELAY_SECONDS: Final = 5
SCAN_INTERVAL: Final = 30
ASSESS_INTERVAL_SECONDS: Final = 15

PLATFORMS: Final[tuple[Platform, ...]] = (
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
)

CONF_INSTANCE_NAME: Final = "instance_name"
CONF_LANGUAGE: Final = "language"
CONF_TIMEZONE: Final = "timezone"
CONF_AI_ENABLED: Final = "ai_enabled"
CONF_AI_PROVIDER: Final = "ai_provider"
CONF_AI_BASE_URL: Final = "ai_base_url"
CONF_AI_API_KEY: Final = "ai_api_key"
CONF_AI_MODEL: Final = "ai_model"
CONF_AI_DAILY_TOKEN_BUDGET: Final = "ai_daily_token_budget"
CONF_TELEGRAM_SERVICE: Final = "telegram_service"
CONF_TELEGRAM_RECIPIENTS: Final = "telegram_recipients"
CONF_QUIET_START: Final = "quiet_start"
CONF_QUIET_END: Final = "quiet_end"
CONF_INCIDENT_RETENTION_DAYS: Final = "incident_retention_days"
CONF_HISTORY_TIMEOUT: Final = "history_timeout_seconds"
CONF_REPORT_PROFILES: Final = "report_profiles"

DEFAULT_AI_PROVIDER: Final = "deepseek"
DEFAULT_AI_BASE_URL: Final = "https://api.deepseek.com"
DEFAULT_AI_MODEL: Final = "deepseek-flash"
DEFAULT_LANGUAGE: Final = "ru"
DEFAULT_INCIDENT_RETENTION_DAYS: Final = 90
DEFAULT_HISTORY_TIMEOUT: Final = 5
DEFAULT_QUIET_START: Final = "22:00:00"
DEFAULT_QUIET_END: Final = "07:00:00"

PANEL_URL: Final = "ha-smart-analyst"
PANEL_MODULE: Final = "/ha_smart_analyst_static/ha-smart-analyst-panel.js"
PANEL_ICON: Final = "mdi:home-analytics"

SERVICE_GENERATE_REPORT: Final = "generate_report"
SERVICE_TEST_NOTIFICATION: Final = "test_notification"
SERVICE_RUN_RULE_TEST: Final = "run_rule_test"
SERVICE_ACKNOWLEDGE_INCIDENT: Final = "acknowledge_incident"
SERVICE_RESOLVE_INCIDENT: Final = "resolve_incident_manually"
SERVICE_REFRESH_ANALYSIS: Final = "refresh_analysis"
SERVICE_EXPORT_CONFIGURATION: Final = "export_configuration"

EVENT_INCIDENT_OPENED: Final = f"{DOMAIN}_incident_opened"
EVENT_INCIDENT_RESOLVED: Final = f"{DOMAIN}_incident_resolved"
EVENT_DATA_QUALITY: Final = f"{DOMAIN}_data_quality"
