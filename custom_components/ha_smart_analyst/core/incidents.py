"""Жизненный цикл инцидентов и дедупликация (INC-01…INC-07).

Подтверждение не закрывает инцидент: ``ACKNOWLEDGED`` — это отметка «человек
увидел», а не «проблема исчезла». Повторные срабатывания одного правила в
одной ситуации обновляют существующий инцидент, а не создают новый.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum

from .engine import Reason, RuleEvent, RuleEventKind, Severity
from .observations import ensure_utc


class IncidentState(StrEnum):
    """Состояния инцидента (INC-01)."""

    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RECOVERING = "recovering"
    ESCALATED = "escalated"
    RESOLVED = "resolved"

    @property
    def is_active(self) -> bool:
        return self is not IncidentState.RESOLVED


class ResolutionKind(StrEnum):
    """Как инцидент был закрыт."""

    AUTOMATIC = "automatic"
    MANUAL = "manual"


@dataclass(frozen=True, slots=True)
class Escalation:
    at: datetime
    reason: str


@dataclass(frozen=True, slots=True)
class Incident:
    """Инцидент с фактами, а не догадками (INC-03)."""

    incident_id: str
    rule_id: str
    system_id: str | None
    severity: Severity
    opened_at: datetime
    last_observed_at: datetime
    state: IncidentState = IncidentState.OPEN
    ack_at: datetime | None = None
    ack_by: str | None = None
    recovery_started_at: datetime | None = None
    resolved_at: datetime | None = None
    resolution: ResolutionKind | None = None
    occurrences: int = 1
    peak_deviation: float | None = None
    peak_unit: str | None = None
    reasons: tuple[Reason, ...] = ()
    data_quality: bool = False
    escalations: tuple[Escalation, ...] = ()
    message_refs: tuple[str, ...] = ()

    @property
    def duration_seconds(self) -> float | None:
        if self.resolved_at is None:
            return None
        return (self.resolved_at - self.opened_at).total_seconds()

    @property
    def is_active(self) -> bool:
        return self.state.is_active

    def to_dict(self) -> dict[str, object]:
        return {
            "incident_id": self.incident_id,
            "rule_id": self.rule_id,
            "system_id": self.system_id,
            "severity": self.severity.value,
            "opened_at": self.opened_at.isoformat(),
            "last_observed_at": self.last_observed_at.isoformat(),
            "state": self.state.value,
            "ack_at": self.ack_at.isoformat() if self.ack_at else None,
            "ack_by": self.ack_by,
            "recovery_started_at": self.recovery_started_at.isoformat() if self.recovery_started_at else None,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "resolution": self.resolution.value if self.resolution else None,
            "occurrences": self.occurrences,
            "peak_deviation": self.peak_deviation,
            "peak_unit": self.peak_unit,
            "reasons": [
                {"code": reason.code, "message": reason.message, "measurement_id": reason.measurement_id}
                for reason in self.reasons
            ],
            "data_quality": self.data_quality,
            "escalations": [
                {"at": item.at.isoformat(), "reason": item.reason} for item in self.escalations
            ],
            "message_refs": list(self.message_refs),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object], now: datetime) -> Incident:
        moment = ensure_utc(now, now)

        def parse(key: str) -> datetime | None:
            raw = payload.get(key)
            return ensure_utc(datetime.fromisoformat(str(raw)), moment) if raw else None

        resolved_at = parse("resolved_at")
        return cls(
            incident_id=str(payload["incident_id"]),
            rule_id=str(payload["rule_id"]),
            system_id=payload.get("system_id"),  # type: ignore[arg-type]
            severity=Severity(payload.get("severity", "warning")),
            opened_at=parse("opened_at") or moment,
            last_observed_at=parse("last_observed_at") or moment,
            state=IncidentState(payload.get("state", "open")),
            ack_at=parse("ack_at"),
            ack_by=payload.get("ack_by"),  # type: ignore[arg-type]
            recovery_started_at=parse("recovery_started_at"),
            resolved_at=resolved_at,
            resolution=ResolutionKind(payload["resolution"]) if payload.get("resolution") else None,
            occurrences=int(payload.get("occurrences", 1)),
            peak_deviation=float(payload["peak_deviation"]) if payload.get("peak_deviation") is not None else None,
            peak_unit=payload.get("peak_unit"),  # type: ignore[arg-type]
            data_quality=bool(payload.get("data_quality", False)),
            escalations=tuple(
                Escalation(ensure_utc(datetime.fromisoformat(item["at"]), moment), str(item["reason"]))
                for item in payload.get("escalations", ())  # type: ignore[union-attr]
            ),
            message_refs=tuple(payload.get("message_refs", ())),  # type: ignore[arg-type]
        )


class IncidentManager:
    """Хранилище инцидентов с дедупликацией по правилу и области (INC-02)."""

    def __init__(self, incidents: Iterable[Incident] = ()) -> None:
        self._incidents: dict[str, Incident] = {item.incident_id: item for item in incidents}

    @property
    def incidents(self) -> tuple[Incident, ...]:
        return tuple(self._incidents.values())

    def active(self) -> tuple[Incident, ...]:
        return tuple(item for item in self._incidents.values() if item.is_active)

    def get(self, incident_id: str) -> Incident | None:
        return self._incidents.get(incident_id)

    def _find_active(self, rule_id: str, system_id: str | None) -> Incident | None:
        for incident in self._incidents.values():
            if (
                incident.rule_id == rule_id
                and incident.system_id == system_id
                and incident.is_active
            ):
                return incident
        return None

    def register(self, event: RuleEvent, system_id: str | None = None, unit: str | None = None) -> Incident:
        """Создать инцидент по событию активации или обновить существующий."""
        if event.kind is not RuleEventKind.ACTIVATED:
            raise ValueError("инцидент создаётся только событием activated")
        existing = self._find_active(event.rule_id, system_id)
        if existing is not None:
            peak = existing.peak_deviation
            if event.peak_deviation is not None:
                peak = event.peak_deviation if peak is None else max(abs(peak), abs(event.peak_deviation))
            updated = replace(
                existing,
                last_observed_at=event.at,
                occurrences=existing.occurrences + 1,
                peak_deviation=peak,
                peak_unit=unit or existing.peak_unit,
                reasons=event.reasons or existing.reasons,
                data_quality=existing.data_quality or event.data_quality,
            )
            self._incidents[updated.incident_id] = updated
            return updated
        incident = Incident(
            incident_id=str(uuid.uuid4()),
            rule_id=event.rule_id,
            system_id=system_id,
            severity=event.severity,
            opened_at=event.at,
            last_observed_at=event.at,
            occurrences=1,
            peak_deviation=abs(event.peak_deviation) if event.peak_deviation is not None else None,
            peak_unit=unit,
            reasons=event.reasons,
            data_quality=event.data_quality,
        )
        self._incidents[incident.incident_id] = incident
        return incident

    def observe(self, incident_id: str, at: datetime, reasons: tuple[Reason, ...] = ()) -> Incident:
        """Отметить продолжающееся наблюдение (без создания нового инцидента)."""
        incident = self._require(incident_id)
        if not incident.is_active:
            return incident
        updated = replace(
            incident,
            last_observed_at=at,
            reasons=reasons or incident.reasons,
        )
        self._incidents[incident_id] = updated
        return updated

    def acknowledge(self, incident_id: str, by: str, at: datetime) -> Incident:
        """Подтверждение фиксируется, но инцидент остаётся активным (INC-01)."""
        incident = self._require(incident_id)
        if not incident.is_active:
            return incident
        updated = replace(incident, ack_at=at, ack_by=by)
        if updated.state is IncidentState.OPEN:
            updated = replace(updated, state=IncidentState.ACKNOWLEDGED)
        self._incidents[incident_id] = updated
        return updated

    def escalate(self, incident_id: str, at: datetime, reason: str) -> Incident:
        incident = self._require(incident_id)
        if not incident.is_active:
            return incident
        updated = replace(
            incident,
            state=IncidentState.ESCALATED,
            escalations=(*incident.escalations, Escalation(at, reason)),
        )
        self._incidents[incident_id] = updated
        return updated

    def mark_recovering(self, incident_id: str, at: datetime) -> Incident:
        incident = self._require(incident_id)
        if not incident.is_active:
            return incident
        updated = replace(
            incident,
            state=IncidentState.RECOVERING,
            recovery_started_at=incident.recovery_started_at or at,
            last_observed_at=at,
        )
        self._incidents[incident_id] = updated
        return updated

    def resolve(
        self,
        incident_id: str,
        at: datetime,
        kind: ResolutionKind = ResolutionKind.AUTOMATIC,
        reasons: tuple[Reason, ...] = (),
    ) -> Incident:
        incident = self._require(incident_id)
        updated = replace(
            incident,
            state=IncidentState.RESOLVED,
            resolved_at=at,
            resolution=kind,
            last_observed_at=at,
            reasons=reasons or incident.reasons,
        )
        self._incidents[incident_id] = updated
        return updated

    def resolve_manually(self, incident_id: str, by: str, at: datetime, note: str = "") -> Incident:
        """Административное закрытие с пометкой (UI-07)."""
        incident = self._require(incident_id)
        updated = replace(
            incident,
            state=IncidentState.RESOLVED,
            resolved_at=at,
            resolution=ResolutionKind.MANUAL,
            ack_by=by,
            ack_at=incident.ack_at or at,
            reasons=(
                *incident.reasons,
                Reason("manual_resolution", note or f"закрыто вручную: {by}"),
            ),
        )
        self._incidents[incident_id] = updated
        return updated

    def attach_message(self, incident_id: str, message_key: str) -> None:
        incident = self._incidents.get(incident_id)
        if incident is None:
            return
        self._incidents[incident_id] = replace(
            incident, message_refs=(*incident.message_refs, message_key)
        )

    def prune(self, now: datetime, retention_days: int = 90, max_incidents: int | None = None) -> int:
        """Очистка журнала (STO-03): по сроку и по количеству."""
        cutoff = ensure_utc(now, now) - timedelta(days=retention_days)
        removed = 0
        for incident_id, incident in list(self._incidents.items()):
            if incident.resolved_at is not None and ensure_utc(incident.resolved_at, now) < cutoff:
                del self._incidents[incident_id]
                removed += 1
        if max_incidents is not None and len(self._incidents) > max_incidents:
            ordered = sorted(
                self._incidents.values(),
                key=lambda item: item.resolved_at or item.last_observed_at,
            )
            for incident in ordered[: len(self._incidents) - max_incidents]:
                if incident.is_active:
                    continue
                del self._incidents[incident.incident_id]
                removed += 1
        return removed

    def _require(self, incident_id: str) -> Incident:
        incident = self._incidents.get(incident_id)
        if incident is None:
            raise KeyError(f"неизвестный инцидент {incident_id!r}")
        return incident

    # -- сериализация -------------------------------------------------
    def to_list(self) -> list[dict[str, object]]:
        return [incident.to_dict() for incident in self._incidents.values()]

    @classmethod
    def from_list(cls, payload: Iterable[Mapping[str, object]], now: datetime) -> IncidentManager:
        return cls(Incident.from_dict(item, now) for item in payload)


@dataclass(frozen=True, slots=True)
class IncidentDigest:
    """Короткая сводка для отчёта и диагностики."""

    incident_id: str
    severity: Severity
    state: IncidentState
    rule_name: str
    opened_at: datetime
    duration_seconds: float | None
    peak_deviation: float | None
    peak_unit: str | None
    acknowledged: bool
    data_quality: bool


def digest(incident: Incident, rule_names: Mapping[str, str], now: datetime) -> IncidentDigest:
    duration = incident.duration_seconds
    if duration is None:
        duration = (ensure_utc(now, now) - ensure_utc(incident.opened_at, now)).total_seconds()
    return IncidentDigest(
        incident_id=incident.incident_id,
        severity=incident.severity,
        state=incident.state,
        rule_name=rule_names.get(incident.rule_id, incident.rule_id),
        opened_at=incident.opened_at,
        duration_seconds=duration,
        peak_deviation=incident.peak_deviation,
        peak_unit=incident.peak_unit,
        acknowledged=incident.ack_at is not None,
        data_quality=incident.data_quality,
    )
