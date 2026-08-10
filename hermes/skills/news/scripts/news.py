#!/usr/bin/env python3
"""Fetch a short list of titles from the official NHK RSS feed."""

from __future__ import annotations

import argparse
import html
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET


FEED_URLS = (
    "https://www3.nhk.or.jp/rss/news/cat0.xml",
    "https://news.web.nhk/n-data/conf/na/rss/cat0.xml",
)
HTTP_TIMEOUT_SECONDS = 8
MAX_RESPONSE_BYTES = 1_000_000
USER_AGENT = "BOCCO-Hermes-News/1.0"


class NewsError(Exception):
    """Expected user-facing RSS failure."""


def fetch_feed() -> bytes:
    for url in FEED_URLS:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/rss+xml, application/xml, text/xml",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                payload = response.read(MAX_RESPONSE_BYTES + 1)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
            continue
        if len(payload) > MAX_RESPONSE_BYTES:
            continue
        try:
            root = ET.fromstring(payload)
        except ET.ParseError:
            continue
        if root.findall(".//item"):
            return payload
    raise NewsError("ニュースを取得できませんでした。")


def clean_title(raw: str) -> str:
    text = html.unescape(raw)
    text = re.sub(r"<[^>]+>", "", text)
    return " ".join(text.split()).strip()


def parse_titles(payload: bytes, limit: int) -> list[str]:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise NewsError("ニュースを読み取れませんでした。") from exc

    titles: list[str] = []
    seen: set[str] = set()
    for item in root.findall(".//item"):
        title_element = item.find("title")
        if title_element is None or not title_element.text:
            continue
        title = clean_title(title_element.text)
        if title and title not in seen:
            seen.add(title)
            titles.append(title)
        if len(titles) >= limit:
            break
    if not titles:
        raise NewsError("ニュースの見出しがありませんでした。")
    return titles


def main() -> None:
    parser = argparse.ArgumentParser(description="NHK主要ニュースの見出しだけを短く返します。")
    parser.add_argument("--limit", type=int, default=5, help="見出し数。1から10。")
    args = parser.parse_args()
    if not 1 <= args.limit <= 10:
        print("見出し数は1から10で指定してください。")
        raise SystemExit(1)
    try:
        titles = parse_titles(fetch_feed(), args.limit)
    except NewsError as exc:
        print(str(exc))
        raise SystemExit(1) from exc

    print(f"NHK主要ニュース、{len(titles)}件です。")
    for index, title in enumerate(titles, 1):
        print(f"{index}。{title}")


if __name__ == "__main__":
    main()
