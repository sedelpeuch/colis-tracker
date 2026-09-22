# Colis Tracker

Suivi de colis Colissimo/Chronopost self-hosted, sans clé API ni compte tiers.

Interroge directement l'endpoint public (non officiel) utilisé par [laposte.fr](https://www.laposte.fr/outils/suivre-vos-envois) pour son propre suivi — le même que celui qu'utilise l'intégration Home Assistant [`ha-parcel-integrations/ha-laposte`](https://github.com/ha-parcel-integrations/ha-laposte), dont le principe (endpoint, mapping de statuts) a inspiré ce projet.

![CI](https://github.com/sedelpeuch/colis-tracker/actions/workflows/ci.yml/badge.svg)

## Pourquoi

Amazon et La Poste n'exposent aucune API grand public pour suivre ses colis sans compte pro ni service tiers payant (17TRACK, AfterShip...). Comme la quasi-totalité des livraisons transitent par La Poste (y compris les colis Amazon), interroger directement son endpoint de suivi suffit à couvrir le besoin — sans dépendance externe.

## Fonctionnalités

- Ajout d'un colis par son numéro de suivi Colissimo ou Chronopost
- Polling automatique et adaptatif (plus fréquent quand un colis est en livraison le jour même, coupure nocturne)
- Historique complet du colis, pas seulement les changements détectés (La Poste renvoie toute la timeline à chaque appel)
- Notification [ntfy](https://ntfy.sh/) optionnelle à chaque changement de statut
- Thème clair/sombre
- Aucune base de données externe : SQLite en fichier

## Démarrage rapide

```bash
docker compose up -d
```

L'application écoute sur `http://localhost:8000`.

Variables d'environnement optionnelles (notification ntfy) :

```bash
NTFY_URL=https://ntfy.sh
NTFY_TOPIC=mes-colis
```

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

## Développement local

Le projet utilise [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run uvicorn app.main:app --reload
uv run pytest         # tests
uv run ty check app   # vérification de types
```

## Comment ça marche

Le client (`app/laposte_client.py`) interroge :

```
GET https://www.laposte.fr/ssu/sun/back/suivi-unifie/{tracking_code}?lang=fr
User-Agent: PostmanRuntime/7.49.1
```

C'est l'endpoint public que la page de suivi laposte.fr appelle elle-même côté navigateur — aucune authentification requise, mais un User-Agent usurpé est nécessaire (l'edge renvoie 403 sur un user-agent identifiable comme bot).

⚠️ Cet endpoint n'est pas un contrat d'API officiel et documenté : il peut changer sans préavis. Si le suivi d'un colis reste bloqué sur une erreur, ouvrez une issue avec le code retourné.

## Limites connues

- Colissimo et Chronopost uniquement (les deux appartiennent au groupe La Poste) — pas de support Mondial Relay, DPD ou GLS
- Import automatique disponible pour les mails Colissimo/Chronopost (voir "Import automatique via mail"), y compris pour certaines commandes Amazon expédiées par La Poste — pas de détection directe des mails Amazon eux-mêmes

## Licence

[MIT](LICENSE)
