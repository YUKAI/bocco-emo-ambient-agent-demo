---
name: datetime
description: "Local time, date arithmetic, and Japanese era years."
version: 1.0.0
author: BOCCO Ambient Robot
license: Apache-2.0
dependencies: []
platforms: [linux]
metadata:
  hermes:
    tags: [date, time, weekday, calendar, date-arithmetic, reiwa, japanese]
    category: home
    requires_toolsets: [terminal]
---

# Datetime Skill

Report the Pi's local date and time, calculate dates, and give the Japanese
Reiwa year. Everything runs offline using the Pi's configured timezone.

## When to Use

- The user asks for the current date, time, or day of week.
- The user asks what date it will be after a number of days or weeks.
- The user asks for the Reiwa year of a date.

## Prerequisites

Python 3.11+ using only the standard library. No network or credentials are
required.

Script path: `~/.hermes/skills/datetime/scripts/datetime_tool.py`

## Commands

```bash
DATETIME=~/.hermes/skills/datetime/scripts/datetime_tool.py
```

### now — Local date and time

```bash
python3 "$DATETIME"
python3 "$DATETIME" now
```

Returns the Gregorian date, Japanese weekday, Reiwa year, local time, and
timezone name.

### date — Describe a date

```bash
python3 "$DATETIME" date
python3 "$DATETIME" date 2026-08-03
```

The date format is exactly `YYYY-MM-DD`. Omitting it means today.

### add — Date arithmetic

```bash
python3 "$DATETIME" add 10 days
python3 "$DATETIME" add 2 weeks --from 2026-08-03
python3 "$DATETIME" add -3 days
```

Units are `day`, `days`, `week`, or `weeks`. A negative amount calculates a
past date. `--from` defaults to today.

## Output

The script prints one concise Japanese sentence. Read it as-is. Do not replace
the result with a time inferred from the model or another machine.

Dates from 2019-05-01 onward include their Reiwa year. Earlier dates are marked
as preceding Reiwa rather than being assigned another era.

## Errors

Invalid dates produce a short Japanese message and a non-zero status. Correct
the command or ask the user to clarify an ambiguous date.

## Verification

```bash
python3 "$DATETIME" now
python3 "$DATETIME" add 10 days --from 2026-08-03
python3 "$DATETIME" date 2019-05-01
python3 "$DATETIME" --help
```
