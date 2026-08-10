---
name: news
description: "Short Japanese headlines from the official NHK RSS feed."
version: 1.0.0
author: BOCCO Ambient Robot
license: Apache-2.0
dependencies: []
platforms: [linux]
metadata:
  hermes:
    tags: [news, headlines, nhk, rss, japan, japanese]
    category: home
    requires_toolsets: [terminal]
---

# News Skill

Fetch a small list of current headlines from NHK's official general-news RSS
feed. Only titles are returned; the skill does not scrape article pages.

## When to Use

- The user asks for today's or the latest news.
- The user wants a short Japanese news briefing.
- The user asks for NHK headlines.

## Prerequisites

Python 3.11+ using only the standard library. No API key is required.

Script path: `~/.hermes/skills/news/scripts/news.py`

## Usage

```bash
NEWS=~/.hermes/skills/news/scripts/news.py

# Five headlines (default)
python3 "$NEWS"

# A shorter briefing
python3 "$NEWS" --limit 3
```

`--limit` accepts an integer from 1 through 10.

## Output

The first line says how many headlines follow. Each remaining line contains a
number and one title. For speech, read the titles in order and stop at the
requested limit. Do not add article details that the feed did not provide.

## Data Source and Network Behavior

- Primary feed: `https://www3.nhk.or.jp/rss/news/cat0.xml`
- Current official endpoint fallback:
  `https://news.web.nhk/n-data/conf/na/rss/cat0.xml`
- Each request has an 8-second timeout and a 1 MB response limit.
- The script parses RSS XML locally and never opens article links.

## Errors

Errors are short Japanese sentences and return a non-zero status. If NHK is
temporarily unavailable, tell the user that headlines could not be retrieved;
do not make up news.

## Verification

```bash
python3 "$NEWS" --limit 3
python3 "$NEWS" --help
```
