"""Очередь уведомлений (INC-06, INC-07, TG-03).

Доставка — best effort: уникальный ключ «событие + получатель» защищает от
дублей, но при неопределённом результате отправки возможен повтор. Очереди
разделены по приоритету; критическое не подавляется тихими часами.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta
from enum import IntEnum, StrEnum

from .observations import ensure_utc


class MessagePriority(IntEnum):
    """Приоритет сообщения; меньшее значение — выше приоритет."""

    CRITICAL = 0
    WARNING = 1
    INFO = 2


class MessageState(StrEnum):
    """Состояния доставки (INC-06)."""

    QUEUED = "queued"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    RETRYING = "retrying"
    SUPPRESSED = "suppressed"


@dataclass(frozen=True, slots=True)
class Message:
    """Сообщение очереди."""

    key: str
    recipient: str
    priority: MessagePriority
    text: str
    created_at: datetime
    state: MessageState = MessageState.QUEUED
    attempts: int = 0
    last_attempt_at: datetime | None = None
    next_attempt_at: datetime | None = None
    incident_id: str | None = None
    error: str | None = None
    uncertain: bool = False

    @property
    def is_pending(self) -> bool:
        return self.state in {MessageState.QUEUED, MessageState.RETRYING}

    @property
    def in_flight(self) -> bool:
        return self.state is MessageState.SENDING

    def to_dict(self) -> dict[str, object]:
        def iso(value: datetime | None) -> str | None:
            return value.isoformat() if value else None

        return {
            "key": self.key,
            "recipient": self.recipient,
            "priority": int(self.priority),
            "text": self.text,
            "created_at": iso(self.created_at),
            "state": self.state.value,
            "attempts": self.attempts,
            "last_attempt_at": iso(self.last_attempt_at),
            "next_attempt_at": iso(self.next_attempt_at),
            "incident_id": self.incident_id,
            "error": self.error,
            "uncertain": self.uncertain,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object], now: datetime) -> Message:
        moment = ensure_utc(now, now)

        def parse(key: str) -> datetime | None:
            raw = payload.get(key)
            return ensure_utc(datetime.fromisoformat(str(raw)), moment) if raw else None

        return cls(
            key=str(payload["key"]),
            recipient=str(payload["recipient"]),
            priority=MessagePriority(int(payload.get("priority", MessagePriority.INFO))),
            text=str(payload.get("text", "")),
            created_at=parse("created_at") or moment,
            state=MessageState(payload.get("state", "queued")),
            attempts=int(payload.get("attempts", 0)),
            last_attempt_at=parse("last_attempt_at"),
            next_attempt_at=parse("next_attempt_at"),
            incident_id=payload.get("incident_id"),  # type: ignore[arg-type]
            error=payload.get("error"),  # type: ignore[arg-type]
            uncertain=bool(payload.get("uncertain", False)),
        )


@dataclass(frozen=True, slots=True)
class QuietHours:
    """Тихие часы в локальном времени; критические сообщения их обходят."""

    start: time
    end: time

    def contains(self, moment: datetime) -> bool:
        local = moment.timetz().replace(tzinfo=None)
        if self.start <= self.end:
            return self.start <= local < self.end
        return local >= self.start or local < self.end


def message_key(event_key: str, recipient: str) -> str:
    """Уникальный ключ события и получателя (INC-06)."""
    return f"{event_key}@{recipient}"


class NotificationQueue:
    """Очередь сообщений с приоритетами, повторами и подавлением."""

    def __init__(self, max_size: int = 500, max_attempts: int = 3) -> None:
        self._messages: dict[str, Message] = {}
        self.max_size = max_size
        self.max_attempts = max_attempts

    def __len__(self) -> int:
        return len(self._messages)

    @property
    def messages(self) -> tuple[Message, ...]:
        return tuple(self._messages.values())

    def pending(self, now: datetime | None = None) -> tuple[Message, ...]:
        moment = ensure_utc(now, now) if now else None
        items = [
            message
            for message in self._messages.values()
            if message.is_pending
            and (moment is None or message.next_attempt_at is None or message.next_attempt_at <= moment)
        ]
        return tuple(sorted(items, key=lambda message: (message.priority, message.created_at)))

    def enqueue(
        self,
        *,
        event_key: str,
        recipient: str,
        priority: MessagePriority,
        text: str,
        now: datetime,
        incident_id: str | None = None,
    ) -> Message:
        """Поставить сообщение в очередь; повтор того же ключа не дублируется."""
        key = message_key(event_key, recipient)
        existing = self._messages.get(key)
        if existing is not None and existing.state in {
            MessageState.QUEUED,
            MessageState.SENDING,
            MessageState.RETRYING,
            MessageState.SENT,
        }:
            return existing
        self._evict_if_needed()
        message = Message(
            key=key,
            recipient=recipient,
            priority=priority,
            text=text,
            created_at=now,
            incident_id=incident_id,
        )
        self._messages[key] = message
        return message

    def _evict_if_needed(self) -> None:
        if len(self._messages) < self.max_size:
            return
        candidates = [
            message
            for message in self._messages.values()
            if message.state in {MessageState.SENT, MessageState.SUPPRESSED, MessageState.FAILED}
        ]
        if not candidates:
            candidates = [message for message in self._messages.values() if not message.is_pending]
        if not candidates:
            return
        victim = min(candidates, key=lambda message: (message.priority, message.created_at))
        del self._messages[victim.key]

    def next_message(
        self, now: datetime, quiet: QuietHours | None = None
    ) -> Message | None:
        """Взять следующее сообщение к отправке с учётом тихих часов."""
        for message in self.pending(now):
            if (
                quiet is not None
                and quiet.contains(now)
                and message.priority is not MessagePriority.CRITICAL
            ):
                continue
            if message.next_attempt_at is not None and message.next_attempt_at > now:
                continue
            updated = replace(
                message,
                state=MessageState.SENDING,
                attempts=message.attempts + 1,
                last_attempt_at=now,
            )
            self._messages[updated.key] = updated
            return updated
        return None

    def mark_sent(self, key: str) -> Message | None:
        message = self._messages.get(key)
        if message is None:
            return None
        updated = replace(message, state=MessageState.SENT, error=None, uncertain=False)
        self._messages[key] = updated
        return updated

    def mark_failed(
        self,
        key: str,
        error: str,
        now: datetime,
        retry_after: timedelta | None = None,
    ) -> Message | None:
        message = self._messages.get(key)
        if message is None:
            return None
        if message.attempts >= self.max_attempts:
            updated = replace(message, state=MessageState.FAILED, error=error)
        else:
            updated = replace(
                message,
                state=MessageState.RETRYING,
                error=error,
                next_attempt_at=now + (retry_after or timedelta(seconds=60)),
            )
        self._messages[key] = updated
        return updated

    def suppress(self, key: str) -> Message | None:
        message = self._messages.get(key)
        if message is None:
            return None
        updated = replace(message, state=MessageState.SUPPRESSED)
        self._messages[key] = updated
        return updated

    def suppress_incident(self, incident_id: str) -> int:
        """«Отключить повторы на 1 час» — только оповещения, инцидент живёт (TG-04)."""
        count = 0
        for key, message in list(self._messages.items()):
            if message.incident_id == incident_id and message.is_pending:
                self._messages[key] = replace(message, state=MessageState.SUPPRESSED)
                count += 1
        return count

    def restore(self) -> int:
        """После рестарта отправить неопределённые сообщения повторно (STO-04)."""
        count = 0
        for key, message in list(self._messages.items()):
            if message.state is MessageState.SENDING:
                self._messages[key] = replace(
                    message,
                    state=MessageState.RETRYING,
                    uncertain=True,
                    error="результат отправки неизвестен после перезапуска",
                )
                count += 1
        return count

    def prune(self, now: datetime, retention: timedelta = timedelta(days=7)) -> int:
        cutoff = ensure_utc(now, now) - retention
        removed = 0
        for key, message in list(self._messages.items()):
            if message.state in {MessageState.SENT, MessageState.SUPPRESSED, MessageState.FAILED} and (
                ensure_utc(message.created_at, now) < cutoff
            ):
                del self._messages[key]
                removed += 1
        return removed

    def to_list(self) -> list[dict[str, object]]:
        return [message.to_dict() for message in self._messages.values()]

    @classmethod
    def from_list(
        cls, payload: Iterable[Mapping[str, object]], now: datetime, max_size: int = 500
    ) -> NotificationQueue:
        queue = cls(max_size=max_size)
        for item in payload:
            message = Message.from_dict(item, now)
            queue._messages[message.key] = message
        return queue


def new_event_key(prefix: str = "evt") -> str:
    """Новый ключ события для дедупликации доставки."""
    return f"{prefix}-{uuid.uuid4()}"
