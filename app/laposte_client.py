"""Client pour l'endpoint public (non officiel) de suivi La Poste.

Principe et constantes repris de l'intégration Home Assistant
`ha-parcel-integrations/ha-laposte` (custom_components/laposte/api.py,
const.py, parcels.py) : endpoint consommateur du site laposte.fr, pas
l'API "Suivi v2" officielle. Aucune clé/auth, mais un User-Agent usurpé
est nécessaire pour ne pas se prendre un 403.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

import httpx

logger = logging.getLogger(__name__)

TRACKING_API_URL = "https://www.laposte.fr/ssu/sun/back/suivi-unifie/{tracking_code}?lang=fr"
TRACKING_URL = "https://www.laposte.fr/outils/suivre-vos-envois?code={tracking_code}"
USER_AGENT = "PostmanRuntime/7.49.1"


class ParcelStatus(StrEnum):
    REGISTERED = "registered"
    IN_TRANSIT = "in_transit"
    OUT_FOR_DELIVERY = "out_for_delivery"
    AT_PICKUP_POINT = "at_pickup_point"
    DELIVERED = "delivered"
    RETURNING = "returning"
    PROBLEM = "problem"
    UNKNOWN = "unknown"


_COLISSIMO_MAP: dict[str, ParcelStatus] = {
    "EXPANN": ParcelStatus.REGISTERED,
    "EDRDEP": ParcelStatus.IN_TRANSIT, "EDRINT": ParcelStatus.IN_TRANSIT,
    "ACHNAT": ParcelStatus.IN_TRANSIT, "ACHORI": ParcelStatus.IN_TRANSIT,
    "ACHERI": ParcelStatus.IN_TRANSIT, "ACHDOU": ParcelStatus.IN_TRANSIT,
    "DISINTAS": ParcelStatus.IN_TRANSIT, "DISARR": ParcelStatus.IN_TRANSIT,
    "DISTOU": ParcelStatus.IN_TRANSIT, "DISIRST": ParcelStatus.IN_TRANSIT,
    "AARIDOU": ParcelStatus.IN_TRANSIT, "AARENDDOU": ParcelStatus.IN_TRANSIT,
    "AARIREF": ParcelStatus.IN_TRANSIT,
    "ACHIECA": ParcelStatus.PROBLEM, "AARAREF": ParcelStatus.PROBLEM,
    "AARABECH": ParcelStatus.PROBLEM, "DISIECHEC": ParcelStatus.PROBLEM,
    "DESLIVD": ParcelStatus.DELIVERED, "DESOBS": ParcelStatus.DELIVERED,
    "DESLIVHD": ParcelStatus.DELIVERED,  # retrait en point relais/casier (confirmé en test réel)
    "DESMDREXP": ParcelStatus.RETURNING,  # à dispo du vendeur suite retour (confirmé en test réel)
    "DISINS": ParcelStatus.AT_PICKUP_POINT,  # colis dispo en point de retrait, pas encore retiré
}
_CHRONOPOST_MAP: dict[str, ParcelStatus] = {
    "PC1": ParcelStatus.IN_TRANSIT, "ET1": ParcelStatus.IN_TRANSIT,
    "EP1": ParcelStatus.IN_TRANSIT, "MD2": ParcelStatus.IN_TRANSIT,
    "DR1": ParcelStatus.IN_TRANSIT, "MD1": ParcelStatus.OUT_FOR_DELIVERY,
    "AG1": ParcelStatus.AT_PICKUP_POINT, "RE1": ParcelStatus.RETURNING,
    "DI1": ParcelStatus.DELIVERED,
}


def _status_map(product: str | None) -> dict[str, ParcelStatus]:
    if product == "colissimo":
        return _COLISSIMO_MAP
    if product == "chronopost":
        return _CHRONOPOST_MAP
    return {}


def map_status(code: str | None, product: str | None) -> ParcelStatus:
    if not code:
        return ParcelStatus.UNKNOWN
    return _status_map(product).get(code, ParcelStatus.UNKNOWN)


def carrier_name(product: str | None) -> str:
    return {"colissimo": "Colissimo", "chronopost": "Chronopost"}.get(
        (product or "").lower(), "La Poste"
    )


def _newest_event(events: list[dict]) -> dict:
    def _key(event: dict) -> str:
        return str(event.get("date") or "")

    return max(events, key=_key, default={})


@dataclass
class HistoryEntry:
    timestamp: str  # ISO 8601
    status: ParcelStatus
    raw_status: str | None


@dataclass
class ParcelSnapshot:
    carrier: str
    status: ParcelStatus
    raw_status: str | None
    sender: str | None
    delivered: bool
    delivered_at: str | None
    url: str
    history: list[HistoryEntry]
    entry_date: str | None = None
    locomotion_mode: str | None = None


class LaPosteApiError(Exception):
    def __init__(self, detail: str, *, status_code: int | None = None, retry_after: float | None = None):
        super().__init__(f"La Poste API request failed: {detail}")
        self.status_code = status_code
        self.retry_after = retry_after


async def fetch_parcel(client: httpx.AsyncClient, tracking_code: str) -> ParcelSnapshot | None:
    """Interroge l'endpoit public La Poste pour un numéro de suivi.

    Retourne ``None`` quand La Poste ne reconnaît pas encore le colis (état
    normal, pas une erreur). Lève ``LaPosteApiError`` pour tout le reste.
    """
    url = TRACKING_API_URL.format(tracking_code=tracking_code)
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    response = await client.get(url, headers=headers, timeout=15.0)

    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After")
        try:
            retry_after_s = float(retry_after) if retry_after else None
        except ValueError:
            retry_after_s = None
        raise LaPosteApiError("HTTP 429", status_code=429, retry_after=retry_after_s)
    if response.status_code != 200:
        raise LaPosteApiError(f"HTTP {response.status_code}", status_code=response.status_code)

    try:
        payload = response.json()
    except ValueError as err:
        raise LaPosteApiError(f"unparseable body ({err})") from err

    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        return None

    envelope = payload[0]
    if envelope.get("returnCode") != 200:
        return None

    shipment = envelope.get("shipment")
    if not isinstance(shipment, dict):
        raise LaPosteApiError("success returnCode without shipment")

    product = str(shipment.get("product") or "").lower()
    events = [e for e in shipment.get("event") or [] if isinstance(e, dict)]
    newest = _newest_event(events)
    current_state = shipment.get("currentState") or {}

    if product == "colissimo":
        status_code = current_state.get("code") or newest.get("group")
    else:
        codes = [e.get("code") for e in events]
        status_code = "RE1" if "RE1" in codes and "DI1" not in codes else newest.get("code")

    status = map_status(status_code, product)
    delivered = status is ParcelStatus.DELIVERED
    context = shipment.get("contextData") or {}
    resolved_code = envelope.get("inputIdShip") or tracking_code

    history: list[HistoryEntry] = []
    for event in sorted(events, key=lambda e: str(e.get("date") or "")):
        event_code = event.get("group") if product == "colissimo" else event.get("code")
        history.append(
            HistoryEntry(
                timestamp=str(event.get("date")),
                status=map_status(event_code, product),
                raw_status=event.get("label"),
            )
        )

    return ParcelSnapshot(
        carrier=carrier_name(product),
        status=status,
        raw_status=current_state.get("shortLabel") or newest.get("label") or status_code,
        sender=context.get("merchantName"),
        delivered=delivered,
        delivered_at=_epoch_ms_to_iso(shipment.get("deliveryDate")) if delivered else None,
        url=TRACKING_URL.format(tracking_code=resolved_code),
        history=history,
        entry_date=shipment.get("entryDate"),
        locomotion_mode=context.get("locomotionMode"),
    )


def _epoch_ms_to_iso(value: Any) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None
