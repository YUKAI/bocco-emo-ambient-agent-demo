#!/usr/bin/env python3
"""Print current NHK headlines as one bounded, speech-ready utterance."""

from __future__ import annotations

import argparse
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET


NEWS_RSS_URL = "https://www.nhk.or.jp/rss/news/cat0.xml"
MAX_SPEECH_CHARS = 190


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", default="")
    parser.parse_args()
    request = Request(
        NEWS_RSS_URL, headers={"User-Agent": "bocco-bridge-fast-route/1"}
    )
    with urlopen(request, timeout=7) as response:
        body = response.read(262_145)
    if len(body) > 262_144:
        return 2
    root = ET.fromstring(body)
    headlines = []
    for item in root.findall("./channel/item"):
        title = " ".join((item.findtext("title") or "").split())
        if title:
            headlines.append(title[:160])
        if len(headlines) == 3:
            break
    if not headlines:
        return 2

    # Compose a speech-ready utterance that stays inside the bridge's
    # speech limit; add headlines only while they still fit.
    utterance = "NHKニュースの主な見出しです。"
    for index, headline in enumerate(headlines, start=1):
        sentence = f"{index}つ目、{headline}。"
        if index > 1 and len(utterance) + len(sentence) > MAX_SPEECH_CHARS:
            break
        utterance += sentence
    print(utterance)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
