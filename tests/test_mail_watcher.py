import email

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
