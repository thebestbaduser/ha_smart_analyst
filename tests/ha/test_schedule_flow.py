"""Планировщик отчётов: сохранение расписания и запуск по времени (REP-05)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_smart_analyst.const import DOMAIN


async def _send(client, payload: dict) -> dict:
    await client.send_json(payload)
    return await client.receive_json()


async def _setup(hass: HomeAssistant, entry: MockConfigEntry):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return hass.data[DOMAIN][entry.entry_id]


async def test_schedule_saved_and_shown(hass, entry, hass_ws_client) -> None:
    await _setup(hass, entry)
    client = await hass_ws_client(hass)
    profile = {
        "id": "p_evening",
        "name": "Вечерний отчёт",
        "locale": "ru",
        "detail": "adaptive",
        "timezone": "Europe/Samara",
        "schedule_times": ["20:00"],
        "schedule_weekdays": [],
    }
    saved = await _send(
        client, {"id": 1, "type": f"{DOMAIN}/profile/save", "profile": profile}
    )
    assert saved["success"] is True
    assert saved["result"]["profile"]["schedule_times"] == ["20:00"]
    assert saved["result"]["profile"]["report_type"] == "scheduled"

    overview = await _send(client, {"id": 2, "type": f"{DOMAIN}/overview"})
    assert overview["result"]["version"] == "0.2.0"
    report = overview["result"]["reports"][0]
    assert report["schedule"] == "ежедневно, 20:00"
    assert report["next_run"] is not None
    await client.close()


async def test_invalid_time_rejected(hass, entry, hass_ws_client) -> None:
    await _setup(hass, entry)
    client = await hass_ws_client(hass)
    rejected = await _send(
        client,
        {
            "id": 1,
            "type": f"{DOMAIN}/profile/save",
            "profile": {"id": "bad", "name": "Плохой", "schedule_times": ["25:00"]},
        },
    )
    assert rejected["success"] is False
    assert "HH:MM" in rejected["error"]["message"]
    await client.close()


async def test_due_report_is_generated(hass, entry) -> None:
    coordinator = await _setup(hass, entry)
    await coordinator.async_save_profile(
        {
            "id": "p_due",
            "name": "Пора",
            "timezone": "Europe/Samara",
            "schedule_times": ["20:00"],
        }
    )
    now = datetime.now(UTC)
    coordinator._report_next = {"p_due": now - timedelta(seconds=1)}

    completed = await coordinator.async_run_due_reports(now)

    assert completed == ["p_due"]
    assert coordinator._last_report_at is not None
    assert "p_due" in coordinator.next_report_runs()
