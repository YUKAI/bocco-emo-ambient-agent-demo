---
name: email
description: "Read-only Gmail checks for unread counts, concise message summaries, sender searches, and explicitly requested plain-text bodies."
version: 1.0.0
author: BOCCO Ambient Robot
license: Apache-2.0
dependencies: []
platforms: [linux]
metadata:
  hermes:
    tags: [email, gmail, imap, unread, inbox, morning-briefing]
    category: home
    requires_toolsets: [terminal]
---

# Email Skill

Check a Gmail inbox over read-only IMAP. Summaries contain only sender names and
subjects. Message bodies are available only after the user explicitly asks for
a specific message.

## When to Use

- The user asks how many unread emails they have.
- The user asks for recent emails or messages from a named sender.
- The morning briefing needs one short inbox line.
- The user explicitly asks to read a specific message returned by this skill.

## Prerequisites

Python 3.11+ using only the standard library. Configure both variables in the
Hermes service environment during a separate credential-wiring task:

- `GMAIL_ADDRESS`: Gmail address used to sign in.
- `GMAIL_APP_PASSWORD`: Google app password; never a normal account password.

If either variable is absent, the script says `メール連携は未設定です。` and exits
successfully. Never ask the user to say a password aloud and never print either
credential.

Script path: `~/.hermes/skills/email/scripts/email_tool.py`

## Commands

```bash
EMAIL=~/.hermes/skills/email/scripts/email_tool.py

# Unread count
python3 "$EMAIL" unread

# Most recent messages; default 5, maximum 20
python3 "$EMAIL" recent
python3 "$EMAIL" recent --limit 3

# Most recent messages from a named sender
python3 "$EMAIL" from-sender "山田"
python3 "$EMAIL" from-sender "Alice" --limit 3

# One short morning-briefing line using recent unread messages
python3 "$EMAIL" briefing
python3 "$EMAIL" briefing --limit 2

# Body retrieval: only after an explicit request for the specific reference ID
python3 "$EMAIL" body 12345 --explicit
```

`recent` and `from-sender` include a numeric reference ID for a later `body`
command. Treat the ID as control metadata; never speak it unless clarification
is required.

## Speech and Privacy Rules

- Summarize senders and subjects concisely. Do not invent message details.
- Never read a full email address. Speak only the decoded display name; when no
  display name exists, the script returns only the local name before `@`.
- Do not retrieve bodies for unread counts, recent lists, sender searches, or
  morning briefings.
- Run `body ID --explicit` only when the user clearly asks to hear that specific
  message. Never infer permission from a general inbox request.
- For morning briefings, produce one short line such as: 「未読は3件です。山田
  さんから『明日の予定』などがあります。」
- The script returns only a plain-text MIME part, normalizes it for speech, and
  truncates it to about 500 characters. Do not request or reconstruct HTML or
  attachments.

## Read-Only and Network Behavior

- Server: `imap.gmail.com:993` using TLS.
- Connection and socket operations use an 8-second timeout.
- The mailbox is opened read-only and every fetch uses `BODY.PEEK`.
- The skill never marks mail read and never deletes, moves, labels, or sends
  messages. It writes no files.

## Errors

Errors are short Japanese sentences. Missing credentials return status 0 so an
unconfigured morning briefing degrades gracefully. Authentication, connection,
and invalid-request failures return a non-zero status. Do not expose raw server
errors to the user.

## Verification

```bash
env -u GMAIL_ADDRESS -u GMAIL_APP_PASSWORD python3 "$EMAIL" unread
python3 "$EMAIL" --help
```
