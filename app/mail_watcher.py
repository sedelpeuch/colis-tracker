from __future__ import annotations

import asyncio
import email
import html as html_module
import imaplib
import logging
import os
import re
from datetime import UTC, datetime
from email.message import Message

from . import db
from .notify import notify
from .poller import poll_due_packages

logger = logging.getLogger(__name__)

SENDER_WHITELIST: tuple[str, ...] = ("notif-colissimo-laposte.info", "laposte.fr")

IMAP_HOST = os.environ.get("IMAP_HOST")
IMAP_PORT = int(os.environ.get("IMAP_PORT", "993"))
IMAP_USER = os.environ.get("IMAP_USER")
IMAP_PASSWORD = os.environ.get("IMAP_PASSWORD")
IMAP_FOLDER = os.environ.get("IMAP_FOLDER", "INBOX")
MAIL_PROCESSED_LABEL = os.environ.get("MAIL_PROCESSED_LABEL", "colis-tracker/traite")
WATCH_INTERVAL_MINUTES = int(os.environ.get("MAIL_WATCH_INTERVAL_MINUTES", "5"))

TRACKING_LABEL_RE = re.compile(
    r"(?:n°\s*(?:du\s*)?colis|num[ée]ro\s+de\s+suivi|n°\s*de\s+suivi)\s*[:\-]?\s*([0-9A-Z]{11,15})",
    re.IGNORECASE,
)
# Fallback quand le mail ne précède pas le code d'un libellé reconnu (ex.
# "Votre colis  6Z00534769671  est disponible..."). Forme observée des
# numéros Colissimo domestiques : 1 chiffre + 1 lettre + 11 chiffres.
TRACKING_SHAPE_RE = re.compile(r"\b\d[A-Z]\d{11}\b")

TAG_RE = re.compile(r"<[^>]+>")


def is_whitelisted_sender(from_header: str) -> bool:
    return any(domain in from_header for domain in SENDER_WHITELIST)


def extract_tracking_code(body: str) -> str | None:
    match = TRACKING_LABEL_RE.search(body)
    if match is not None:
        return match.group(1).upper()
    match = TRACKING_SHAPE_RE.search(body)
    if match is not None:
        return match.group(0).upper()
    return None


def _decode_part(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if not isinstance(payload, bytes):
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError):
        return payload.decode("utf-8", errors="replace")


def decode_body(msg: Message) -> str:
    plain_parts: list[str] = []
    html_parts: list[str] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        content_type = part.get_content_type()
        if content_type == "text/plain":
            plain_parts.append(_decode_part(part))
        elif content_type == "text/html":
            html_parts.append(_decode_part(part))
    if plain_parts:
        return "\n".join(plain_parts)
    stripped = [html_module.unescape(TAG_RE.sub(" ", part)) for part in html_parts]
    return "\n".join(stripped)


def build_from_search_criteria(domains: tuple[str, ...]) -> str:
    if len(domains) == 1:
        return f'FROM "{domains[0]}"'
    return f'OR {build_from_search_criteria(domains[:1])} {build_from_search_criteria(domains[1:])}'


def _connect() -> imaplib.IMAP4:
    if not IMAP_HOST or not IMAP_USER or not IMAP_PASSWORD:
        raise RuntimeError("IMAP_HOST/IMAP_USER/IMAP_PASSWORD must be set")
    imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    imap.login(IMAP_USER, IMAP_PASSWORD)
    return imap


def _ensure_processed_folder(imap: imaplib.IMAP4) -> None:
    typ, _ = imap.select(MAIL_PROCESSED_LABEL)
    if typ != "OK":
        imap.create(MAIL_PROCESSED_LABEL)


def _create_package(tracking_code: str) -> bool:
    now = datetime.now(UTC).isoformat()
    with db.get_conn() as conn:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO packages (tracking_code, created_at, next_poll_at) VALUES (?, ?, ?)",
            (tracking_code, now, now),
        )
        return cursor.rowcount > 0


def _process_one(imap: imaplib.IMAP4, msg_id: str) -> bool:
    typ, msg_data = imap.fetch(msg_id, "(RFC822)")
    if not msg_data or not isinstance(msg_data[0], tuple):
        return False
    raw = msg_data[0][1]
    if not isinstance(raw, bytes):
        return False
    msg = email.message_from_bytes(raw)

    from_header = msg.get("From", "")
    if not is_whitelisted_sender(from_header):
        # La recherche IMAP (SEARCH FROM) est un matching flou côté Gmail : elle
        # peut remonter des mails d'autres expéditeurs (le terme apparaît dans le
        # corps, pas l'en-tête). On revérifie l'en-tête From strictement ici, et
        # on ne touche pas au mail si ça ne matche pas : ni déplacement, ni
        # suppression du label d'origine.
        return False

    body = decode_body(msg)
    tracking_code = extract_tracking_code(body)
    created = _create_package(tracking_code) if tracking_code is not None else False

    imap.copy(msg_id, MAIL_PROCESSED_LABEL)
    imap.store(msg_id, "+FLAGS", "\\Deleted")
    return created


def scan_once() -> int:
    imap = _connect()
    created_count = 0
    try:
        _ensure_processed_folder(imap)
        imap.select(IMAP_FOLDER)
        typ, data = imap.search(None, build_from_search_criteria(SENDER_WHITELIST))
        msg_ids = [m.decode() for m in data[0].split()] if data and data[0] else []
        for msg_id in msg_ids:
            try:
                if _process_one(imap, msg_id):
                    created_count += 1
            except Exception:
                logger.exception("failed to process mail %r", msg_id)
        imap.expunge()
    finally:
        imap.logout()
    return created_count


async def mail_watcher_loop() -> None:
    if not IMAP_HOST:
        logger.info("mail watcher disabled (IMAP_HOST not set)")
        return
    logger.info("mail watcher started")
    while True:
        try:
            created = await asyncio.to_thread(scan_once)
            if created:
                logger.info("mail watcher: %d nouveau(x) colis importé(s)", created)
                await poll_due_packages()
        except Exception as err:
            logger.exception("mail scan cycle failed")
            await notify("Colis Tracker — import mail", str(err))
        await asyncio.sleep(WATCH_INTERVAL_MINUTES * 60)
