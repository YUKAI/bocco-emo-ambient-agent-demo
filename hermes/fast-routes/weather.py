#!/usr/bin/env python3
"""Print bounded current weather for a location as one speech-ready sentence."""

from __future__ import annotations

import argparse
import json
import os
from urllib.parse import quote
from urllib.request import Request, urlopen


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", default="")
    parser.add_argument("--location", default="")
    args = parser.parse_args()
    location = (
        args.location.strip() or os.environ.get("DEFAULT_LOCATION", "").strip()
    )
    if not location or len(location) > 100:
        return 2
    request = Request(
        f"https://wttr.in/{quote(location)}?format=j1&lang=ja",
        headers={"User-Agent": "bocco-bridge-fast-route/1"},
    )
    with urlopen(request, timeout=7) as response:
        payload = json.load(response)
    current = payload["current_condition"][0]
    descriptions = current.get("lang_ja") or current.get("weatherDesc") or []
    description = descriptions[0].get("value", "") if descriptions else ""

    parts: list[str] = []
    if description:
        parts.append(f"{location}の今の天気は{description}です。")
    temperature = str(current.get("temp_C") or "").strip()
    feels_like = str(current.get("FeelsLikeC") or "").strip()
    if temperature:
        sentence = f"気温は{temperature}度"
        if feels_like and feels_like != temperature:
            sentence += f"、体感は{feels_like}度"
        parts.append(sentence + "です。")
    humidity = str(current.get("humidity") or "").strip()
    if humidity:
        parts.append(f"湿度は{humidity}パーセントです。")
    if not parts:
        return 2
    print("".join(parts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
