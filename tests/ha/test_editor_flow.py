"""Редактор панели: системы, датчики, правила через WebSocket (UI-03, UI-04)."""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_smart_analyst.const import DOMAIN


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _send(client, payload: dict) -> dict:
    await client.send_json(payload)
    return await client.receive_json()


async def test_editor_flow_creates_system_measurement_and_rule(hass, entry, hass_ws_client) -> None:
    hass.states.async_set(
        "sensor.boiler_supply",
        "57.8",
        {"unit_of_measurement": "°C", "device_class": "temperature", "friendly_name": "Подача котла"},
    )
    await _setup(hass, entry)
    client = await hass_ws_client(hass)

    catalog = await _send(
        client,
        {"id": 1, "type": f"{DOMAIN}/entities", "query": "подача", "only_unused": True},
    )
    assert catalog["success"] is True
    entities = catalog["result"]["entities"]
    assert [item["entity_id"] for item in entities] == ["sensor.boiler_supply"]
    suggestion = entities[0]["suggestion"]
    assert suggestion["role"] == "supply"
    assert suggestion["preset"] == "heating"
    assert suggestion["needs_confirmation"] is True

    created = await _send(
        client,
        {"id": 2, "type": f"{DOMAIN}/system/create", "name": "Отопление", "preset": "heating"},
    )
    assert created["success"] is True
    system_id = created["result"]["system"]["id"]

    added = await _send(
        client,
        {
            "id": 3,
            "type": f"{DOMAIN}/measurement/add",
            "system_id": system_id,
            "entity_id": "sensor.boiler_supply",
            "name": "Подача котла",
            "role": "supply",
            "unit": "°C",
        },
    )
    assert added["success"] is True
    measurement_id = added["result"]["measurement"]["id"]

    duplicate = await _send(
        client,
        {
            "id": 4,
            "type": f"{DOMAIN}/measurement/add",
            "system_id": system_id,
            "entity_id": "sensor.boiler_supply",
            "role": "supply",
        },
    )
    assert duplicate["success"] is False

    rule = {
        "id": "rule_supply_high",
        "name": "Подача выше 50",
        "severity": "warning",
        "scope_id": system_id,
        "expression": {
            "op": "gt",
            "left": {"measurement_id": measurement_id},
            "right": {"value": 50, "unit": "°C"},
        },
        "for_seconds": 0,
    }
    saved = await _send(client, {"id": 5, "type": f"{DOMAIN}/rule/save", "rule": rule})
    assert saved["success"] is True
    assert saved["result"]["rule"]["for_seconds"] == 0

    tested = await _send(client, {"id": 6, "type": f"{DOMAIN}/rule_test", "rule_id": "rule_supply_high"})
    assert tested["success"] is True
    assert tested["result"]["value"] is True
    assert tested["result"]["deviation"] == pytest.approx(7.8)

    overview = await _send(client, {"id": 7, "type": f"{DOMAIN}/overview"})
    assert overview["success"] is True
    assert overview["result"]["systems"][0]["measurements"][0]["value"] == pytest.approx(57.8)
    assert overview["result"]["rules"][0]["name"] == "Подача выше 50"

    deleted_rule = await _send(client, {"id": 8, "type": f"{DOMAIN}/rule/delete", "rule_id": "rule_supply_high"})
    assert deleted_rule["success"] is True

    deleted_measurement = await _send(
        client, {"id": 9, "type": f"{DOMAIN}/measurement/delete", "measurement_id": measurement_id}
    )
    assert deleted_measurement["success"] is True

    deleted_system = await _send(
        client, {"id": 10, "type": f"{DOMAIN}/system/delete", "system_id": system_id}
    )
    assert deleted_system["success"] is True

    await client.close()


async def test_invalid_rule_is_rejected_by_server(hass, entry, hass_ws_client) -> None:
    hass.states.async_set("sensor.temp", "20", {"unit_of_measurement": "°C"})
    await _setup(hass, entry)
    client = await hass_ws_client(hass)

    created = await _send(
        client, {"id": 1, "type": f"{DOMAIN}/system/create", "name": "Климат", "preset": "climate"}
    )
    system_id = created["result"]["system"]["id"]
    added = await _send(
        client,
        {
            "id": 2,
            "type": f"{DOMAIN}/measurement/add",
            "system_id": system_id,
            "entity_id": "sensor.temp",
            "role": "actual",
            "unit": "°C",
        },
    )
    measurement_id = added["result"]["measurement"]["id"]

    mismatched = {
        "id": "bad_rule",
        "name": "Несопоставимые единицы",
        "expression": {
            "op": "gt",
            "left": {"measurement_id": measurement_id},
            "right": {"value": 1, "unit": "V"},
        },
    }
    rejected = await _send(client, {"id": 3, "type": f"{DOMAIN}/rule/save", "rule": mismatched})
    assert rejected["success"] is False
    assert "несовместим" in rejected["error"]["message"] or "несопоставим" in rejected["error"]["message"].lower()

    unknown = {
        "id": "bad_rule_2",
        "name": "Неизвестный датчик",
        "expression": {"op": "gt", "left": {"measurement_id": "nope"}, "right": {"value": 1}},
    }
    rejected = await _send(client, {"id": 4, "type": f"{DOMAIN}/rule/save", "rule": unknown})
    assert rejected["success"] is False

    await client.close()
