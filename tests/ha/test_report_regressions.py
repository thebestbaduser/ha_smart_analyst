"""Регрессии отчётов из технического разбора: компактный отчёт, история, пустой отчёт."""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.ha_smart_analyst.const import DOMAIN
from custom_components.ha_smart_analyst.core import SmartAnalystError
from custom_components.ha_smart_analyst.core.engine import RuleRuntime, RuntimeState


async def _setup(hass: HomeAssistant, entry):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return hass.data[DOMAIN][entry.entry_id]


async def _prepare_sensor(hass: HomeAssistant, coordinator, unit: str = "°C") -> str:
    hass.states.async_set("sensor.temp", "57.8", {"unit_of_measurement": unit})
    system = await coordinator.async_create_system("Отопление", "heating")
    measurement = await coordinator.async_add_measurement(
        system_id=system.id, entity_id="sensor.temp", role="actual", unit=unit
    )
    return measurement.id


async def test_compact_report_contains_readings(hass, entry) -> None:
    coordinator = await _setup(hass, entry)
    await _prepare_sensor(hass, coordinator)
    await coordinator.async_save_profile(
        {"id": "compact", "name": "Короткий", "detail": "compact"}
    )

    rendered = await coordinator.async_generate_report("compact", use_ai=False)

    assert "Ключевые показатели" in rendered.text
    assert "57,8" in rendered.text
    assert rendered.detail.value == "compact"


async def test_empty_report_is_not_sent(hass, entry) -> None:
    coordinator = await _setup(hass, entry)

    with pytest.raises(SmartAnalystError):
        await coordinator.async_generate_report(use_ai=False)

    assert coordinator._last_report_at is None


async def test_attribute_measurement_reads_attribute(hass, entry) -> None:
    hass.states.async_set(
        "climate.living",
        "heat",
        {"current_temperature": 21.5, "temperature": 23.0, "unit_of_measurement": "°C"},
    )
    coordinator = await _setup(hass, entry)
    system = await coordinator.async_create_system("Климат", "climate")
    measurement = await coordinator.async_add_measurement(
        system_id=system.id,
        entity_id="climate.living",
        role="actual",
        unit="°C",
        attribute="current_temperature",
    )

    observations = coordinator.build_observations(dt_util.utcnow())
    observation = observations[measurement.id]
    assert observation.numeric_value == pytest.approx(21.5)


async def test_rule_runtimes_are_serialized(hass, entry) -> None:
    coordinator = await _setup(hass, entry)
    coordinator.runtimes["rule_x"] = RuleRuntime("rule_x", state=RuntimeState.ACTIVE)

    serialized = coordinator._serialize()

    assert serialized["runtime"]
    assert serialized["runtime"][0]["state"] == "active"
    assert "runtimes" not in serialized


async def test_fast_rules_do_not_wait_for_history(hass, entry) -> None:
    hass.states.async_set("binary_sensor.leak", "on")
    coordinator = await _setup(hass, entry)
    system = await coordinator.async_create_system("Протечка", "water")
    measurement = await coordinator.async_add_measurement(
        system_id=system.id, entity_id="binary_sensor.leak", role="leak"
    )
    await coordinator.async_save_rule(
        {
            "id": "rule_leak",
            "name": "Протечка",
            "severity": "critical",
            "scope_id": system.id,
            "expression": {
                "op": "state_is",
                "measurement_id": measurement.id,
                "state": "on",
            },
        }
    )

    async def fail_history(now):  # pragma: no cover - не должен вызываться
        raise AssertionError("история не должна читаться для быстрых правил")

    coordinator._async_load_history = fail_history  # type: ignore[method-assign]
    await coordinator._async_evaluate()

    assert coordinator._history is None
    assert coordinator.incidents.active()
