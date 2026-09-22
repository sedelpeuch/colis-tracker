import email
from unittest.mock import MagicMock, patch

import pytest

from app import db, mail_watcher
from app.mail_watcher import (
    build_from_search_criteria,
    decode_body,
    extract_tracking_code,
    is_whitelisted_sender,
)


def test_is_whitelisted_sender_matches_known_domain():
    assert is_whitelisted_sender("La Poste-Colissimo <noreply@notif-colissimo-laposte.info>")


def test_is_whitelisted_sender_matches_laposte_fr():
    assert is_whitelisted_sender("Service Client <contact@laposte.fr>")


def test_is_whitelisted_sender_rejects_unknown_domain():
    assert not is_whitelisted_sender("Amazon <ship-confirm@amazon.fr>")


def test_is_whitelisted_sender_rejects_empty():
    assert not is_whitelisted_sender("")


COLISSIMO_MAIL_BODY = """
Votre colis arrive !
 En transit

N° du colis 6Z00547709541

Votre colis Amazon est en transit.

    Il sera livré
vendredi 18 septembre
Suivre mon colis
    Votre colis sera déposé en point de retrait

Adresse de livraison
TALENCE
262 COURS GAMBETTA
33400 TALENCE
"""


def test_extract_tracking_code_from_real_colissimo_mail():
    assert extract_tracking_code(COLISSIMO_MAIL_BODY) == "6Z00547709541"


def test_extract_tracking_code_with_colon_separator():
    assert extract_tracking_code("Numéro de suivi : 7Y12345678901") == "7Y12345678901"


def test_extract_tracking_code_returns_none_when_absent():
    assert extract_tracking_code("Merci pour votre commande, elle est en préparation.") is None


def test_extract_tracking_code_ignores_short_numbers():
    assert extract_tracking_code("N° du colis 123") is None


def test_decode_body_prefers_plain_text():
    msg = email.message_from_string(
        "Content-Type: multipart/alternative; boundary=\"B\"\n"
        "\n"
        "--B\n"
        "Content-Type: text/plain; charset=utf-8\n"
        "\n"
        "N° du colis 6Z00547709541\n"
        "--B\n"
        "Content-Type: text/html; charset=utf-8\n"
        "\n"
        "<html><body>autre contenu</body></html>\n"
        "--B--\n"
    )
    assert "6Z00547709541" in decode_body(msg)


def test_decode_body_falls_back_to_html_stripped_of_tags():
    msg = email.message_from_string(
        "Content-Type: text/html; charset=utf-8\n"
        "\n"
        "<html><body><p>N&deg; du colis <b>6Z00547709541</b></p></body></html>\n"
    )
    assert "6Z00547709541" in decode_body(msg)


def test_build_from_search_criteria_single_domain():
    assert build_from_search_criteria(("a.com",)) == 'FROM "a.com"'


def test_build_from_search_criteria_two_domains():
    assert build_from_search_criteria(("a.com", "b.com")) == 'OR FROM "a.com" FROM "b.com"'


def test_build_from_search_criteria_three_domains():
    assert (
        build_from_search_criteria(("a.com", "b.com", "c.com"))
        == 'OR FROM "a.com" OR FROM "b.com" FROM "c.com"'
    )


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db()
    yield db_path


def _fake_imap(raw_messages: dict[str, bytes]) -> MagicMock:
    imap = MagicMock()
    imap.select.return_value = ("OK", [b"1"])
    imap.search.return_value = ("OK", [b" ".join(k.encode() for k in raw_messages)])

    def fetch(msg_id, spec):
        return "OK", [(b"1 (RFC822 {123}", raw_messages[msg_id])]

    imap.fetch.side_effect = fetch
    imap.copy.return_value = ("OK", [b"copied"])
    imap.store.return_value = ("OK", [b"stored"])
    imap.expunge.return_value = ("OK", [b"expunged"])
    return imap


def _raw_email(from_addr: str, body: str) -> bytes:
    return (
        f"From: {from_addr}\n"
        "Content-Type: text/plain; charset=utf-8\n"
        "\n"
        f"{body}\n"
    ).encode("utf-8")


def test_scan_once_creates_package_from_matching_mail(temp_db):
    raw = {
        "1": _raw_email(
            "La Poste-Colissimo <noreply@notif-colissimo-laposte.info>",
            "N° du colis 6Z00547709541",
        )
    }
    imap = _fake_imap(raw)
    with patch.object(mail_watcher, "_connect", return_value=imap):
        created = mail_watcher.scan_once()

    assert created == 1
    with db.get_conn() as conn:
        row = conn.execute("SELECT tracking_code FROM packages").fetchone()
    assert row["tracking_code"] == "6Z00547709541"
    imap.copy.assert_called_once_with("1", mail_watcher.MAIL_PROCESSED_LABEL)
    imap.store.assert_called_once_with("1", "+FLAGS", "\\Deleted")
    imap.expunge.assert_called_once()
    imap.logout.assert_called_once()


def test_scan_once_moves_mail_even_without_tracking_code(temp_db):
    raw = {
        "1": _raw_email(
            "La Poste-Colissimo <noreply@notif-colissimo-laposte.info>",
            "Votre commande est en préparation, pas encore de numéro.",
        )
    }
    imap = _fake_imap(raw)
    with patch.object(mail_watcher, "_connect", return_value=imap):
        created = mail_watcher.scan_once()

    assert created == 0
    with db.get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM packages").fetchone()
    assert row["n"] == 0
    imap.copy.assert_called_once_with("1", mail_watcher.MAIL_PROCESSED_LABEL)


def test_scan_once_does_not_duplicate_existing_tracking_code(temp_db):
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO packages (tracking_code, created_at, next_poll_at) VALUES (?, ?, ?)",
            ("6Z00547709541", "2026-09-22T00:00:00+00:00", "2026-09-22T00:00:00+00:00"),
        )
    raw = {
        "1": _raw_email(
            "La Poste-Colissimo <noreply@notif-colissimo-laposte.info>",
            "N° du colis 6Z00547709541",
        )
    }
    imap = _fake_imap(raw)
    with patch.object(mail_watcher, "_connect", return_value=imap):
        created = mail_watcher.scan_once()

    assert created == 0
    with db.get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM packages").fetchone()
    assert row["n"] == 1


def test_scan_once_skips_message_with_unhandled_error(temp_db):
    imap = _fake_imap({"1": b"not a valid rfc822 payload but still bytes"})
    imap.fetch.side_effect = Exception("boom")
    with patch.object(mail_watcher, "_connect", return_value=imap):
        created = mail_watcher.scan_once()

    assert created == 0
    imap.logout.assert_called_once()
