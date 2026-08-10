---
name: weather
description: "Current and two-day weather via Open-Meteo."
version: 1.1.0
author: BOCCO Ambient Robot
license: Apache-2.0
dependencies: []
platforms: [linux]
metadata:
  hermes:
    tags: [weather, forecast, rain, umbrella, temperature, postal-code, open-meteo, japanese]
    category: home
    requires_toolsets: [terminal]
---

# Weather Skill

Get current conditions plus today's and tomorrow's forecast from Open-Meteo.
The output is one concise Japanese sentence suitable for a speech robot.

## When to Use

- The user asks about current weather or temperature.
- The user asks for today's or tomorrow's forecast.
- The user asks whether they need an umbrella.
- The user gives a Japanese postal code for their home location.

## Prerequisites

Python 3.11+ using only the standard library. No API key is required.

Script path: `~/.hermes/skills/weather/scripts/weather.py`

Optional environment variable:

- `DEFAULT_LOCATION`: fallback place name or Japanese postal code when the user
  does not name a location. It is intentionally not set by this skill.

## Usage

A default location is configured on this device. When the user does not name a
place, ALWAYS invoke the script with NO location argument — it uses the
configured default. Only pass a location when the user names one. If no default
is configured the script says so clearly — only then ask the user.

```bash
WEATHER=~/.hermes/skills/weather/scripts/weather.py

# Explicit location
python3 "$WEATHER" Tokyo
python3 "$WEATHER" "Yokohama, Japan"

# Japanese postal codes: seven digits, optional hyphen and 〒 prefix
python3 "$WEATHER" 100-0001
python3 "$WEATHER" 1000001
python3 "$WEATHER" 〒100-0001

# Use DEFAULT_LOCATION
python3 "$WEATHER"
DEFAULT_LOCATION='100-0001' python3 "$WEATHER"
DEFAULT_LOCATION='Tokyo' python3 "$WEATHER"
```

The location may contain spaces. Pass it as one quoted argument or as multiple
arguments. An explicit location always overrides `DEFAULT_LOCATION`.

Postal codes are resolved to prefecture, city, and town with Zipcloud before
the resolved Japanese place name is sent to Open-Meteo geocoding.

Location resolution is cached by the exact input string, so a cached place or
postal code skips both Zipcloud and geocoding. Forecast responses are cached
for about 10 minutes. The cache is a small JSON file next to the script.

`--warm-cache` is an internal prefetch option. It resolves `DEFAULT_LOCATION`
and force-refreshes its forecast cache without printing a user-facing forecast.
A warm failure is reported with a non-zero exit status so callers can treat it
as best-effort.

## Output

The script prints one short Japanese line containing:

- resolved place name;
- current condition, temperature, and apparent temperature;
- today's and tomorrow's condition, low/high, and maximum rain probability;
- a short umbrella recommendation for today.

Read the result as-is or shorten it further. Do not add raw JSON or unsupported
forecast details.

## Data Source and Network Behavior

- Geocoding: `https://geocoding-api.open-meteo.com/v1/search`
- Forecast: `https://api.open-meteo.com/v1/forecast`
- Japanese postal lookup: `https://zipcloud.ibsnet.co.jp/api/search`
- Each request has an 8-second timeout and a 1 MB response limit.
- Open-Meteo weather codes are converted to short Japanese descriptions.

## Errors

Errors are short Japanese sentences and return a non-zero status. If no location
was given and `DEFAULT_LOCATION` is unset, ask the user for a place. For a
postal-code lookup or network error, say that the location or weather is
temporarily unavailable; do not invent data.

## Verification

```bash
python3 "$WEATHER" Tokyo
python3 "$WEATHER" 100-0001
DEFAULT_LOCATION=Tokyo python3 "$WEATHER"
DEFAULT_LOCATION=100-0001 python3 "$WEATHER"
DEFAULT_LOCATION=100-0001 python3 "$WEATHER" --warm-cache
python3 "$WEATHER" --help
```
