from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime, timedelta

import httpx

from . import db
from .laposte_client import LaPosteApiError, ParcelStatus, fetch_parcel

logger = logging.getLogger(__name__)

HOT_INTERVAL = timedelta(minutes=15)
MID_INTERVAL = timedelta(minutes=45)
TICK_SECONDS = 60
QUIET_START_HOUR = 0
QUIET_END_HOUR = 6

NTFY_URL = os.environ.get("NTFY_URL")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")


def _in_quiet_window(now: datetime) -> bool:
    return QUIET_START_HOUR <= now.hour < QUIET_END_HOUR


async def _notify(title: str, message: str) -> None:
    if not NTFY_URL or not NTFY_TOPIC:
        return
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{NTFY_URL.rstrip('/')}/{NTFY_TOPIC}",
                content=message.encode("utf-8"),
                headers={"Title": title},
                timeout=10.0,
            )
    except httpx.HTTPError:
        logger.warning("ntfy notification failed", exc_info=True)


async def _poll_one(client: httpx.AsyncClient, row) -> None:
    now = datetime.now(UTC)
    try:
        snapshot = await fetch_parcel(client, row["tracking_code"])
    except LaPosteApiError as err:
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE packages SET error = ?, last_polled_at = ?, next_poll_at = ? WHERE id = ?",
                (str(err), now.isoformat(), (now + MID_INTERVAL).isoformat(), row["id"]),
            )
        return

    if snapshot is None:
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE packages SET last_polled_at = ?, next_poll_at = ?, error = NULL WHERE id = ?",
                (now.isoformat(), (now + MID_INTERVAL).isoformat(), row["id"]),
            )
        return

    status_changed = snapshot.status.value != row["status"]
    interval = HOT_INTERVAL if snapshot.status == ParcelStatus.OUT_FOR_DELIVERY else MID_INTERVAL
    if snapshot.delivered:
        interval = timedelta(days=3650)  # colis livré : on arrête de le poller activement

    with db.get_conn() as conn:
        conn.execute(
            """
            UPDATE packages
            SET carrier = ?, status = ?, raw_status = ?, sender = COALESCE(?, sender),
                delivered = ?, delivered_at = ?, entry_date = COALESCE(?, entry_date),
                locomotion_mode = COALESCE(?, locomotion_mode), url = ?, error = NULL,
                updated_at = ?, last_polled_at = ?, next_poll_at = ?
            WHERE id = ?
            """,
            (
                snapshot.carrier, snapshot.status.value, snapshot.raw_status, snapshot.sender,
                int(snapshot.delivered), snapshot.delivered_at, snapshot.entry_date,
                snapshot.locomotion_mode, snapshot.url,
                now.isoformat(), now.isoformat(), (now + interval).isoformat(),
                row["id"],
            ),
        )
        # La Poste renvoie tout l'historique du colis à chaque appel (pas
        # seulement le dernier événement) : on resynchronise la table events
        # en entier plutôt que de ne logger que les transitions vues par notre
        # propre polling, sinon un colis déjà livré à l'ajout n'a qu'une ligne.
        existing = {
            r["created_at"] for r in conn.execute(
                "SELECT created_at FROM events WHERE package_id = ?", (row["id"],)
            ).fetchall()
        }
        for entry in snapshot.history:
            if entry.timestamp in existing:
                continue
            conn.execute(
                "INSERT INTO events (package_id, status, raw_status, created_at) VALUES (?, ?, ?, ?)",
                (row["id"], entry.status.value, entry.raw_status, entry.timestamp),
            )

    if status_changed:
        label = row["label"] or row["tracking_code"]
        await _notify(f"Colis {label}", snapshot.raw_status or snapshot.status.value)


async def poll_due_packages() -> None:
    now = datetime.now(UTC)
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM packages WHERE delivered = 0 AND next_poll_at <= ?",
            (now.isoformat(),),
        ).fetchall()
    if not rows:
        return
    async with httpx.AsyncClient() as client:
        for row in rows:
            await _poll_one(client, row)


async def poller_loop() -> None:
    logger.info("poller started")
    while True:
        now = datetime.now(UTC)
        if not _in_quiet_window(now):
            try:
                await poll_due_packages()
            except Exception:
                logger.exception("poll cycle failed")
        await asyncio.sleep(TICK_SECONDS)
