---
name: units
description: "Offline conversions for common household units."
version: 1.0.0
author: BOCCO Ambient Robot
license: Apache-2.0
dependencies: []
platforms: [linux]
metadata:
  hermes:
    tags: [units, conversion, length, weight, temperature, area, volume, speed]
    category: home
    requires_toolsets: [terminal]
---

# Units Skill

Convert common length, weight, temperature, area, volume, and speed units.
Calculations are deterministic, offline, and use Python's standard library.

## When to Use

- The user asks to convert measurements or cooking volumes.
- The user asks to convert Celsius, Fahrenheit, or Kelvin.
- The user asks to convert floor area, including Japanese tsubo.
- The user asks to convert travel or wind speed.

Do not use this skill for currency conversion.

## Prerequisites

Python 3.11+ using only the standard library. No network or credentials are
required.

Script path: `~/.hermes/skills/units/scripts/units.py`

## Usage

```bash
UNITS=~/.hermes/skills/units/scripts/units.py

python3 "$UNITS" 10 km mi
python3 "$UNITS" 32 F C
python3 "$UNITS" 1 坪 m2
python3 "$UNITS" 250 ml cup
python3 "$UNITS" 60 km/h mph
python3 "$UNITS" --list
```

The command format is `VALUE SOURCE_UNIT TARGET_UNIT`.

## Supported Units

- Length: `mm cm m km in ft yd mi`
- Weight: `mg g kg oz lb`
- Temperature: `C F K`
- Area: `cm2 m2 km2 in2 ft2 yd2 acre ha tsubo` (`坪` is an alias)
- Volume: `ml l m3 tsp tbsp cup pt qt gal`
- Speed: `m/s km/h mph knot`

US customary definitions are used for `tsp`, `tbsp`, `cup`, `pt`, `qt`, and
`gal`. Common English names such as `meters`, `pounds`, and `liters` are also
accepted.

## Output

The script prints one concise Japanese sentence with the input and converted
value. Read it as-is, rounding conversationally only when that improves speech.

## Errors

Unsupported units, cross-category conversions, non-numeric values, and
temperatures below absolute zero produce a short Japanese message and a non-zero
status. Use `--list` to inspect valid unit symbols.

## Verification

```bash
python3 "$UNITS" 10 km mi
python3 "$UNITS" 32 F C
python3 "$UNITS" 1 坪 m2
python3 "$UNITS" --help
```
