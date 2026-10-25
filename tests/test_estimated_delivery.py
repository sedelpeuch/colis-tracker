import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx

from app import db, notify, poller
from app.dates import eta_label, eta_readout, french_date
from app.laposte_client import ParcelSnapshot, ParcelStatus, fetch_parcel


def _shipment(is_final: bool, code: str) -> dict:
    # Extrait réduit d'une vraie réponse La Poste (colis Colissimo, 2026-10-25).
    return {
        "product": "colissimo",
        "isFinal": is_final,
        "estimDate": "2026-10-27T01:00:00+01:00",
        "deliveryDate": "2026-10-27T17:37:00+01:00" if is_final else None,
        "currentState": {"code": code, "shortLabel": "..."},
        "contextData": {"merchantName": "Amazon"},
        "event": [],
    }


def _fetch(shipment: dict):
    def handler(request):
        return httpx.Response(200, json=[{"returnCode": 200, "shipment": shipment}])

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await fetch_parcel(client, "6Z00548212842")

    return asyncio.run(run())


def test_estimated_delivery_read_while_in_transit():
    snapshot = _fetch(_shipment(False, "ACHERI"))
    assert snapshot.estimated_delivery == "2026-10-27T01:00:00+01:00"


def test_estimated_delivery_dropped_once_delivered():
    snapshot = _fetch(_shipment(True, "DESLIVHD"))
    assert snapshot.delivered
    assert snapshot.estimated_delivery is None
    assert snapshot.delivered_at == "2026-10-27T17:37:00+01:00"


def test_french_date():
    assert french_date("2026-10-27T01:00:00+01:00") == "mardi 27 octobre"
    assert french_date(None) == ""


def test_eta_label_relative_days():
    paris = timezone(timedelta(hours=1))
    eta = "2026-10-27T01:00:00+01:00"
    assert eta_label(eta, now=datetime(2026, 10, 27, 8, tzinfo=paris)) == "Aujourd'hui · mardi 27 octobre"
    # 23h30 UTC le 25 = déjà le 26 à Paris : la veille de la livraison.
    assert eta_label(eta, now=datetime(2026, 10, 25, 23, 30, tzinfo=timezone.utc)) == "Demain · mardi 27 octobre"
    assert eta_label(eta, now=datetime(2026, 10, 20, tzinfo=paris)) == "Dans 7 jours · mardi 27 octobre"


def test_eta_readout_tone_follows_voyants():
    paris = timezone(timedelta(hours=1))
    eta = "2026-10-27T01:00:00+01:00"
    assert eta_readout(eta, now=datetime(2026, 10, 25, 12, tzinfo=paris)) == {
        "countdown": "Dans 2 jours", "date": "mardi 27 octobre", "tone": None,
    }
    assert eta_readout(eta, now=datetime(2026, 10, 27, 12, tzinfo=paris))["tone"] == "warn"
    late = eta_readout(eta, now=datetime(2026, 10, 28, 12, tzinfo=paris))
    assert (late["countdown"], late["tone"]) == ("En retard", "alarm")
    assert eta_readout(None) is None


def test_init_db_adds_column_to_existing_db(tmp_path, monkeypatch):
    path = tmp_path / "colis.db"
    old_schema = db.SCHEMA.replace("    estimated_delivery TEXT,\n", "")  # base créée avant la colonne
    with sqlite3.connect(path) as conn:
        conn.executescript(old_schema)
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    db.init_db()  # idempotent
    with db.get_conn() as conn:
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(packages)")}
    assert "estimated_delivery" in columns


def test_init_db_backfills_delivered_at(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "colis.db")
    db.init_db()
    with db.get_conn() as conn:
        conn.execute("INSERT INTO packages (tracking_code, status, delivered) VALUES ('6A1', 'delivered', 1)")
        conn.execute(
            "INSERT INTO events (package_id, status, created_at) VALUES "
            "(1, 'in_transit', '2026-10-22T10:00:00+02:00'), (1, 'delivered', '2026-10-23T17:37:00+02:00')"
        )
    db.init_db()
    with db.get_conn() as conn:
        assert conn.execute("SELECT delivered_at FROM packages").fetchone()[0] == "2026-10-23T17:37:00+02:00"


def _poll_with(monkeypatch, tmp_path, *, status, eta, stored_eta):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "colis.db")
    db.init_db()
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO packages (tracking_code, label, status, estimated_delivery) VALUES ('6A1', 'Livres', 'in_transit', ?)",
            (stored_eta,),
        )
        row = conn.execute("SELECT * FROM packages").fetchone()

    async def fake_fetch(client, code):
        return ParcelSnapshot(
            carrier="Colissimo", status=status, raw_status="En route", sender=None, delivered=False,
            delivered_at=None, url="", history=[], estimated_delivery=eta,
        )

    sent = []

    async def fake_notify(title, message, **kwargs):
        sent.append((title, message))

    monkeypatch.setattr(poller, "fetch_parcel", fake_fetch)
    monkeypatch.setattr(poller, "notify", fake_notify)
    asyncio.run(poller._poll_one(None, row))
    return sent


def test_notification_on_status_change_includes_eta(monkeypatch, tmp_path):
    sent = _poll_with(monkeypatch, tmp_path, status=ParcelStatus.OUT_FOR_DELIVERY,
                      eta="2099-01-01T01:00:00+01:00", stored_eta="2099-01-01T01:00:00+01:00")
    assert sent == [("Colis Livres", f"En route\nLivraison prévue : {eta_label('2099-01-01T01:00:00+01:00')}")]


def test_notification_when_only_eta_changes(monkeypatch, tmp_path):
    sent = _poll_with(monkeypatch, tmp_path, status=ParcelStatus.IN_TRANSIT,
                      eta="2099-01-02T01:00:00+01:00", stored_eta="2099-01-01T01:00:00+01:00")
    assert sent == [("Colis Livres", f"Livraison prévue : {eta_label('2099-01-02T01:00:00+01:00')}")]


def test_no_notification_when_nothing_changes(monkeypatch, tmp_path):
    sent = _poll_with(monkeypatch, tmp_path, status=ParcelStatus.IN_TRANSIT,
                      eta="2099-01-01T01:00:00+01:00", stored_eta="2099-01-01T01:00:00+01:00")
    assert sent == []


def test_ntfy_accepts_accents(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(notify, "NTFY_URL", "http://ntfy")
    monkeypatch.setattr(notify, "NTFY_TOPIC", "colis")
    monkeypatch.setattr(notify.httpx, "AsyncClient", lambda: real_client(transport=httpx.MockTransport(handler)))
    asyncio.run(notify.notify("Colis déjà retiré", "Livraison prévue", attachment=b"png", attachment_name="6A1.png"))
    assert requests[0].url.params["title"] == "Colis déjà retiré"
    assert requests[0].url.params["message"] == "Livraison prévue"
