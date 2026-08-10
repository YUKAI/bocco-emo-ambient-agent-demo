#!/usr/bin/env python3
"""Offline local date/time, date arithmetic, and Reiwa conversion."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta


WEEKDAYS_JA = ("月曜日", "火曜日", "水曜日", "木曜日", "金曜日", "土曜日", "日曜日")
REIWA_START = date(2019, 5, 1)


def reiwa(value: date) -> str:
    if value < REIWA_START:
        return "令和以前"
    year = value.year - 2018
    return "令和元年" if year == 1 else f"令和{year}年"


def date_text(value: date) -> str:
    return f"{value.year}年{value.month}月{value.day}日（{WEEKDAYS_JA[value.weekday()]}）、{reiwa(value)}"


def parse_date(raw: str | None) -> date:
    if not raw or raw == "today":
        return datetime.now().astimezone().date()
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError("日付はYYYY-MM-DDで指定してください。") from exc


def show_now() -> None:
    now = datetime.now().astimezone()
    zone = now.tzname() or now.strftime("UTC%z")
    print(f"{date_text(now.date())}、{now.hour}時{now.minute:02d}分、{zone}です。")


def show_date(raw: str | None) -> None:
    print(f"{date_text(parse_date(raw))}です。")


def show_add(amount: int, unit: str, start_raw: str | None) -> None:
    start = parse_date(start_raw)
    days = amount * 7 if unit in {"week", "weeks"} else amount
    result = start + timedelta(days=days)
    magnitude = abs(amount)
    unit_ja = "週間" if unit in {"week", "weeks"} else "日"
    direction = "後" if amount >= 0 else "前"
    print(f"{magnitude}{unit_ja}{direction}は、{date_text(result)}です。")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Piの現地時刻、日付計算、令和年を返します。")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("now", help="現在の日時")

    date_parser = subparsers.add_parser("date", help="日付と曜日、令和年")
    date_parser.add_argument("value", nargs="?", help="YYYY-MM-DD。省略時は今日。")

    add_parser = subparsers.add_parser("add", help="日または週を加算")
    add_parser.add_argument("amount", type=int, help="加算する数。負数は過去。")
    add_parser.add_argument(
        "unit", nargs="?", default="days", choices=("day", "days", "week", "weeks")
    )
    add_parser.add_argument("--from", dest="start", help="基準日YYYY-MM-DD。省略時は今日。")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command in {None, "now"}:
            show_now()
        elif args.command == "date":
            show_date(args.value)
        elif args.command == "add":
            show_add(args.amount, args.unit, args.start)
    except ValueError as exc:
        print(str(exc))
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
