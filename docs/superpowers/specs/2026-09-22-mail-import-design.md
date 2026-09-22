# Import automatique de colis via mail (IMAP)

Date : 2026-09-22

## Contexte

Colis Tracker demande aujourd'hui d'ajouter chaque colis manuellement via son
numéro de suivi. Beaucoup de ces numéros arrivent déjà par mail (confirmation
d'expédition Colissimo/Chronopost, y compris pour des commandes Amazon
expédiées par La Poste). Le but est de détecter ces mails et d'ajouter les
colis automatiquement, sans intervention manuelle.

**Tension avec la philosophie du projet** : le README met en avant « sans clé
API ni compte tiers ». Un accès Gmail complet (OAuth + Gmail API) irait à
l'encontre de ça. La solution retenue (IMAP + App Password) reste dans cet
esprit : c'est un compte mail personnel avec un mot de passe généré, pas une
inscription à un service tiers ni une clé API.

## Portée

- Fournisseur mail : Gmail (via IMAP standard, donc compatible avec tout
  serveur IMAP, mais seul Gmail est testé/documenté).
- Expéditeurs reconnus : domaines officiels Colissimo/Chronopost/La Poste
  uniquement (whitelist), pas de parsing générique tous expéditeurs, pas de
  support Amazon en direct (hors scope — cf. Limites connues du README).
- Fonctionnalité optionnelle : désactivée si les variables d'env IMAP ne sont
  pas définies, sans impact sur le reste de l'appli.

## Architecture

Nouveau module `app/mail_watcher.py`, sur le modèle de `app/poller.py` :
une boucle async démarrée dans le `lifespan` de `main.py` aux côtés du
`poller_loop()` existant, avec son propre intervalle.

```
main.py (lifespan)
  ├── poller_loop()        (existant, tick 60s)
  └── mail_watcher_loop()  (nouveau, tick ~5 min)
```

Les deux boucles sont indépendantes et ne partagent pas d'état, seulement la
table `packages` en base.

### Extraction de la logique de notification

