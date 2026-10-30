"""Сквозной сценарий отчёта: WS → координатор → mock Telegram → frontend-контракт."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.ha_smart_analyst import history_provider as history_module
from custom_components.ha_smart_analyst.const import (
    CONF_TELEGRAM_RECIPIENTS,
    CONF_TELEGRAM_SERVICE,
    DOMAIN,
)
from custom_components.ha_smart_analyst.core.history import Window
from custom_components.ha_smart_analyst.history_provider import (
    CachedHistory,
    HistorySource,
)

NOTIFY_SERVICE = "telegram_bot_mock"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_CHECK = PROJECT_ROOT / "tests" / "frontend" / "check_response.mjs"
AI_REPLY = json.dumps(
    {
        "summary": "Котёл работает в штатном режиме.",
        "attention_level": "info",
        "observations": [],
        "hypotheses": [],
        "recommendations": [],
        "uncertainties": [],
        "evidence_refs": [],
    },
    ensure_ascii=False,
)


async def _setup(hass: HomeAssistant, entry):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return hass.data[DOMAIN][entry.entry_id]


async def _send(client, payload: dict) -> dict:
    await client.send_json(payload)
    return await client.receive_json()


async def test_history_timeout_is_explicit_in_report(hass, entry, monkeypatch) -> None:
    hass.states.async_set("sensor.temp", "57.8", {"unit_of_measurement": "°C"})
    coordinator = await _setup(hass, entry)
    system = await coordinator.async_create_system("Отопление", "heating")
    await coordinator.async_add_measurement(
        system_id=system.id, entity_id="sensor.temp", role="actual", unit="°C"
    )
    coordinator.config_state.settings["history_timeout_seconds"] = 0.5

    async def slow_load(cls, hass, window, sources, timeout_seconds=None):
        await asyncio.sleep(5)

    monkeypatch.setattr(CachedHistory, "async_load", classmethod(slow_load))

    rendered = await coordinator.async_generate_report(use_ai=False)

    assert "57,8" in rendered.text
    assert "история не загружена за 0.5 с" in rendered.text
    assert "отчёт по текущим показаниям" in rendered.text


async def test_cached_history_respects_timeout(hass, monkeypatch) -> None:
    class SlowRecorder:
        async def async_wait_recorder(self) -> None:
            await asyncio.sleep(5)

    monkeypatch.setattr(history_module, "get_instance", lambda hass: SlowRecorder())

    with pytest.raises(TimeoutError):
        await CachedHistory.async_load(
            hass,
            Window.last(dt_util.utcnow(), 60),
            {"m": HistorySource("sensor.temp")},
            0.05,
        )


async def test_ai_mode_and_profile_changes_are_not_cached(
    hass, entry, monkeypatch
) -> None:
    hass.states.async_set("sensor.temp", "57.8", {"unit_of_measurement": "°C"})
    coordinator = await _setup(hass, entry)
    system = await coordinator.async_create_system("Отопление", "heating")
    measurement = await coordinator.async_add_measurement(
        system_id=system.id, entity_id="sensor.temp", role="actual", unit="°C"
    )
    await coordinator.async_save_profile(
        {
            "id": "p_ai",
            "name": "Первый",
            "detail": "compact",
            "required_measurement_ids": [measurement.id],
        }
    )
    calls: list[object] = []

    class FakeProvider:
        async def async_analyze(self, package):
            calls.append(package)
            return AI_REPLY

    monkeypatch.setattr(coordinator, "_provider", lambda: FakeProvider())

    without_ai = await coordinator.async_generate_report("p_ai", use_ai=False)
    with_ai = await coordinator.async_generate_report("p_ai", use_ai=True)
    again_with_ai = await coordinator.async_generate_report("p_ai", use_ai=True)

    assert without_ai.used_ai is False
    assert with_ai.used_ai is True
    assert "Вывод ИИ" in with_ai.text
    assert len(calls) == 1
    assert again_with_ai is with_ai

    auto = await coordinator.async_generate_report("p_ai")
    assert auto is without_ai

    await coordinator.async_save_profile(
        {
            "id": "p_ai",
            "name": "Второй",
            "detail": "compact",
            "required_measurement_ids": [measurement.id],
        }
    )
    changed = await coordinator.async_generate_report("p_ai", use_ai=False)

    assert changed is not without_ai
    assert "Второй" in changed.text


async def test_report_flow_reaches_mock_telegram(
    hass: HomeAssistant, entry, hass_ws_client, monkeypatch
) -> None:
    hass.states.async_set("sensor.boiler", "57.8", {"unit_of_measurement": "°C"})
    hass.states.async_set("sensor.boiler_target", "60.0", {"unit_of_measurement": "°C"})
    messages: list[dict] = []

    async def handler(call) -> None:
        messages.append(dict(call.data))

    hass.services.async_register("notify", NOTIFY_SERVICE, handler)
    hass.config_entries.async_update_entry(
        entry,
        options={
            CONF_TELEGRAM_SERVICE: NOTIFY_SERVICE,
            CONF_TELEGRAM_RECIPIENTS: ["12345"],
        },
    )
    coordinator = await _setup(hass, entry)
    client = await hass_ws_client(hass)

    created = await _send(
        client,
        {"id": 1, "type": f"{DOMAIN}/system/create", "name": "Отопление", "preset": "heating"},
    )
    system_id = created["result"]["system"]["id"]
    actual = await _send(
        client,
        {
            "id": 2,
            "type": f"{DOMAIN}/measurement/add",
            "system_id": system_id,
            "entity_id": "sensor.boiler",
            "name": "Котёл",
            "role": "actual",
            "unit": "°C",
        },
    )
    target = await _send(
        client,
        {
            "id": 3,
            "type": f"{DOMAIN}/measurement/add",
            "system_id": system_id,
            "entity_id": "sensor.boiler_target",
            "name": "Цель котла",
            "role": "setpoint",
            "unit": "°C",
        },
    )
    actual_id = actual["result"]["measurement"]["id"]
    target_id = target["result"]["measurement"]["id"]

    saved = await _send(
        client,
        {
            "id": 4,
            "type": f"{DOMAIN}/profile/save",
            "profile": {
                "id": "p_e2e",
                "name": "Вечерний",
                "detail": "compact",
                "required_measurement_ids": [actual_id, target_id],
                "max_optional": 0,
            },
        },
    )
    assert saved["success"] is True

    response = await hass.services.async_call(
        DOMAIN,
        "generate_report",
        {"profile_id": "p_e2e", "use_ai": False},
        blocking=True,
        return_response=True,
    )

    assert response["used_ai"] is False
    assert "57,8" in response["text"]
    assert "60,0" in response["text"]
    assert set(response) >= {"text", "detail", "used_ai", "attention"}
    node = shutil.which("node")
    if node is not None:
        frontend = subprocess.run(
            [node, str(FRONTEND_CHECK)],
            input=json.dumps({"response": response}, ensure_ascii=False),
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=PROJECT_ROOT,
            check=True,
        )
        message = json.loads(frontend.stdout)
        assert message["text"] == response["text"]
        assert message["usedAi"] is False
    assert len(messages) == 1
    assert messages[0]["message"] == response["text"]
    assert messages[0]["target"] == "12345"
    assert messages[0]["data"]["chat_id"] == "12345"

    class FakeProvider:
        async def async_analyze(self, package):
            return AI_REPLY

    monkeypatch.setattr(coordinator, "_provider", lambda: FakeProvider())
    ai_response = await hass.services.async_call(
        DOMAIN,
        "generate_report",
        {"profile_id": "p_e2e", "use_ai": True},
        blocking=True,
        return_response=True,
    )

    assert ai_response["used_ai"] is True
    assert "Вывод ИИ" in ai_response["text"]
    assert len(messages) == 2

    await client.close()
