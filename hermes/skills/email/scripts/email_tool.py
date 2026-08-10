#!/usr/bin/env python3
"""Read a Gmail inbox without changing mailbox state."""

from __future__ import annotations

import argparse
import imaplib
import os
import re
import socket
import ssl
import sys
from contextlib import contextmanager
from email import policy
from email.header import decode_header
from email.parser import BytesParser
from email.utils import parseaddr
from typing import Iterator


HOST = "imap.gmail.com"
PORT = 993
TIMEOUT_SECONDS = 8.0
DEFAULT_LIMIT = 5
MAX_LIMIT = 20
BODY_FETCH_BYTES = 131_072
BODY_CHAR_LIMIT = 500


class MailError(Exception):
    """A safe, user-facing mail error."""


def _credentials() -> tuple[str, str] | None:
    address = os.environ.get("GMAIL_ADDRESS", "").strip()
    password = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    if not address or not password:
        return None
    return address, password


@contextmanager
def _mailbox(address: str, password: str) -> Iterator[imaplib.IMAP4_SSL]:
    client: imaplib.IMAP4_SSL | None = None
    try:
        context = ssl.create_default_context()
        client = imaplib.IMAP4_SSL(
            HOST,
            PORT,
            ssl_context=context,
            timeout=TIMEOUT_SECONDS,
        )
        if client.sock is not None:
            client.sock.settimeout(TIMEOUT_SECONDS)
        client.login(address, password)
        status, _ = client.select("INBOX", readonly=True)
        if status != "OK":
            raise MailError("受信箱を開けませんでした。")
        yield client
    except imaplib.IMAP4.error as exc:
        raise MailError("メール認証に失敗しました。") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise MailError("メールサーバーが応答しません。") from exc
    except (OSError, ssl.SSLError) as exc:
        raise MailError("メールを取得できませんでした。") from exc
    finally:
        if client is not None:
            try:
                client.logout()
            except (imaplib.IMAP4.error, OSError):
                pass


def _needs_charset(criteria: tuple[str | bytes, ...]) -> bool:
    for item in criteria:
        raw = item if isinstance(item, bytes) else item.encode("utf-8")
        if any(byte > 0x7F for byte in raw):
            return True
    return False


def _uid_search(client: imaplib.IMAP4_SSL, *criteria: str | bytes) -> list[str]:
    """Run one UID SEARCH, declaring a charset when the keys need one.

    RFC 3501 requires a SEARCH to declare its charset once any search key
    carries non-ASCII octets. A Japanese sender name therefore failed here: the
    search was issued as raw UTF-8 bytes with nothing saying so, and a
    conforming server answers BAD.

    The declaration has to be written out in full, as the two tokens
    ``"CHARSET", "UTF-8"``. ``imaplib.IMAP4.search`` takes a charset argument
    and inserts the literal ``CHARSET`` keyword itself, but ``IMAP4.uid`` has no
    such parameter — it forwards every argument to the command verbatim. Passing
    ``"UTF-8"`` alone in that position sends ``UID SEARCH UTF-8 FROM "..."``,
    where ``UTF-8`` is not a valid search key, so a conforming server answers
    BAD just as it did before. The correct line is
    ``UID SEARCH CHARSET UTF-8 FROM "..."``.

    Not every server implements SEARCH CHARSET UTF-8, so a rejection falls back
    to the undeclared form — which is what this always did, and which Gmail
    happens to accept — rather than turning a supported query into an error.
    """
    # Each entry is the token sequence placed before the search keys.
    prefixes: list[tuple[str, ...]] = (
        [("CHARSET", "UTF-8"), ()] if _needs_charset(criteria) else [()]
    )
    for attempt, prefix in enumerate(prefixes):
        last_attempt = attempt + 1 == len(prefixes)
        try:
            status, data = client.uid("search", *prefix, *criteria)
        except imaplib.IMAP4.error:
            if last_attempt:
                raise MailError("メールを検索できませんでした。") from None
            continue
        if status != "OK" or not data:
            if last_attempt:
                raise MailError("メールを検索できませんでした。")
            continue
        raw = data[0] or b""
        if not isinstance(raw, bytes):
            raise MailError("メールを検索できませんでした。")
        return [token.decode("ascii") for token in raw.split() if token.isdigit()]
    raise MailError("メールを検索できませんでした。")


def _decode_header_value(value: str | None, fallback: str) -> str:
    if not value:
        return fallback
    pieces: list[str] = []
    for part, charset in decode_header(value):
        if isinstance(part, bytes):
            encoding = charset or "utf-8"
            try:
                pieces.append(part.decode(encoding, errors="replace"))
            except LookupError:
                pieces.append(part.decode("utf-8", errors="replace"))
        else:
            pieces.append(part)
    text = "".join(pieces)
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
    return re.sub(r"\s+", " ", text).strip() or fallback


def _safe_sender(value: str | None) -> str:
    decoded = _decode_header_value(value, "送信者不明")
    name, address = parseaddr(decoded)
    display = _decode_header_value(name, "")
    if not display and address:
        display = address.split("@", 1)[0]
    if not display:
        display = "送信者不明"
    display = display.replace("@", " ")
    return re.sub(r"\s+", " ", display).strip()[:100]


