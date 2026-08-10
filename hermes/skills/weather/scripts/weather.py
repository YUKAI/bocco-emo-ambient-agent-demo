#!/usr/bin/env python3
"""Concise Japanese weather from Open-Meteo."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ZIPCLOUD_URL = "https://zipcloud.ibsnet.co.jp/api/search"
HTTP_TIMEOUT_SECONDS = 8
MAX_RESPONSE_BYTES = 1_000_000
USER_AGENT = "BOCCO-Hermes-Weather/1.1.0"
POSTAL_CODE_RE = re.compile(r"^\s*〒?\s*(\d{3})-?(\d{4})\s*$")
CACHE_PATH = Path(__file__).with_name(".location_cache.json")
CACHE_VERSION = 1
FORECAST_TTL_SECONDS = 600
MAX_LOCATION_CACHE_ENTRIES = 32
MAX_FORECAST_CACHE_ENTRIES = 16

WEATHER_JA = {
    0: "快晴",
    1: "晴れ",
    2: "晴れ時々くもり",
    3: "くもり",
    45: "霧",
    48: "霧",
    51: "弱い霧雨",
    53: "霧雨",
    55: "強い霧雨",
    56: "弱い着氷性の霧雨",
    57: "着氷性の霧雨",
    61: "弱い雨",
    63: "雨",
    65: "強い雨",
    66: "弱い着氷性の雨",
    67: "着氷性の雨",
    71: "弱い雪",
    73: "雪",
    75: "大雪",
    77: "細かい雪",
    80: "弱いにわか雨",
    81: "にわか雨",
    82: "激しいにわか雨",
    85: "弱いにわか雪",
    86: "強いにわか雪",
    95: "雷雨",
    96: "ひょうを伴う雷雨",
    99: "激しいひょうを伴う雷雨",
}

RAIN_CODES = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99}


class WeatherError(Exception):
    """Expected user-facing weather failure."""


class LocationNotFound(WeatherError):
    """A location query returned no geocoding result."""


def fail(message: str) -> "None":
    print(message)
    raise SystemExit(1)


def fetch_json(
    url: str,
    params: dict[str, object],
    connection_error: str = "天気情報に接続できませんでした。",
) -> dict:
    full_url = url + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        full_url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            payload = response.read(MAX_RESPONSE_BYTES + 1)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
        raise WeatherError(connection_error) from exc

    if len(payload) > MAX_RESPONSE_BYTES:
        raise WeatherError("天気情報の応答が大きすぎます。")
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WeatherError("天気情報を読み取れませんでした。") from exc
    if not isinstance(data, dict) or data.get("error"):
        raise WeatherError("天気情報を取得できませんでした。")
    return data


def empty_cache() -> dict:
    return {"version": CACHE_VERSION, "locations": {}, "forecasts": {}}


def load_cache() -> dict:
    try:
        payload = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return empty_cache()
    if not isinstance(payload, dict) or payload.get("version") != CACHE_VERSION:
        return empty_cache()
    locations = payload.get("locations")
    forecasts = payload.get("forecasts")
    if not isinstance(locations, dict):
        locations = {}
    if not isinstance(forecasts, dict):
        forecasts = {}
    return {
        "version": CACHE_VERSION,
        "locations": locations,
        "forecasts": forecasts,
    }


def _timestamp(entry: object, field: str) -> float:
    if not isinstance(entry, dict):
        return 0.0
    value = entry.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _trim_cache(cache: dict) -> None:
    locations = cache.get("locations")
    forecasts = cache.get("forecasts")
    if not isinstance(locations, dict):
        locations = {}
        cache["locations"] = locations
    if not isinstance(forecasts, dict):
        forecasts = {}
        cache["forecasts"] = forecasts

    if len(locations) > MAX_LOCATION_CACHE_ENTRIES:
        newest_locations = sorted(
            locations.items(),
            key=lambda pair: _timestamp(pair[1], "resolved_at"),
            reverse=True,
        )[:MAX_LOCATION_CACHE_ENTRIES]
        cache["locations"] = dict(newest_locations)

    now = time.time()
    usable_forecasts = {
        key: entry
        for key, entry in forecasts.items()
        if -60 <= now - _timestamp(entry, "fetched_at") < FORECAST_TTL_SECONDS
    }
    if len(usable_forecasts) > MAX_FORECAST_CACHE_ENTRIES:
        usable_forecasts = dict(
            sorted(
                usable_forecasts.items(),
                key=lambda pair: _timestamp(pair[1], "fetched_at"),
                reverse=True,
            )[:MAX_FORECAST_CACHE_ENTRIES]
        )
    cache["forecasts"] = usable_forecasts


def save_cache(cache: dict, *, required: bool = False) -> None:
    cache["version"] = CACHE_VERSION
    _trim_cache(cache)
    encoded = (json.dumps(cache, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    descriptor: int | None = None
    temporary: Path | None = None
    try:
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{CACHE_PATH.name}.", dir=CACHE_PATH.parent
        )
        temporary = Path(temp_name)
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, CACHE_PATH)
        temporary = None
        os.chmod(CACHE_PATH, 0o644)
        directory_fd = os.open(CACHE_PATH.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        if required:
            raise WeatherError("天気キャッシュを保存できませんでした。") from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def cached_location(cache: dict, key: str) -> tuple[str, dict] | None:
    locations = cache.get("locations")
    entry = locations.get(key) if isinstance(locations, dict) else None
    if not isinstance(entry, dict):
        return None
    locality = entry.get("locality")
    latitude = entry.get("lat")
    longitude = entry.get("lon")
    if not isinstance(locality, str) or not locality.strip():
        return None
    if (
        isinstance(latitude, bool)
        or not isinstance(latitude, (int, float))
        or not math.isfinite(float(latitude))
        or isinstance(longitude, bool)
        or not isinstance(longitude, (int, float))
        or not math.isfinite(float(longitude))
    ):
        return None
    return locality, {
        "latitude": float(latitude),
        "longitude": float(longitude),
    }


def cached_forecast(cache: dict, key: str) -> dict | None:
    forecasts = cache.get("forecasts")
    entry = forecasts.get(key) if isinstance(forecasts, dict) else None
    if not isinstance(entry, dict):
        return None
    fetched_at = _timestamp(entry, "fetched_at")
    age = time.time() - fetched_at
    data = entry.get("data")
    if -60 <= age < FORECAST_TTL_SECONDS and isinstance(data, dict):
        return data
    return None


def geocode(location: str) -> dict:
    data = fetch_json(
        GEOCODING_URL,
        {"name": location, "count": 1, "language": "ja", "format": "json"},
    )
    results = data.get("results")
    if not isinstance(results, list) or not results:
        raise LocationNotFound(f"{location}の場所が見つかりませんでした。")
    result = results[0]
    if not isinstance(result, dict) or "latitude" not in result or "longitude" not in result:
        raise WeatherError("場所の情報を読み取れませんでした。")
    return result


def postal_code(raw: str) -> str | None:
    match = POSTAL_CODE_RE.fullmatch(raw)
    return "".join(match.groups()) if match else None


def resolve_postal_code(code: str) -> tuple[str, list[str]]:
    data = fetch_json(
        ZIPCLOUD_URL,
        {"zipcode": code, "limit": 1},
        "郵便番号を確認できませんでした。",
    )
    results = data.get("results")
    if data.get("status") != 200 or not isinstance(results, list) or not results:
        raise WeatherError(f"郵便番号{code[:3]}-{code[3:]}が見つかりませんでした。")
    result = results[0]
    if not isinstance(result, dict):
        raise WeatherError("郵便番号の住所を読み取れませんでした。")

    parts = [
        result.get(key, "").strip()
        for key in ("address1", "address2", "address3")
        if isinstance(result.get(key), str) and result.get(key, "").strip()
    ]
    if not parts:
        raise WeatherError("郵便番号の住所を読み取れませんでした。")

    full_address = "".join(parts)
    candidates = [full_address]
    if len(parts) >= 2:
        candidates.extend((parts[0] + parts[1], parts[1]))
    candidates.append(parts[0])
    return full_address, list(dict.fromkeys(candidates))


def geocode_candidates(candidates: list[str], postal: str | None = None) -> dict:
    for candidate in candidates:
        try:
            return geocode(candidate)
        except LocationNotFound:
            continue
    if postal:
        raise LocationNotFound(
            f"郵便番号{postal[:3]}-{postal[3:]}の場所が見つかりませんでした。"
        )
    raise LocationNotFound(f"{candidates[0]}の場所が見つかりませんでした。")


def weather_description(code: object) -> str:
    try:
        return WEATHER_JA.get(int(code), "天気不明")
    except (TypeError, ValueError):
        return "天気不明"


def number(value: object, suffix: str = "") -> str:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return "不明"
    rounded = round(numeric, 1)
    text = str(int(rounded)) if rounded.is_integer() else f"{rounded:.1f}"
    return text + suffix


def item(values: object, index: int) -> object | None:
    if isinstance(values, list) and len(values) > index:
        return values[index]
    return None


def location_name(place: dict) -> str:
    parts: list[str] = []
    for key in ("name", "admin1"):
        value = place.get(key)
        if isinstance(value, str) and value and value not in parts:
            parts.append(value)
    return "、".join(parts) or "指定した場所"


def resolve_location(
    requested: str,
    cache: dict,
    *,
    cache_required: bool = False,
) -> tuple[str, dict, bool]:
    cached = cached_location(cache, requested)
    if cached is not None:
        locality, place = cached
        return locality, place, True

    code = postal_code(requested)
    locality = ""
    candidates = [requested]
    if code:
        locality, candidates = resolve_postal_code(code)
    place = geocode_candidates(candidates, code)
    if not locality:
        locality = location_name(place)

    try:
        latitude = float(place["latitude"])
        longitude = float(place["longitude"])
    except (KeyError, TypeError, ValueError) as exc:
        raise WeatherError("場所の情報を読み取れませんでした。") from exc
    if not math.isfinite(latitude) or not math.isfinite(longitude):
        raise WeatherError("場所の情報を読み取れませんでした。")

    locations = cache.setdefault("locations", {})
    if not isinstance(locations, dict):
        locations = {}
        cache["locations"] = locations
    locations[requested] = {
        "locality": locality,
        "lat": latitude,
        "lon": longitude,
        "resolved_at": time.time(),
    }
    save_cache(cache, required=cache_required)
    return locality, {"latitude": latitude, "longitude": longitude}, False


def forecast_sentence(label: str, daily: dict, index: int) -> str:
    code = item(daily.get("weather_code"), index)
    low = number(item(daily.get("temperature_2m_min"), index), "度")
    high = number(item(daily.get("temperature_2m_max"), index), "度")
    rain = number(item(daily.get("precipitation_probability_max"), index), "％")
    return f"{label}は{weather_description(code)}、{low}から{high}、雨{rain}"


def umbrella_advice(daily: dict) -> str:
    probability = item(daily.get("precipitation_probability_max"), 0)
    precipitation = item(daily.get("precipitation_sum"), 0)
    code = item(daily.get("weather_code"), 0)
    try:
        probability_value = float(probability)
    except (TypeError, ValueError):
        probability_value = 0.0
    try:
        precipitation_value = float(precipitation)
    except (TypeError, ValueError):
        precipitation_value = 0.0
    try:
        weather_code = int(code)
    except (TypeError, ValueError):
        weather_code = -1

    if probability_value >= 60 or precipitation_value >= 5:
        return "今日は傘が必要です"
    if probability_value >= 30 or precipitation_value > 0 or weather_code in RAIN_CODES:
        return "折りたたみ傘があると安心です"
    return "今日は傘はたぶん不要です"


def get_forecast(place: dict) -> dict:
    return fetch_json(
        FORECAST_URL,
        {
            "latitude": place["latitude"],
            "longitude": place["longitude"],
            "current": "temperature_2m,apparent_temperature,weather_code",
            "daily": (
                "weather_code,temperature_2m_max,temperature_2m_min,"
                "precipitation_probability_max,precipitation_sum"
            ),
            "timezone": "auto",
            "forecast_days": 2,
        },
    )


def forecast_for(
    requested: str,
    place: dict,
    cache: dict,
    *,
    force_refresh: bool = False,
    cache_required: bool = False,
) -> tuple[dict, bool]:
    if not force_refresh:
        data = cached_forecast(cache, requested)
        if data is not None:
            return data, True

    data = get_forecast(place)
    current = data.get("current")
    daily = data.get("daily")
    if not isinstance(current, dict) or not isinstance(daily, dict):
        raise WeatherError("天気情報を読み取れませんでした。")

    forecasts = cache.setdefault("forecasts", {})
    if not isinstance(forecasts, dict):
        forecasts = {}
        cache["forecasts"] = forecasts
    forecasts[requested] = {"fetched_at": time.time(), "data": data}
    save_cache(cache, required=cache_required)
    return data, False


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Open-Meteoの現在・今日・明日の天気を短い日本語で返します。"
    )
    parser.add_argument("location", nargs="*", help="場所名。省略時はDEFAULT_LOCATION。")
    parser.add_argument(
        "--warm-cache",
        action="store_true",
        help="DEFAULT_LOCATIONの場所解決と予報キャッシュを更新して終了します。",
    )
    args = parser.parse_args()

    if args.warm_cache and args.location:
        parser.error("--warm-cacheでは場所を指定できません。")

    default_location = os.environ.get("DEFAULT_LOCATION", "").strip()
    requested = default_location if args.warm_cache else " ".join(args.location).strip() or default_location
    if not requested:
        fail("場所を指定するか、DEFAULT_LOCATIONを設定してください。")

    try:
        cache = load_cache()
        started = time.monotonic()
        locality, place, location_cache_hit = resolve_location(
            requested,
            cache,
            cache_required=args.warm_cache,
        )
        if args.warm_cache:
            forecast_for(
                requested,
                place,
                cache,
                force_refresh=True,
                cache_required=True,
            )
            location_state = "hit" if location_cache_hit else "warmed"
            print(
                f"weather location cache {location_state}; forecast refreshed "
                f"in {time.monotonic() - started:.3f}s"
            )
            return

        data, _forecast_cache_hit = forecast_for(requested, place, cache)
        current = data.get("current")
        daily = data.get("daily")
        if not isinstance(current, dict) or not isinstance(daily, dict):
            raise WeatherError("天気情報を読み取れませんでした。")

        current_text = (
            f"{locality}。"
            f"現在は{weather_description(current.get('weather_code'))}、"
            f"{number(current.get('temperature_2m'), '度')}、"
            f"体感{number(current.get('apparent_temperature'), '度')}。"
        )
        today = forecast_sentence("今日", daily, 0)
        tomorrow = forecast_sentence("明日", daily, 1)
        print(f"{current_text}{today}。{tomorrow}。{umbrella_advice(daily)}。")
    except WeatherError as exc:
        fail(str(exc))


if __name__ == "__main__":
    main()
