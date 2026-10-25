from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

NTFY_URL = os.environ.get("NTFY_URL")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")


async def notify(
    title: str, message: str, *, attachment: bytes | None = None, attachment_name: str | None = None
) -> None:
    if not NTFY_URL or not NTFY_TOPIC:
        return
    # Titre et message en paramètres d'URL plutôt qu'en en-têtes : httpx refuse les
    # en-têtes non ASCII, or les libellés La Poste et les noms de colis ont des accents.
    params = {"title": title}
    headers = {}
    content = message.encode("utf-8")
    if attachment is not None:
        params["message"] = message
        headers["Filename"] = attachment_name or "barcode.png"
        content = attachment
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{NTFY_URL.rstrip('/')}/{NTFY_TOPIC}",
                content=content,
                params=params,
                headers=headers,
                timeout=10.0,
            )
    except httpx.HTTPError:
        logger.warning("ntfy notification failed", exc_info=True)
