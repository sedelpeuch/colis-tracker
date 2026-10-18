"""Opérations sur les colis partagées par l'interface web et le serveur MCP."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from . import db
from .laposte_client import fetch_parcel
from .poller import poll_due_packages

POSTMARK_LABELS = {
    "registered": "Enregistré",
    "in_transit": "En transit",
    "out_for_delivery": "En livraison",
    "at_pickup_point": "En point relais",
    "delivered": "Livré",
    "returning": "Retour",
    "problem": "Anomalie",
    "unknown": "Inconnu",
}


async def register_package(tracking_code: str, label: str) -> None:
    """Ajoute un colis (sans effet s'il est déjà suivi) puis relève son statut aussitôt."""
    now = datetime.now(UTC).isoformat()
    with db.get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO packages (tracking_code, label, created_at, next_poll_at) VALUES (?, ?, ?, ?)",
            (tracking_code, label.strip() or None, now, now),
        )

    # Poll immédiat pour un premier retour sans attendre le prochain tick.
    async with httpx.AsyncClient() as client:
        try:
            await fetch_parcel(client, tracking_code)
        except Exception:
            pass
    await poll_due_packages()
