"""История: агрегаты, покрытие и запрет выдуманных трендов (HIST-01…HIST-04).

Модуль работает с уже полученными сэмплами. Доступ к Recorder — задача
адаптера интеграции; ядро отвечает за расчёты и за честные ответы «данных
недостаточно» вместо догадок (HIST-02, AC-10).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from .observations import Quality
from .units import UnitError, convert


@dataclass(frozen=True, slots=True)
class Sample:
    """Точка истории."""

    at: datetime
    value: float
    quality: Quality = Quality.OK


@dataclass(frozen=True, slots=True)
class Window:
    """Окно анализа (HIST-03)."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError("конец окна должен быть позже начала")

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()

    @classmethod
    def last(cls, now: datetime, seconds: float) -> Window:
        return cls(now - timedelta(seconds=seconds), now)


class HistoryProvider(Protocol):
    """Минимальный интерфейс источника истории (HIST-08, P2-адаптеры)."""

    def samples(self, measurement_id: str, window: Window) -> Sequence[Sample]:
        """Сэмплы в окне; пустой список, если истории нет."""


@dataclass(frozen=True, slots=True)
class Coverage:
    """Качество покрытия окна данными."""

    ratio: float
    samples: int
    largest_gap_seconds: float | None
    reason: str | None = None

    @property
    def usable(self) -> bool:
        return self.reason is None


@dataclass(frozen=True, slots=True)
class Aggregate:
    """Детерминированные метрики окна (HIST-04)."""

    count: int
    minimum: float | None
    maximum: float | None
    mean: float | None
    first: float | None
    last: float | None
    delta: float | None
    rate_per_second: float | None
    coverage: float
    switches: int | None = None
    time_above_threshold: float | None = None
    energy: float | None = None
    reason: str | None = None

    @property
    def usable(self) -> bool:
        return self.reason is None


@dataclass(frozen=True, slots=True)
class Trend:
    """Разность и скорость за окно, либо причина отказа."""

    delta: float | None
    rate_per_second: float | None
    coverage: Coverage
    unit: str | None = None

    @property
    def usable(self) -> bool:
        return self.delta is not None and self.coverage.usable


def _valid(samples: Sequence[Sample]) -> list[Sample]:
    return [
        sample
        for sample in samples
        if sample.quality in {Quality.OK, Quality.STALE}
        and not math.isnan(sample.value)
        and not math.isinf(sample.value)
    ]


def evaluate_coverage(samples: Sequence[Sample], window: Window, min_ratio: float = 0.5) -> Coverage:
    """Оценить покрытие окна: доля времени между крайними сэмплами и разрывы."""
    usable = sorted(_valid(samples), key=lambda sample: sample.at)
    if len(usable) < 2:
        return Coverage(0.0, len(usable), None, "нет истории за окно")
    span = (usable[-1].at - usable[0].at).total_seconds()
    gaps = [
        (later.at - earlier.at).total_seconds()
        for earlier, later in zip(usable, usable[1:], strict=False)
    ]
    largest_gap = max(gaps) if gaps else None
    ratio = min(1.0, span / window.seconds) if window.seconds else 0.0
    reason = None
    if ratio < min_ratio:
        reason = f"покрытие {ratio:.0%} ниже порога {min_ratio:.0%}"
    elif largest_gap is not None and largest_gap > max(window.seconds * 0.5, 900):
        reason = f"разрыв в данных {largest_gap:.0f} с"
    return Coverage(ratio, len(usable), largest_gap, reason)


