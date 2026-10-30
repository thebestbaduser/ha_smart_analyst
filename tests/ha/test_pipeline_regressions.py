"""Регрессии конвейера: атрибуты истории, провайдер, покрытие, медленный Recorder."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import timedelta
from types import SimpleNamespace

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.ha_smart_analyst import coordinator as coordinator_module
from custom_components.ha_smart_analyst import history_provider as history_module
from custom_components.ha_smart_analyst.const import DOMAIN
from custom_components.ha_smart_analyst.core.history import Sample, Window, summarize
from custom_components.ha_smart_analyst.core.observations import Quality
from custom_components.ha_smart_analyst.history_provider import (
    CachedHistory,
    HistorySource,
    _read_states,
    state_sample,
)
from custom_components.ha_smart_analyst.provider import DeepSeekProvider, ProviderConfig


async def _setup(hass: HomeAssistant, entry):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return hass.data[DOMAIN][entry.entry_id]


def test_state_sample_reads_attribute_not_mode() -> None:
    now = dt_util.utcnow()
    state = SimpleNamespace(
        state="heat",
        attributes={"current_temperature": 55.0},
        last_updated=now,
    )

    sample = state_sample(state, "current_temperature")

    assert sample is not None
    assert sample.value == 55.0
    assert state_sample(state, None) is None


def test_attribute_changes_give_delta(hass: HomeAssistant, monkeypatch) -> None:
    now = dt_util.utcnow()
    states = [
        SimpleNamespace(
            state="heat",
            attributes={"current_temperature": 55.0},
            last_updated=now - timedelta(minutes=10),
        ),
        SimpleNamespace(
            state="heat",
            attributes={"current_temperature": 58.0},
            last_updated=now,
        ),
    ]
    calls: list[object] = []

    def fake(hass, start, end, entity_id=None, **kwargs):
        calls.append(entity_id)
        return {entity_id: states}

    monkeypatch.setattr(history_module, "state_changes_during_period", fake)
    sources = {"m": HistorySource("climate.boiler", "current_temperature")}
    window = Window.last(now, 1_800)

    raw = _read_states(hass, window, sources)
    samples = raw["m"]

    assert calls == ["climate.boiler"]
    assert [sample.value for sample in samples] == [55.0, 58.0]
    assert summarize(samples, window).delta == pytest.approx(3.0)
    assert summarize(samples, window).coverage == pytest.approx(600 / 1_800)


def test_attribute_history_without_values_is_empty(hass: HomeAssistant, monkeypatch) -> None:
    now = dt_util.utcnow()
    monkeypatch.setattr(
        history_module,
        "state_changes_during_period",
        lambda *args, **kwargs: {
            "climate.boiler": [
                SimpleNamespace(
                    state="heat",
                    attributes={"current_temperature": "unavailable"},
                    last_updated=now,
                )
            ]
        },
    )
    raw = _read_states(
        hass,
        Window.last(now, 1_800),
        {"m": HistorySource("climate.boiler", "current_temperature")},
    )

    assert raw["m"] == []


def test_provider_sends_bearer_token_and_hides_key() -> None:
    config = ProviderConfig(api_key="secret-key", base_url="https://example.test", model="m")
    provider = DeepSeekProvider(None, config)  # type: ignore[arg-type]

    assert provider._headers["Authorization"] == "Bearer secret-key"
    assert "secret-key" not in repr(config)


async def test_provider_recreated_when_options_change(
    hass: HomeAssistant, entry, monkeypatch
) -> None:
    monkeypatch.setattr(coordinator_module, "async_get_clientsession", lambda hass: None)
    hass.config_entries.async_update_entry(entry, data={**entry.data, "ai_api_key": "old-key"})
    coordinator = await _setup(hass, entry)
    first = coordinator._provider()
    assert first is not None

    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "ai_api_key": "new-key"}
    )

    second = coordinator._provider()

    assert second is not None
    assert second is not first


async def test_system_coverage_averages_sensors(hass: HomeAssistant, entry, monkeypatch) -> None:
    hass.states.async_set("sensor.a", "20", {"unit_of_measurement": "°C"})
    hass.states.async_set("sensor.b", "21", {"unit_of_measurement": "°C"})
    coordinator = await _setup(hass, entry)
    system = await coordinator.async_create_system("Отопление", "heating")
    first = await coordinator.async_add_measurement(
        system_id=system.id, entity_id="sensor.a", unit="°C"
    )
    second = await coordinator.async_add_measurement(
        system_id=system.id, entity_id="sensor.b", unit="°C"
    )
    now = dt_util.utcnow()
    window = Window.last(now, 1_800)
    good = [
        Sample(now - timedelta(seconds=offset), 20.0, Quality.OK)
        for offset in range(1710, 0, -60)
    ] + [Sample(now, 20.0, Quality.OK)]
    poor = [
        Sample(now - timedelta(minutes=6), 20.0, Quality.OK),
        Sample(now, 21.0, Quality.OK),
    ]

    async def fake_load(cls, hass, window, sources, timeout_seconds=None):
        return CachedHistory({first.id: good, second.id: poor})

    monkeypatch.setattr(CachedHistory, "async_load", classmethod(fake_load))
    profile = coordinator._profile(None)

    sections, history_note = await coordinator._async_build_sections(
        profile, coordinator.build_observations(now), window
    )

    assert history_note is None
    assert len(sections) == 1
    assert sections[0].coverage == pytest.approx((0.95 + 0.2) / 2, abs=1e-6)
    assert sections[0].coverage != pytest.approx(0.95)


async def test_report_is_generated_once_for_concurrent_clicks(
    hass: HomeAssistant, entry
) -> None:
    hass.states.async_set("sensor.temp", "57.8", {"unit_of_measurement": "°C"})
    coordinator = await _setup(hass, entry)
    system = await coordinator.async_create_system("Отопление", "heating")
    await coordinator.async_add_measurement(system_id=system.id, entity_id="sensor.temp", unit="°C")

    first = await coordinator.async_generate_report(use_ai=False)
    second = await coordinator.async_generate_report(use_ai=False)

    assert first is second


async def test_fast_rule_does_not_wait_for_slow_history(hass: HomeAssistant, entry) -> None:
    hass.states.async_set("binary_sensor.leak", "on")
    hass.states.async_set("sensor.temp", "20", {"unit_of_measurement": "°C"})
    coordinator = await _setup(hass, entry)
    system = await coordinator.async_create_system("Дом", "custom")
    leak = await coordinator.async_add_measurement(
        system_id=system.id, entity_id="binary_sensor.leak", role="leak"
    )
    temperature = await coordinator.async_add_measurement(
        system_id=system.id, entity_id="sensor.temp", unit="°C"
    )
    await coordinator.async_save_rule(
        {
            "id": "rule_fast",
            "name": "Протечка",
            "severity": "critical",
            "scope_id": system.id,
            "expression": {"op": "state_is", "measurement_id": leak.id, "state": "on"},
        }
    )
    await coordinator.async_save_rule(
        {
            "id": "rule_slow",
            "name": "Резкий рост",
            "severity": "warning",
            "scope_id": system.id,
            "expression": {
                "op": "delta_above",
                "measurement_id": temperature.id,
                "window_seconds": 600,
                "threshold": {"value": 5, "unit": "°C"},
            },
        }
    )

    async def slow_history(now):
        await asyncio.sleep(5)
        return None

    coordinator._async_load_history = slow_history  # type: ignore[method-assign]
    task = hass.async_create_task(coordinator._async_evaluate())
    await asyncio.sleep(0.1)

    assert coordinator._history is None
    assert any(incident.rule_id == "rule_fast" for incident in coordinator.incidents.active())
    assert coordinator.queue.to_list()

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

async def test_fast_rule_survives_history_timeout(
    hass: HomeAssistant, entry, monkeypatch
) -> None:
    hass.states.async_set("binary_sensor.leak", "on")
    hass.states.async_set("sensor.temp", "20", {"unit_of_measurement": "°C"})
    coordinator = await _setup(hass, entry)
    system = await coordinator.async_create_system("Дом", "custom")
    leak = await coordinator.async_add_measurement(
        system_id=system.id, entity_id="binary_sensor.leak", role="leak"
    )
    temperature = await coordinator.async_add_measurement(
        system_id=system.id, entity_id="sensor.temp", unit="°C"
    )
    await coordinator.async_save_rule(
        {
            "id": "rule_fast_timeout",
            "name": "Протечка",
            "severity": "critical",
            "scope_id": system.id,
            "expression": {"op": "state_is", "measurement_id": leak.id, "state": "on"},
        }
    )
    await coordinator.async_save_rule(
        {
            "id": "rule_slow_timeout",
            "name": "Резкий рост",
            "severity": "warning",
            "scope_id": system.id,
            "expression": {
                "op": "delta_above",
                "measurement_id": temperature.id,
                "window_seconds": 600,
                "threshold": {"value": 5, "unit": "°C"},
            },
        }
    )
    coordinator.config_state.settings["history_timeout_seconds"] = 0.5

    async def slow_load(cls, hass, window, sources, timeout_seconds=None):
        await asyncio.sleep(5)

    monkeypatch.setattr(CachedHistory, "async_load", classmethod(slow_load))
    started = asyncio.get_running_loop().time()
    await coordinator._async_evaluate()
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 3
    assert any(
        incident.rule_id == "rule_fast_timeout"
        for incident in coordinator.incidents.active()
    )
