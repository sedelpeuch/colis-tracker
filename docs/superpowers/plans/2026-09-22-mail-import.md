# Import automatique de colis via mail (IMAP) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Détecter automatiquement les mails de confirmation Colissimo/Chronopost dans une boîte mail via IMAP et ajouter les colis correspondants sans intervention manuelle.

**Architecture:** Nouveau module `app/mail_watcher.py` qui tourne en boucle async (comme `app/poller.py`), scanne une boîte IMAP à intervalle régulier, extrait les numéros de suivi par regex depuis les mails d'expéditeurs whitelistés, insère les colis en base, et déplace les mails traités vers un label dédié pour ne jamais les revoir. La logique de notification ntfy est extraite de `poller.py` vers un nouveau module partagé `app/notify.py`.

**Tech Stack:** Python 3.12+, `imaplib`/`email` (stdlib, pas de nouvelle dépendance IMAP), pytest (nouvelle dépendance dev, le projet n'a aucun test actuellement).

**Spec:** `docs/superpowers/specs/2026-09-22-mail-import-design.md`

## Global Constraints

- Pas de clé API ni compte tiers : IMAP + App Password uniquement, jamais OAuth/Gmail API.
- Feature optionnelle : si `IMAP_HOST` n'est pas défini, le watcher ne démarre pas et le reste de l'appli est inchangé.
- Whitelist expéditeurs codée en dur : `notif-colissimo-laposte.info`, `laposte.fr`.
- Aucune migration de schéma : réutilisation intégrale de la table `packages` existante et de sa contrainte `UNIQUE(tracking_code)`.
- État "mail traité" porté par IMAP (déplacement vers un label), pas par une nouvelle table SQLite.
- `ty check app` doit passer sans erreur (règle du projet, cf. `CONTRIBUTING.md`).

---

## Task 1: Extraire `_notify` de `poller.py` vers `app/notify.py`

**Files:**
- Create: `app/notify.py`
- Modify: `app/poller.py:1-51` (retirer `NTFY_URL`, `NTFY_TOPIC`, `_notify`, importer depuis `notify.py`, remplacer les appels `_notify(` par `notify(`)

**Interfaces:**
- Produces: `app.notify.notify(title: str, message: str, *, attachment: bytes | None = None, attachment_name: str | None = None) -> None` — coroutine, no-op silencieux si `NTFY_URL`/`NTFY_TOPIC` absents. Utilisée par `poller.py` (déjà) et par `mail_watcher.py` (Task 4).

Ce refactor est un pur déplacement de code sans changement de comportement : pas de nouveau test, l'appli existante n'a aucun test à ce jour (vérifié via `find . -iname "test_*"` : aucun résultat).

- [ ] **Step 1: Créer `app/notify.py`**

```python
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
    headers = {"Title": title}
    content = message.encode("utf-8")
    if attachment is not None:
        headers["Message"] = message
        headers["Filename"] = attachment_name or "barcode.png"
        content = attachment
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{NTFY_URL.rstrip('/')}/{NTFY_TOPIC}",
                content=content,
                headers=headers,
                timeout=10.0,
            )
    except httpx.HTTPError:
        logger.warning("ntfy notification failed", exc_info=True)
```

- [ ] **Step 2: Modifier `app/poller.py`**

Retirer les lignes 22-23 (`NTFY_URL`/`NTFY_TOPIC`) et la fonction `_notify` (lignes 30-50), ajouter l'import, et remplacer les deux appels `await _notify(` (lignes 121 et 130) par `await notify(`.

```python
# En haut du fichier, dans les imports (après `from . import db`) :
from .notify import notify
```

Le fichier `app/poller.py` ne doit plus contenir `NTFY_URL`, `NTFY_TOPIC`, ni `def _notify`.

- [ ] **Step 3: Vérifier le typage et l'absence de régression évidente**

Run: `uv run ty check app`
Expected: aucune erreur.

Run: `uv run python -c "from app import poller, notify; print(poller.notify is notify.notify)"`
Expected: `True`

- [ ] **Step 4: Commit**

```bash
git add app/notify.py app/poller.py
git commit -m "refactor: extrait la notification ntfy dans app/notify.py"
```

---

## Task 2: Ajouter pytest + logique pure de parsing des mails

**Files:**
- Create: `app/mail_watcher.py`
- Create: `tests/__init__.py`
- Create: `tests/test_mail_watcher.py`
- Modify: `pyproject.toml` (ajouter `pytest` au groupe `dev`)

**Interfaces:**
- Produces (utilisées par Task 3) :
  - `app.mail_watcher.SENDER_WHITELIST: tuple[str, ...]`
  - `app.mail_watcher.is_whitelisted_sender(from_header: str) -> bool`
  - `app.mail_watcher.extract_tracking_code(body: str) -> str | None`
  - `app.mail_watcher.decode_body(msg: email.message.Message) -> str`
  - `app.mail_watcher.build_from_search_criteria(domains: tuple[str, ...]) -> str`

- [ ] **Step 1: Ajouter pytest en dépendance dev**

Modifier `pyproject.toml` :

```toml
[dependency-groups]
dev = [
    "pytest>=8.0",
    "ty>=0.0.1a0",
]
```

Run: `uv sync`

- [ ] **Step 2: Créer `tests/__init__.py` (vide)**

```python
```

- [ ] **Step 3: Écrire les tests qui échouent pour `is_whitelisted_sender`**

Créer `tests/test_mail_watcher.py` :

```python
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
```

- [ ] **Step 4: Run pour vérifier l'échec**

Run: `uv run pytest tests/test_mail_watcher.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.mail_watcher'` (le module n'existe pas encore).

- [ ] **Step 5: Créer `app/mail_watcher.py` avec `SENDER_WHITELIST` et `is_whitelisted_sender`**

```python
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

SENDER_WHITELIST: tuple[str, ...] = ("notif-colissimo-laposte.info", "laposte.fr")


def is_whitelisted_sender(from_header: str) -> bool:
    return any(domain in from_header for domain in SENDER_WHITELIST)
```

- [ ] **Step 6: Run pour vérifier que ces 4 tests passent**

Run: `uv run pytest tests/test_mail_watcher.py -v`
Expected: les 4 tests `test_is_whitelisted_sender_*` PASS, les autres échouent encore à l'import (normal, pas encore écrits).

- [ ] **Step 7: Ajouter les tests pour `extract_tracking_code`, avec l'exemple réel de mail Colissimo**

Ajouter à `tests/test_mail_watcher.py` :

```python
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
```

- [ ] **Step 8: Run pour vérifier l'échec**

Run: `uv run pytest tests/test_mail_watcher.py -v`
Expected: FAIL — `ImportError: cannot import name 'extract_tracking_code'`.

- [ ] **Step 9: Implémenter `extract_tracking_code`**

Ajouter à `app/mail_watcher.py` :

```python
TRACKING_CODE_RE = re.compile(
    r"(?:n°\s*(?:du\s*)?colis|num[ée]ro\s+de\s+suivi|n°\s*de\s+suivi)\s*[:\-]?\s*([0-9A-Z]{11,15})",
    re.IGNORECASE,
)


def extract_tracking_code(body: str) -> str | None:
    match = TRACKING_CODE_RE.search(body)
    if match is None:
        return None
    return match.group(1).upper()
```

- [ ] **Step 10: Run pour vérifier que ces 4 tests passent**

Run: `uv run pytest tests/test_mail_watcher.py -v`
Expected: PASS pour les 8 tests écrits jusqu'ici.

- [ ] **Step 11: Ajouter les tests pour `decode_body`**

Ajouter à `tests/test_mail_watcher.py` :

```python
import email


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
```

- [ ] **Step 12: Run pour vérifier l'échec**

Run: `uv run pytest tests/test_mail_watcher.py -v`
Expected: FAIL — `ImportError: cannot import name 'decode_body'`.

- [ ] **Step 13: Implémenter `decode_body`**

Ajouter à `app/mail_watcher.py` (en haut du fichier, ajouter les imports `html` et `email.message.Message`) :

```python
import html as html_module
from email.message import Message

TAG_RE = re.compile(r"<[^>]+>")


def _decode_part(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
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
```

- [ ] **Step 14: Run pour vérifier que ces 2 tests passent**

Run: `uv run pytest tests/test_mail_watcher.py -v`
Expected: PASS pour les 10 tests écrits jusqu'ici.

- [ ] **Step 15: Ajouter les tests pour `build_from_search_criteria`**

Ajouter à `tests/test_mail_watcher.py` :

```python
def test_build_from_search_criteria_single_domain():
    assert build_from_search_criteria(("a.com",)) == 'FROM "a.com"'


def test_build_from_search_criteria_two_domains():
    assert build_from_search_criteria(("a.com", "b.com")) == 'OR FROM "a.com" FROM "b.com"'


def test_build_from_search_criteria_three_domains():
    assert (
        build_from_search_criteria(("a.com", "b.com", "c.com"))
        == 'OR FROM "a.com" OR FROM "b.com" FROM "c.com"'
    )
```

- [ ] **Step 16: Run pour vérifier l'échec**

Run: `uv run pytest tests/test_mail_watcher.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_from_search_criteria'`.

- [ ] **Step 17: Implémenter `build_from_search_criteria`**

Ajouter à `app/mail_watcher.py` :

```python
def build_from_search_criteria(domains: tuple[str, ...]) -> str:
    if len(domains) == 1:
        return f'FROM "{domains[0]}"'
    return f'OR {build_from_search_criteria(domains[:1])} {build_from_search_criteria(domains[1:])}'
```

- [ ] **Step 18: Run pour vérifier que tous les tests passent**

Run: `uv run pytest tests/test_mail_watcher.py -v`
Expected: PASS pour les 13 tests.

- [ ] **Step 19: Vérifier le typage**

Run: `uv run ty check app`
Expected: aucune erreur.

- [ ] **Step 20: Commit**

```bash
git add pyproject.toml uv.lock tests/__init__.py tests/test_mail_watcher.py app/mail_watcher.py
git commit -m "feat: parsing pur des mails de suivi (whitelist, extraction du code, corps du mail)"
```

---

## Task 3: Scan IMAP avec insertion en base (mocké)

**Files:**
- Modify: `app/mail_watcher.py` (ajouter connexion IMAP, insertion DB, déplacement des mails)
- Modify: `tests/test_mail_watcher.py` (ajouter les tests du flux complet mocké)

**Interfaces:**
- Consumes : `app.db.get_conn()` (context manager déjà existant, cf. `app/db.py:45-54`), `app.mail_watcher.is_whitelisted_sender`, `extract_tracking_code`, `decode_body`, `build_from_search_criteria` (Task 2).
- Produces (utilisées par Task 4) :
  - `app.mail_watcher.IMAP_HOST`, `IMAP_PORT`, `IMAP_USER`, `IMAP_PASSWORD`, `IMAP_FOLDER`, `MAIL_PROCESSED_LABEL` (variables de module, lues depuis l'environnement)
  - `app.mail_watcher.scan_once() -> int` — se connecte, scanne, retourne le nombre de colis nouvellement créés. Lève les exceptions de connexion (pas de try/except au niveau connexion, c'est `mail_watcher_loop` en Task 4 qui les attrape).

- [ ] **Step 1: Écrire le test du flux complet, avec `imaplib` mocké**

Ajouter à `tests/test_mail_watcher.py` :

```python
import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from app import db, mail_watcher


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db()
    yield db_path


def _fake_imap(raw_messages: dict[bytes, bytes]) -> MagicMock:
    imap = MagicMock()
    imap.select.return_value = ("OK", [b"1"])
    imap.search.return_value = ("OK", [b" ".join(raw_messages.keys())])

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
        b"1": _raw_email(
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
    imap.copy.assert_called_once_with(b"1", mail_watcher.MAIL_PROCESSED_LABEL)
    imap.store.assert_called_once_with(b"1", "+FLAGS", "\\Deleted")
    imap.expunge.assert_called_once()
    imap.logout.assert_called_once()


def test_scan_once_moves_mail_even_without_tracking_code(temp_db):
    raw = {
        b"1": _raw_email(
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
    imap.copy.assert_called_once_with(b"1", mail_watcher.MAIL_PROCESSED_LABEL)


def test_scan_once_does_not_duplicate_existing_tracking_code(temp_db):
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO packages (tracking_code, created_at, next_poll_at) VALUES (?, ?, ?)",
            ("6Z00547709541", "2026-09-22T00:00:00+00:00", "2026-09-22T00:00:00+00:00"),
        )
    raw = {
        b"1": _raw_email(
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
    imap = _fake_imap({b"1": b"not a valid rfc822 payload but still bytes"})
    imap.fetch.side_effect = Exception("boom")
    with patch.object(mail_watcher, "_connect", return_value=imap):
        created = mail_watcher.scan_once()

    assert created == 0
    imap.logout.assert_called_once()
```

- [ ] **Step 2: Run pour vérifier l'échec**

Run: `uv run pytest tests/test_mail_watcher.py -v`
Expected: FAIL — `AttributeError: module 'app.mail_watcher' has no attribute '_connect'` (ou `scan_once`).

- [ ] **Step 3: Implémenter la connexion, l'insertion en base et `scan_once`**

Ajouter à `app/mail_watcher.py` (imports supplémentaires en haut : `email`, `imaplib`, `os`, `from . import db`) :

```python
import email
import imaplib
import os
from datetime import UTC, datetime

from . import db

IMAP_HOST = os.environ.get("IMAP_HOST")
IMAP_PORT = int(os.environ.get("IMAP_PORT", "993"))
IMAP_USER = os.environ.get("IMAP_USER")
IMAP_PASSWORD = os.environ.get("IMAP_PASSWORD")
IMAP_FOLDER = os.environ.get("IMAP_FOLDER", "INBOX")
MAIL_PROCESSED_LABEL = os.environ.get("MAIL_PROCESSED_LABEL", "colis-tracker/traite")


def _connect() -> imaplib.IMAP4:
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


def _process_one(imap: imaplib.IMAP4, msg_id: bytes) -> bool:
    typ, msg_data = imap.fetch(msg_id, "(RFC822)")
    raw = msg_data[0][1]
    msg = email.message_from_bytes(raw)

    created = False
    from_header = msg.get("From", "")
    if is_whitelisted_sender(from_header):
        body = decode_body(msg)
        tracking_code = extract_tracking_code(body)
        if tracking_code is not None:
            created = _create_package(tracking_code)

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
        msg_ids = data[0].split() if data and data[0] else []
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
```

- [ ] **Step 4: Run pour vérifier que tous les tests passent**

Run: `uv run pytest tests/test_mail_watcher.py -v`
Expected: PASS pour les 17 tests (13 précédents + 4 nouveaux).

- [ ] **Step 5: Vérifier le typage**

Run: `uv run ty check app`
Expected: aucune erreur.

- [ ] **Step 6: Commit**

```bash
git add app/mail_watcher.py tests/test_mail_watcher.py
git commit -m "feat: scan IMAP mocké — insertion des colis et déplacement des mails traités"
```

---

## Task 4: Boucle async + intégration dans `main.py`

**Files:**
- Modify: `app/mail_watcher.py` (ajouter `WATCH_INTERVAL_MINUTES`, `mail_watcher_loop`)
- Modify: `app/main.py:1-32` (démarrer la boucle dans `lifespan` si configurée)
- Modify: `tests/test_mail_watcher.py` (test du cas désactivé)

**Interfaces:**
- Consumes : `app.notify.notify` (Task 1), `app.mail_watcher.scan_once` (Task 3).
- Produces : `app.mail_watcher.mail_watcher_loop() -> None` (coroutine), consommée par `app/main.py`.

- [ ] **Step 1: Écrire le test du cas désactivé (pas d'`IMAP_HOST`)**

Ajouter à `tests/test_mail_watcher.py` :

```python
import asyncio


def test_mail_watcher_loop_returns_immediately_when_disabled(monkeypatch):
    monkeypatch.setattr(mail_watcher, "IMAP_HOST", None)
    called = False

    def _fail_if_called():
        nonlocal called
        called = True

    monkeypatch.setattr(mail_watcher, "scan_once", _fail_if_called)
    asyncio.run(mail_watcher.mail_watcher_loop())

    assert called is False
```

- [ ] **Step 2: Run pour vérifier l'échec**

Run: `uv run pytest tests/test_mail_watcher.py -v`
Expected: FAIL — `AttributeError: module 'app.mail_watcher' has no attribute 'mail_watcher_loop'`.

- [ ] **Step 3: Implémenter `mail_watcher_loop`**

Ajouter à `app/mail_watcher.py` (imports supplémentaires : `asyncio`) et à la suite des constantes IMAP :

```python
WATCH_INTERVAL_MINUTES = int(os.environ.get("MAIL_WATCH_INTERVAL_MINUTES", "5"))
```

Puis à la fin du fichier :

```python
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
        except Exception as err:
            logger.exception("mail scan cycle failed")
            from .notify import notify

            await notify("Colis Tracker — import mail", str(err))
        await asyncio.sleep(WATCH_INTERVAL_MINUTES * 60)
```

Ajouter `import asyncio` en haut du fichier avec les autres imports stdlib.

- [ ] **Step 4: Run pour vérifier que le test passe**

Run: `uv run pytest tests/test_mail_watcher.py -v`
Expected: PASS pour les 18 tests.

Note : ce test vérifie uniquement la branche "désactivé" (return immédiat). La boucle infinie elle-même (branche activée) n'est pas testée unitairement — cohérent avec `poller_loop()` qui n'a pas non plus de test de sa boucle infinie ; le comportement testé est celui de `scan_once` (Task 3).

- [ ] **Step 5: Intégrer dans `app/main.py`**

Modifier la fonction `lifespan` dans `app/main.py` :

```python
from .mail_watcher import mail_watcher_loop
from .poller import poller_loop


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    poller_task = asyncio.create_task(poller_loop())
    mail_task = asyncio.create_task(mail_watcher_loop())
    yield
    poller_task.cancel()
    mail_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await poller_task
    with contextlib.suppress(asyncio.CancelledError):
        await mail_task
```

- [ ] **Step 6: Vérifier le typage et le démarrage de l'appli**

Run: `uv run ty check app`
Expected: aucune erreur.

Run: `uv run python -c "from app.main import app; print('ok')"`
Expected: `ok` (pas d'exception à l'import, la boucle mail ne démarre pas réellement ici puisqu'on n'exécute pas le lifespan).

- [ ] **Step 7: Vérification manuelle rapide (sans vrai serveur IMAP)**

Run: `IMAP_HOST= uv run uvicorn app.main:app &` puis `curl -s localhost:8000 -o /dev/null -w "%{http_code}\n"` puis arrêter le serveur (`kill %1`).
Expected: `200` — l'appli démarre normalement, log `"mail watcher disabled (IMAP_HOST not set)"` visible dans la sortie du serveur, aucune régression sur le poller La Poste existant.

- [ ] **Step 8: Commit**

```bash
git add app/mail_watcher.py app/main.py tests/test_mail_watcher.py
git commit -m "feat: démarre la boucle d'import mail dans le lifespan de l'appli"
```

---

## Task 5: Configuration, documentation, CI

**Files:**
- Modify: `compose.yml`
- Modify: `README.md`
- Modify: `CONTRIBUTING.md`
- Modify: `.github/workflows/ci.yml`

**Interfaces:** Aucune (tâche de configuration/documentation uniquement).

- [ ] **Step 1: Ajouter les variables IMAP à `compose.yml`**

```yaml
    environment:
      - NTFY_URL=${NTFY_URL:-}
      - NTFY_TOPIC=${NTFY_TOPIC:-}
      - IMAP_HOST=${IMAP_HOST:-}
      - IMAP_PORT=${IMAP_PORT:-993}
      - IMAP_USER=${IMAP_USER:-}
      - IMAP_PASSWORD=${IMAP_PASSWORD:-}
      - IMAP_FOLDER=${IMAP_FOLDER:-INBOX}
      - MAIL_PROCESSED_LABEL=${MAIL_PROCESSED_LABEL:-colis-tracker/traite}
      - MAIL_WATCH_INTERVAL_MINUTES=${MAIL_WATCH_INTERVAL_MINUTES:-5}
```

- [ ] **Step 2: Ajouter un job de tests dans `.github/workflows/ci.yml`**

Ajouter un nouveau job au fichier, à côté de `lint` et `docker-build` :

```yaml
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
        with:
          version: "latest"
      - run: uv sync
      - run: uv run pytest -v
```

- [ ] **Step 3: Mettre à jour `CONTRIBUTING.md`**

Dans la section "Avant d'ouvrir une PR", ajouter avant `uv run ty check app` :

```
uv run pytest
```

- [ ] **Step 4: Mettre à jour `README.md`**

Ajouter une nouvelle section après "## Démarrage rapide" (avant "## Développement local") :

```markdown
## Import automatique via mail (optionnel)

Colis Tracker peut surveiller une boîte mail en IMAP pour détecter les mails
de confirmation Colissimo/Chronopost et ajouter les colis automatiquement,
sans copier-coller de numéro de suivi. Désactivé par défaut.

```bash
IMAP_HOST=imap.gmail.com
IMAP_PORT=993
IMAP_USER=vous@gmail.com
IMAP_PASSWORD=xxxx xxxx xxxx xxxx   # App Password, jamais le mot de passe du compte
IMAP_FOLDER=INBOX
MAIL_PROCESSED_LABEL=colis-tracker/traite
MAIL_WATCH_INTERVAL_MINUTES=5
```

Sur Gmail, un [App Password](https://myaccount.google.com/apppasswords)
dédié est requis (nécessite la validation en 2 étapes activée sur le
compte) — jamais le mot de passe principal. Aucun accès OAuth ni API Google
n'est utilisé, uniquement le protocole IMAP standard.

Seuls les mails provenant de domaines officiels La Poste/Colissimo (ex.
`notif-colissimo-laposte.info`, `laposte.fr`) sont analysés. Chaque mail
traité (numéro trouvé ou non) est déplacé vers le label IMAP
`MAIL_PROCESSED_LABEL` pour ne jamais être scanné deux fois.
```

Modifier la section "## Limites connues" : remplacer

```
- Pas d'import automatique des commandes Amazon : le numéro de suivi doit être ajouté manuellement
```

par

```
- Import automatique disponible pour les mails Colissimo/Chronopost (voir "Import automatique via mail"), y compris pour certaines commandes Amazon expédiées par La Poste — pas de détection directe des mails Amazon eux-mêmes
```

Ajouter dans la section "## Développement local", après la commande `ty check` :

```bash
uv run pytest         # tests
```

- [ ] **Step 5: Vérifier que tout est cohérent**

Run: `uv run pytest -v && uv run ty check app`
Expected: tous les tests passent, aucune erreur de typage.

- [ ] **Step 6: Commit**

```bash
git add compose.yml README.md CONTRIBUTING.md .github/workflows/ci.yml
git commit -m "docs: documente l'import automatique via mail et ajoute le job de tests en CI"
```
