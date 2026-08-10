#!/usr/bin/env python3
"""Offline conversions for common household units."""

from __future__ import annotations

import argparse
import math


GROUPS: dict[str, dict[str, float]] = {
    "length": {
        "mm": 0.001,
        "cm": 0.01,
        "m": 1.0,
        "km": 1000.0,
        "in": 0.0254,
        "ft": 0.3048,
        "yd": 0.9144,
        "mi": 1609.344,
    },
    "weight": {
        "mg": 0.000001,
        "g": 0.001,
        "kg": 1.0,
        "oz": 0.028349523125,
        "lb": 0.45359237,
    },
    "area": {
        "cm2": 0.0001,
        "m2": 1.0,
        "km2": 1_000_000.0,
        "in2": 0.00064516,
        "ft2": 0.09290304,
        "yd2": 0.83612736,
        "acre": 4046.8564224,
        "ha": 10_000.0,
        "tsubo": 3.30578512397,
    },
    "volume": {
        "ml": 0.001,
        "l": 1.0,
        "m3": 1000.0,
        "tsp": 0.00492892159375,
        "tbsp": 0.01478676478125,
        "cup": 0.2365882365,
        "pt": 0.473176473,
        "qt": 0.946352946,
        "gal": 3.785411784,
    },
    "speed": {
        "m/s": 1.0,
        "km/h": 1 / 3.6,
        "mph": 0.44704,
        "knot": 0.514444444444,
    },
}

ALIASES = {
    "millimeter": "mm",
    "millimeters": "mm",
    "centimeter": "cm",
    "centimeters": "cm",
    "meter": "m",
    "meters": "m",
    "metre": "m",
    "metres": "m",
    "kilometer": "km",
    "kilometers": "km",
    "inch": "in",
    "inches": "in",
    "foot": "ft",
    "feet": "ft",
    "yard": "yd",
    "yards": "yd",
    "mile": "mi",
    "miles": "mi",
    "gram": "g",
    "grams": "g",
    "kilogram": "kg",
    "kilograms": "kg",
    "pound": "lb",
    "pounds": "lb",
    "lbs": "lb",
    "ounce": "oz",
    "ounces": "oz",
    "liter": "l",
    "liters": "l",
    "litre": "l",
    "litres": "l",
    "坪": "tsubo",
    "kph": "km/h",
    "kmh": "km/h",
    "ms": "m/s",
    "knots": "knot",
    "celsius": "c",
    "fahrenheit": "f",
    "kelvin": "k",
    "°c": "c",
    "°f": "f",
}

DISPLAY = {
    "c": "°C",
    "f": "°F",
    "k": "K",
    "cm2": "cm²",
    "m2": "m²",
    "km2": "km²",
    "in2": "in²",
    "ft2": "ft²",
    "yd2": "yd²",
    "m3": "m³",
    "tsubo": "坪",
}


def normalize(raw: str) -> str:
    value = raw.strip().lower().replace("²", "2").replace("³", "3")
    return ALIASES.get(value, value)


def category(unit: str) -> str | None:
    if unit in {"c", "f", "k"}:
        return "temperature"
    for name, units in GROUPS.items():
        if unit in units:
            return name
    return None


def to_celsius(value: float, unit: str) -> float:
    if unit == "c":
        return value
    if unit == "f":
        return (value - 32) * 5 / 9
    return value - 273.15


def from_celsius(value: float, unit: str) -> float:
    if unit == "c":
        return value
    if unit == "f":
        return value * 9 / 5 + 32
    return value + 273.15


def convert(value: float, source: str, target: str) -> float:
    source_category = category(source)
    target_category = category(target)
    if source_category is None or target_category is None:
        raise ValueError("未対応の単位です。--listで確認してください。")
    if source_category != target_category:
        raise ValueError("異なる種類の単位は変換できません。")
    if source_category == "temperature":
        celsius = to_celsius(value, source)
        if celsius < -273.15 - 1e-9:
            raise ValueError("絶対零度より低い温度は変換できません。")
        return from_celsius(celsius, target)
    base = value * GROUPS[source_category][source]
    return base / GROUPS[target_category][target]


def format_number(value: float) -> str:
    if not math.isfinite(value):
        raise ValueError("数値が大きすぎます。")
    if abs(value) < 5e-13:
        value = 0.0
    return f"{value:.8g}"


def print_units() -> None:
    print("長さ: mm cm m km in ft yd mi")
    print("重さ: mg g kg oz lb")
    print("温度: C F K")
    print("面積: cm2 m2 km2 in2 ft2 yd2 acre ha tsubo(坪)")
    print("体積: ml l m3 tsp tbsp cup pt qt gal（米国単位）")
    print("速度: m/s km/h mph knot")


def main() -> None:
    parser = argparse.ArgumentParser(description="一般的な単位をオフラインで変換します。")
    parser.add_argument("value", nargs="?", type=float)
    parser.add_argument("source", nargs="?")
    parser.add_argument("target", nargs="?")
    parser.add_argument("--list", action="store_true", help="対応単位を表示")
    args = parser.parse_args()

    if args.list:
        print_units()
        return
    if args.value is None or args.source is None or args.target is None:
        print("使い方: units.py 数値 変換元 変換先")
        raise SystemExit(1)

    source = normalize(args.source)
    target = normalize(args.target)
    try:
        result = convert(args.value, source, target)
        source_text = DISPLAY.get(source, source)
        target_text = DISPLAY.get(target, target)
        print(f"{format_number(args.value)} {source_text} は {format_number(result)} {target_text} です。")
    except ValueError as exc:
        print(str(exc))
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
