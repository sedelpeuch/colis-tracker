from __future__ import annotations

import html as html_module
import logging
import re
from email.message import Message

logger = logging.getLogger(__name__)

SENDER_WHITELIST: tuple[str, ...] = ("notif-colissimo-laposte.info", "laposte.fr")

TRACKING_CODE_RE = re.compile(
    r"(?:n°\s*(?:du\s*)?colis|num[ée]ro\s+de\s+suivi|n°\s*de\s+suivi)\s*[:\-]?\s*([0-9A-Z]{11,15})",
    re.IGNORECASE,
)

TAG_RE = re.compile(r"<[^>]+>")


def is_whitelisted_sender(from_header: str) -> bool:
    return any(domain in from_header for domain in SENDER_WHITELIST)


def extract_tracking_code(body: str) -> str | None:
    match = TRACKING_CODE_RE.search(body)
    if match is None:
        return None
    return match.group(1).upper()


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
