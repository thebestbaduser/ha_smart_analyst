"""Тесты очереди уведомлений и хранилища конфигурации."""

from __future__ import annotations

import json
from datetime import UTC, datetime, time, timedelta

import pytest
from core.errors import SchemaError, StorageError
from core.model import HomeModel, Measurement, Site, SourceRef
from core.notifications import (
    MessagePriority,
    MessageState,
    NotificationQueue,
    QuietHours,
    message_key,
)
from core.storage import (
    SCHEMA_VERSION,
    ConfigState,
    export_configuration,
    import_configuration,
    load_config,
    migrate,
    validate_state,
)

NOW = datetime(2026, 10, 30, 20, 0, tzinfo=UTC)


def test_message_deduplication_by_key() -> None:
    queue = NotificationQueue()
    first = queue.enqueue(
        event_key="evt-1",
        recipient="tg:1",
        priority=MessagePriority.WARNING,
        text="первое",
        now=NOW,
    )
    second = queue.enqueue(
        event_key="evt-1",
        recipient="tg:1",
        priority=MessagePriority.WARNING,
        text="второе",
        now=NOW + timedelta(seconds=5),
    )
    assert first.key == second.key == message_key("evt-1", "tg:1")
    assert len(queue) == 1
    assert second.text == "первое"


def test_priority_order() -> None:
    queue = NotificationQueue()
    queue.enqueue(event_key="info", recipient="a", priority=MessagePriority.INFO, text="i", now=NOW)
    queue.enqueue(event_key="crit", recipient="a", priority=MessagePriority.CRITICAL, text="c", now=NOW)
    message = queue.next_message(NOW)
    assert message is not None
    assert message.priority is MessagePriority.CRITICAL
    assert message.state is MessageState.SENDING


def test_quiet_hours_skip_non_critical_only() -> None:
    queue = NotificationQueue()
    quiet = QuietHours(start=time(22, 0), end=time(7, 0))
    night = NOW + timedelta(hours=3)
    queue.enqueue(event_key="warning", recipient="a", priority=MessagePriority.WARNING, text="w", now=NOW)
    assert queue.next_message(night, quiet=quiet) is None
    queue.enqueue(event_key="critical", recipient="a", priority=MessagePriority.CRITICAL, text="c", now=NOW)
    message = queue.next_message(night, quiet=quiet)
    assert message is not None
    assert message.priority is MessagePriority.CRITICAL
    assert QuietHours(start=time(22, 0), end=time(7, 0)).contains(datetime(2026, 10, 30, 2, 0, tzinfo=UTC))
    assert not QuietHours(start=time(22, 0), end=time(7, 0)).contains(
        datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
    )


def test_failure_retries_then_gives_up() -> None:
    queue = NotificationQueue(max_attempts=2)
    queue.enqueue(event_key="e", recipient="a", priority=MessagePriority.WARNING, text="x", now=NOW)
    message = queue.next_message(NOW)
    assert message is not None
    retried = queue.mark_failed(message.key, "timeout", NOW)
    assert retried is not None
    assert retried.state is MessageState.RETRYING
    assert retried.next_attempt_at == NOW + timedelta(seconds=60)

    second = queue.next_message(NOW + timedelta(minutes=2))
    assert second is not None
    final = queue.mark_failed(second.key, "timeout", NOW + timedelta(minutes=2))
    assert final is not None
    assert final.state is MessageState.FAILED


def test_suppress_incident_keeps_incident_alive() -> None:
    queue = NotificationQueue()
    queue.enqueue(
        event_key="e1", recipient="a", priority=MessagePriority.WARNING, text="x", now=NOW, incident_id="inc-1"
    )
    assert queue.suppress_incident("inc-1") == 1
    assert all(message.state is MessageState.SUPPRESSED for message in queue.messages)


def test_restart_marks_uncertain_delivery() -> None:
    queue = NotificationQueue()
    queue.enqueue(event_key="e", recipient="a", priority=MessagePriority.CRITICAL, text="x", now=NOW)
    assert queue.next_message(NOW) is not None
    assert queue.restore() == 1
    message = queue.messages[0]
    assert message.state is MessageState.RETRYING
    assert message.uncertain is True


def test_queue_round_trip_and_prune() -> None:
    queue = NotificationQueue()
    queue.enqueue(event_key="e", recipient="a", priority=MessagePriority.INFO, text="x", now=NOW)
    message = queue.next_message(NOW)
    assert message is not None
    queue.mark_sent(message.key)

    restored = NotificationQueue.from_list(queue.to_list(), NOW + timedelta(hours=1))
    assert len(restored) == 1
    assert restored.messages[0].state is MessageState.SENT
    assert restored.prune(NOW + timedelta(days=30)) == 1


def sample_state() -> ConfigState:
    model = HomeModel()
    model.set_site(Site(id="site", name="Дом"))
    model.add_measurement(
        Measurement(
            id="voltage_l1",
            name="L1",
            unit="V",
            system_id=None,
            source=SourceRef("sensor.l1_voltage", registry_entry_id="reg-1"),
        )
    )
    return ConfigState(model=model, settings={"locale": "ru", "api_key": "secret-value"})


def test_migration_from_unversioned_payload() -> None:
    payload = {"model": HomeModel().to_dict(), "settings": {"locale": "en"}}
    migrated, backup = migrate(payload)
    assert migrated["schema_version"] == SCHEMA_VERSION
    assert backup is not None
    assert migrated["settings"]["locale"] == "en"
    assert migrated["rules"] == []


def test_migration_rejects_future_version() -> None:
    with pytest.raises(StorageError):
        migrate({"schema_version": SCHEMA_VERSION + 1})


def test_load_config_rejects_corrupted_json() -> None:
    with pytest.raises(StorageError):
        load_config('{"model": ', NOW)


def test_export_strips_secrets() -> None:
    exported = export_configuration(sample_state(), NOW)
    assert "secret-value" not in exported
    payload = json.loads(exported)
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["exported_at"]


def test_round_trip_through_export_and_import() -> None:
    state = sample_state()
    restored = import_configuration(export_configuration(state, NOW))
    assert restored.model.require_measurement("voltage_l1").source is not None
    assert restored.settings["locale"] == "ru"


def test_validate_state_rejects_broken_rules_container() -> None:
    payload = ConfigState().to_dict()
    payload["rules"] = "не список"
    with pytest.raises(SchemaError):
        validate_state(payload)
