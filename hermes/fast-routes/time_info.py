#!/usr/bin/env python3
"""Print the host-local date, weekday, and time as one speech-ready sentence."""

from __future__ import annotations

import argparse
from datetime import datetime


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", default="")
    parser.parse_args()
    current = datetime.now().astimezone()
    weekdays = ("月曜日", "火曜日", "水曜日", "木曜日", "金曜日", "土曜日", "日曜日")
    print(
        f"今日は{current.year}年{current.month}月{current.day}日、"
        f"{weekdays[current.weekday()]}です。"
        f"時刻は{current.hour}時{current.minute}分です。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