def _extract_payload(data: list[object] | tuple[object, ...] | None) -> bytes:
    for item in data or []:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
            return item[1]
    raise MailError("メールの内容を取得できませんでした。")


def _header_for_uid(client: imaplib.IMAP4_SSL, uid: str) -> tuple[str, str]:
    status, data = client.uid(
        "fetch",
        uid,
        "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])",
    )
    if status != "OK":
        raise MailError("メールの概要を取得できませんでした。")
    message = BytesParser(policy=policy.default).parsebytes(_extract_payload(data))
    sender = _safe_sender(message.get("From"))
    subject = _decode_header_value(message.get("Subject"), "件名なし")[:200]
    return sender, subject


def _print_summaries(
    client: imaplib.IMAP4_SSL,
    uids: list[str],
    limit: int,
    empty_message: str,
) -> None:
    selected = list(reversed(uids[-limit:]))
    if not selected:
        print(empty_message)
        return
    for index, uid in enumerate(selected, start=1):
        sender, subject = _header_for_uid(client, uid)
        print(f"{index}. {sender}さん「{subject}」（参照ID: {uid}）")


def _sender_criterion(sender: str) -> bytes:
    if not sender.strip() or any(char in sender for char in "\r\n\x00"):
        raise MailError("送信者名を指定してください。")
    escaped = sender.strip().replace("\\", "\\\\").replace('"', '\\"')
    return f'FROM "{escaped}"'.encode("utf-8")


def _plain_text(message_bytes: bytes) -> str:
    message = BytesParser(policy=policy.default).parsebytes(message_bytes)
    candidates = message.walk() if message.is_multipart() else [message]
    for part in candidates:
        if part.get_content_type() != "text/plain":
            continue
        if part.get_content_disposition() == "attachment":
            continue
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            content = part.get_payload()
            if not isinstance(content, str):
                continue
            text = content
        else:
            charset = part.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except LookupError:
                text = payload.decode("utf-8", errors="replace")
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            suffix = "…" if len(text) > BODY_CHAR_LIMIT else ""
            return text[:BODY_CHAR_LIMIT].rstrip() + suffix
    return ""


def _body_for_uid(client: imaplib.IMAP4_SSL, uid: str) -> str:
    status, data = client.uid(
        "fetch",
        uid,
        f"(BODY.PEEK[]<0.{BODY_FETCH_BYTES}>)",
    )
    if status != "OK":
        raise MailError("メール本文を取得できませんでした。")
    return _plain_text(_extract_payload(data))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Gmailを読み取り専用で確認します。",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("unread", help="未読件数を表示")

    recent = commands.add_parser("recent", help="最近のメール概要を表示")
    recent.add_argument("--limit", type=int, default=DEFAULT_LIMIT)

    sender = commands.add_parser("from-sender", help="指定した送信者のメールを表示")
    sender.add_argument("sender")
    sender.add_argument("--limit", type=int, default=DEFAULT_LIMIT)

    briefing = commands.add_parser("briefing", help="朝の短い未読メール概要")
    briefing.add_argument("--limit", type=int, default=2)

    body = commands.add_parser("body", help="明示的に依頼された本文を表示")
    body.add_argument("uid", help="recent/from-senderで返された参照ID")
    body.add_argument("--explicit", action="store_true", help="明示的な本文依頼を確認")
    return parser


def _validate_limit(limit: int) -> int:
    if not 1 <= limit <= MAX_LIMIT:
        raise MailError(f"件数は1から{MAX_LIMIT}で指定してください。")
    return limit


def _run(args: argparse.Namespace, client: imaplib.IMAP4_SSL) -> None:
    if args.command == "unread":
        count = len(_uid_search(client, "UNSEEN"))
        print(f"未読メールは{count}件です。")
        return

    if args.command == "recent":
        _print_summaries(client, _uid_search(client, "ALL"), _validate_limit(args.limit), "メールはありません。")
        return

    if args.command == "from-sender":
        uids = _uid_search(client, _sender_criterion(args.sender))
        _print_summaries(client, uids, _validate_limit(args.limit), "その送信者からのメールは見つかりませんでした。")
        return

    if args.command == "briefing":
        uids = _uid_search(client, "UNSEEN")
        count = len(uids)
        if not uids:
            print("未読メールはありません。")
            return
        summaries: list[str] = []
        for uid in reversed(uids[-_validate_limit(args.limit):]):
            sender, subject = _header_for_uid(client, uid)
            summaries.append(f"{sender}さんから「{subject}」")
        print(f"未読は{count}件です。" + "、".join(summaries) + "などがあります。")
        return

    if args.command == "body":
        if not args.explicit:
            raise MailError("本文を読むには、特定のメールについて明示的な依頼が必要です。")
        if not args.uid.isdigit():
            raise MailError("メールの参照IDを指定してください。")
        text = _body_for_uid(client, args.uid)
        print(text or "このメールには読み上げられる本文がありません。")
        return

    raise MailError("メール操作を指定してください。")


def main() -> int:
    args = _parser().parse_args()
    credentials = _credentials()
    if credentials is None:
        print("メール連携は未設定です。")
        return 0
    try:
        with _mailbox(*credentials) as client:
            _run(args, client)
        return 0
    except MailError as exc:
        print(str(exc))
        return 2


if __name__ == "__main__":
    sys.exit(main())
