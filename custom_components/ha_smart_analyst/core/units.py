"""Нормализация единиц измерения (DATA-05).

Правило DATA-05: приводить совместимые единицы, не смешивать мощность и
энергию, хранить исходное значение. Неизвестные единицы и значения NaN/inf
отклоняются, а не «угадываются».

Значение в базовой единице: ``base = value * factor + offset``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

from .errors import InvalidValueError, UnitError

#: Единицы, у которых базовая единица имеет смещение (температура).
TEMPERATURE: Final = "temperature"
#: Единицы отношения (мощность, энергия, давление, расход, время, проценты).
RATIO: Final = "ratio"


@dataclass(frozen=True, slots=True)
class Unit:
    """Единица измерения и её связь с базовой единицей размерности."""

    symbol: str
    dimension: str
    factor: float
    offset: float = 0.0

    def to_base(self, value: float) -> float:
        return value * self.factor + self.offset

    def from_base(self, value: float) -> float:
        return (value - self.offset) / self.factor


@dataclass(frozen=True, slots=True)
class Dimension:
    """Размерность: базовая единица и допустимые единицы."""

    name: str
    base: str
    kind: str
    units: dict[str, Unit]


def _ratio(dimension: str, base: str, **units: float) -> Dimension:
    table: dict[str, Unit] = {}
    for symbol, factor in units.items():
        table[symbol] = Unit(symbol, dimension, factor)
    table[base] = Unit(base, dimension, 1.0)
    return Dimension(dimension, base, RATIO, table)


_DIMENSIONS: Final[dict[str, Dimension]] = {
    dim.name: dim
    for dim in (
        Dimension(
            "temperature",
            "°C",
            TEMPERATURE,
            {
                "°C": Unit("°C", "temperature", 1.0),
                "°F": Unit("°F", "temperature", 5.0 / 9.0, -160.0 / 9.0),
                "K": Unit("K", "temperature", 1.0, -273.15),
            },
        ),
        _ratio("power", "W", W=1.0, kW=1_000.0, MW=1_000_000.0),
        _ratio("energy", "Wh", Wh=1.0, kWh=1_000.0, MWh=1_000_000.0, J=1.0 / 3600.0),
        _ratio("pressure", "Pa", Pa=1.0, kPa=1_000.0, MPa=1_000_000.0, bar=100_000.0, mbar=100.0),
        _ratio("voltage", "V", V=1.0, mV=0.001, kV=1_000.0),
        _ratio("current", "A", A=1.0, mA=0.001),
        _ratio("flow", "m³/h", **{"m³/h": 1.0, "l/min": 0.06, "l/h": 0.001, "m3/h": 1.0}),
        _ratio("volume", "m³", **{"m³": 1.0, "l": 0.001, "m3": 1.0}),
        _ratio("time", "s", s=1.0, min=60.0, h=3_600.0),
        _ratio("percent", "%", **{"%": 1.0, "‰": 0.1}),
        _ratio("frequency", "Hz", Hz=1.0, kHz=1_000.0),
        _ratio("ratio", "", **{"": 1.0, "%": 100.0}),
    )
}

#: Синонимы, которые встречаются в Home Assistant и русских переводах.
_ALIASES: Final[dict[str, str]] = {
    "c": "°C",
    "°c": "°C",
    "celsius": "°C",
    "f": "°F",
    "°f": "°F",
    "fahrenheit": "°F",
    "kelvin": "K",
    "квт": "kW",
    "w": "W",
    "вт": "W",
    "квт·ч": "kWh",
    "kwh": "kWh",
    "wh": "Wh",
    "бар": "bar",
    "па": "Pa",
    "м3/ч": "m³/h",
    "m3h": "m³/h",
    "m3/h": "m³/h",
    "процент": "%",
}


def find_unit(symbol: str | None) -> Unit | None:
    """Найти единицу по символу или синониму; ``None`` для пустой строки."""
    if symbol is None:
        return None
    text = symbol.strip()
    if not text:
        return None
    text = _ALIASES.get(text.lower(), _ALIASES.get(text, text))
    for dimension in _DIMENSIONS.values():
        if text in dimension.units:
            return dimension.units[text]
    return None


def require_unit(symbol: str | None, path: str = "unit") -> Unit:
    """Вернуть единицу или поднять ``UnitError`` с понятным текстом."""
    unit = find_unit(symbol)
    if unit is None:
        raise UnitError(f"{path}: неизвестная единица измерения {symbol!r}")
    return unit


def validate_number(value: object, path: str = "value") -> float:
    """Проверить, что значение — конечное число (DATA-05)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidValueError(f"{path}: ожидалось число, получено {type(value).__name__}")
    number = float(value)
    if math.isnan(number) or math.isinf(number):
        raise InvalidValueError(f"{path}: значение {number!r} недопустимо")
    return number


def compatible(left: str | None, right: str | None) -> bool:
    """Совместимы ли единицы (одна размерность или обе не заданы)."""
    left_unit = find_unit(left)
    right_unit = find_unit(right)
    if left_unit is None or right_unit is None:
        return left_unit is None and right_unit is None
    return left_unit.dimension == right_unit.dimension


def convert(value: float, from_unit: str | None, to_unit: str | None, path: str = "value") -> float:
    """Перевести значение в другую единицу той же размерности."""
    number = validate_number(value, path)
    source = find_unit(from_unit)
    target = find_unit(to_unit)
    if source is None and target is None:
        return number
    if source is None or target is None:
        raise UnitError(f"{path}: нельзя перевести {from_unit!r} в {to_unit!r}: единица не определена")
    if source.dimension != target.dimension:
        raise UnitError(
            f"{path}: несовместимые размерности {source.dimension} и {target.dimension}"
        )
    return target.from_base(source.to_base(number))


def same_dimension(first: str | None, second: str | None) -> bool:
    """Строгая проверка: обе единицы известны и относятся к одной размерности."""
    one = find_unit(first)
    two = find_unit(second)
    return one is not None and two is not None and one.dimension == two.dimension