def summarize(samples: Sequence[Sample], window: Window) -> Aggregate:
    """Базовые метрики без догадок: при недостатке данных — ``reason``."""
    coverage = evaluate_coverage(samples, window)
    usable = sorted(_valid(samples), key=lambda sample: sample.at)
    if len(usable) < 2:
        return Aggregate(
            count=len(usable),
            minimum=None,
            maximum=None,
            mean=None,
            first=None,
            last=None,
            delta=None,
            rate_per_second=None,
            coverage=coverage.ratio,
            reason=coverage.reason or "недостаточно сэмплов",
        )
    values = [sample.value for sample in usable]
    delta = usable[-1].value - usable[0].value
    span = (usable[-1].at - usable[0].at).total_seconds()
    return Aggregate(
        count=len(usable),
        minimum=min(values),
        maximum=max(values),
        mean=sum(values) / len(values),
        first=usable[0].value,
        last=usable[-1].value,
        delta=delta,
        rate_per_second=delta / span if span > 0 else None,
        coverage=coverage.ratio,
        reason=coverage.reason,
    )


def trend(samples: Sequence[Sample], window: Window, min_ratio: float = 0.5) -> Trend:
    """Разность и скорость за окно с обязательной оговоркой о покрытии."""
    coverage = evaluate_coverage(samples, window, min_ratio)
    usable = sorted(_valid(samples), key=lambda sample: sample.at)
    if len(usable) < 2 or not coverage.usable:
        return Trend(None, None, coverage)
    delta = usable[-1].value - usable[0].value
    span = (usable[-1].at - usable[0].at).total_seconds()
    return Trend(delta, delta / span if span > 0 else None, coverage)


def switch_count(samples: Sequence[Sample], threshold: float) -> int:
    """Число пересечений порога (для правил гистерезиса и статистики)."""
    usable = sorted(_valid(samples), key=lambda sample: sample.at)
    count = 0
    for previous, current in zip(usable, usable[1:], strict=False):
        if (previous.value < threshold) != (current.value < threshold):
            count += 1
    return count


def time_above(samples: Sequence[Sample], window: Window, threshold: float) -> float | None:
    """Время выше порога; ``None``, если покрытие недостаточно."""
    coverage = evaluate_coverage(samples, window)
    usable = sorted(_valid(samples), key=lambda sample: sample.at)
    if len(usable) < 2 or not coverage.usable:
        return None
    total = 0.0
    for earlier, later in zip(usable, usable[1:], strict=False):
        if earlier.value > threshold:
            total += (later.at - earlier.at).total_seconds()
    return total


def integrate_energy(samples: Sequence[Sample], window: Window) -> float | None:
    """Интегрировать мощность в энергию (HIST-02).

    Энергия считается только при достаточной плотности: иначе результат был бы
    выдуманным. Единица результата совпадает с ``unit`` мощности, умноженной на
    час (Вт·ч для Вт).
    """
    coverage = evaluate_coverage(samples, window)
    usable = sorted(_valid(samples), key=lambda sample: sample.at)
    if len(usable) < 2 or not coverage.usable:
        return None
    total = 0.0
    for earlier, later in zip(usable, usable[1:], strict=False):
        hours = (later.at - earlier.at).total_seconds() / 3_600.0
        total += (earlier.value + later.value) / 2.0 * hours
    return total


def counter_total(samples: Sequence[Sample], unit: str | None = None) -> tuple[float | None, int]:
    """Суммарный прирост счётчика ``total_increasing`` с учётом сбросов.

    Возвращает (прирост, число обнаруженных сбросов). Для счётчика сброс — это
    уменьшение значения между соседними сэмплами.
    """
    usable = sorted(_valid(samples), key=lambda sample: sample.at)
    if len(usable) < 2:
        return None, 0
    total = 0.0
    resets = 0
    for earlier, later in zip(usable, usable[1:], strict=False):
        if later.value >= earlier.value:
            total += later.value - earlier.value
        else:
            resets += 1
            total += later.value
    return total, resets


def convert_window(samples: Sequence[Sample], from_unit: str | None, to_unit: str | None) -> list[Sample]:
    """Перевести сэмплы в целевую единицу, не смешивая размерности."""
    if not from_unit or not to_unit or from_unit == to_unit:
        return list(samples)
    try:
        return [Sample(sample.at, convert(sample.value, from_unit, to_unit), sample.quality) for sample in samples]
    except UnitError:
        return list(samples)