`_notify()` dans `poller.py` est utilisée par `mail_watcher.py` (alerte en
cas d'échec de connexion IMAP répété). Pour éviter une dépendance croisée
`mail_watcher → poller`, `_notify()` et les variables `NTFY_URL`/`NTFY_TOPIC`
sont extraites dans un nouveau module `app/notify.py`. `poller.py` est mis à
jour pour importer depuis ce module (comportement inchangé).

## Configuration

Variables d'environnement (mêmes conventions que `NTFY_URL`/`NTFY_TOPIC`) :

| Variable | Obligatoire | Défaut | Description |
|---|---|---|---|
| `IMAP_HOST` | non | — | Hôte du serveur IMAP. Absent = feature désactivée. |
| `IMAP_PORT` | non | `993` | Port IMAP SSL. |
| `IMAP_USER` | oui si `IMAP_HOST` défini | — | Adresse mail. |
| `IMAP_PASSWORD` | oui si `IMAP_HOST` défini | — | App Password (Gmail : mot de passe d'application dédié, pas le mot de passe du compte). |
| `IMAP_FOLDER` | non | `INBOX` | Dossier à scanner. |
| `MAIL_WATCH_INTERVAL_MINUTES` | non | `5` | Intervalle entre deux scans. |
| `MAIL_PROCESSED_LABEL` | non | `colis-tracker/traite` | Label IMAP où déplacer les mails traités. |

Si `IMAP_HOST` est vide/absent, `mail_watcher_loop()` ne démarre pas
(log d'info au démarrage) — comportement symétrique à l'absence de
`NTFY_URL`.

## Cycle de scan

1. Connexion IMAP SSL (`imaplib.IMAP4_SSL`), authentification, sélection du
   dossier `IMAP_FOLDER`.
2. Recherche des mails non traités : `SEARCH` sur les expéditeurs de la
   whitelist (`FROM` contient un domaine whitelisté), dans le dossier source
   uniquement — une fois déplacés vers le label de traitement, ils
   disparaissent du dossier source et ne sont donc jamais revus.
3. Whitelist de domaines expéditeurs, codée en dur dans `mail_watcher.py`
   (liste courte, pas besoin de config externe) :
   - `notif-colissimo-laposte.info`
   - `laposte.fr`
4. Pour chaque mail trouvé :
   a. Extraction du corps (texte brut, ou fallback sur le texte extrait du
      HTML si pas de partie texte).
   b. Recherche d'un numéro de suivi Colissimo/Chronopost par regex sur le
      corps du mail. Format cible observé : 2 lettres + 11 chiffres (ex.
      `6Z00547709541` — 13 caractères, préfixe alphanumérique). Regex :
      `\b[0-9A-Z]{1}[A-Z0-9]{12}\b` affinée pour capturer spécifiquement le
      pattern après une étiquette du type `N° du colis` / `numéro de colis`
      / `n° de suivi` (recherche textuelle du label suivi du code, pour
      limiter les faux positifs plutôt qu'un regex générique sur tout le
      corps).
   c. Code trouvé et non déjà en base → `INSERT OR IGNORE INTO packages
      (tracking_code, created_at, next_poll_at) VALUES (...)`, comme le fait
      `add_package()` dans `main.py`. Pas de poll immédiat synchrone (la
      boucle `poller_loop()` le prendra dans son prochain cycle, `next_poll_at`
      = maintenant).
   d. Aucun code trouvé → le mail est quand même marqué comme traité (voir
      étape 5), pour ne pas le rescanner indéfiniment.
   e. Pas de label posé sur le package créé (`label = NULL`), l'utilisateur
      peut renommer a posteriori (fonctionnalité déjà existante).
5. Que le mail ait produit un colis ou non, il est déplacé vers le label
   IMAP `MAIL_PROCESSED_LABEL` (créé s'il n'existe pas via `CREATE` puis
   `COPY` + suppression du dossier source, ou `MOVE` si le serveur le
   supporte). Ceci garantit qu'un mail n'est jamais retraité, y compris en
   cas de whitelist élargie plus tard sur un mail déjà vu.
6. Déconnexion propre de la session IMAP en fin de cycle (pas de connexion
   persistante entre deux scans).

## Gestion des erreurs

- Échec de connexion/authentification IMAP : log serveur (`logger.warning`)
  + notification ntfy via `notify._notify()` si `NTFY_URL`/`NTFY_TOPIC` sont
  définis (titre `"Colis Tracker — import mail"`, message = erreur). Pas de
  retry immédiat, on attend le prochain tick.
- Erreur de parsing sur un mail individuel (corps illisible, encodage) :
  log + mail marqué comme traité quand même, pour ne pas bloquer le scan des
  suivants.
- La boucle `mail_watcher_loop()` attrape toute exception non prévue par
  cycle (même pattern que `poller_loop()`), pour ne jamais crasher le task
  asyncio.

## Base de données

Aucune migration de schéma nécessaire : réutilisation intégrale de la table
`packages` existante et de la contrainte `UNIQUE(tracking_code)` pour la
déduplication. L'état "mail traité" est porté par IMAP (label), pas par
SQLite — pas de nouvelle table.

## Tests

- `test_mail_watcher.py` (nouveau) :
  - Extraction du numéro de suivi depuis le corps du mail d'exemple fourni
    (fixture texte), assertions sur le code exact `6Z00547709541`.
  - Cas négatif : mail sans numéro reconnaissable → aucune exception, retour
    `None`.
  - Whitelist : mail d'un expéditeur hors whitelist → ignoré.
  - Flux bout-en-bout avec `imaplib` mocké : un mail retourné par `SEARCH`
    → un package inséré en base + le mail marqué/déplacé comme traité.
- Pas de test d'intégration contre un vrai serveur IMAP (pas d'infra de test
  disponible pour ça) — cohérent avec l'absence de tests contre le vrai
  endpoint La Poste en CI.

## Documentation

Mise à jour du README :
- Nouvelle section "Import automatique via mail" décrivant la config IMAP,
  avec l'avertissement sur l'usage d'un App Password (jamais le mot de passe
  principal du compte).
- Retrait de la ligne "Pas d'import automatique des commandes Amazon" des
  limites connues si Amazon est couvert transitivement par les mails
  Colissimo (à reformuler : "Import automatique disponible pour les mails
  Colissimo/Chronopost, y compris certaines commandes Amazon expédiées par La
  Poste").

## Hors scope (explicitement exclu)

- OAuth / Gmail API.
- Support d'autres transporteurs (Mondial Relay, DPD, GLS) dans le parsing
  mail — cohérent avec la limite déjà documentée du projet.
- Interface de configuration IMAP dans l'UI web.
- Détection Amazon directe (mails "Votre colis a été expédié" Amazon sans
  passer par un mail Colissimo).
- IMAP IDLE / push temps réel.
