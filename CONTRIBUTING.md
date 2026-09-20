# Contribuer

Merci de l'intérêt porté à ce projet. C'est un petit outil perso rendu public, donc gardons les choses simples.

## Setup

```bash
uv sync
uv run uvicorn app.main:app --reload
```

## Avant d'ouvrir une PR

```bash
uv run ty check app
docker build -t colis-tracker .   # vérifie que l'image build toujours
```

## Signaler un problème de suivi

Si un colis reste bloqué sur un statut `unknown` ou une erreur, ouvrez une issue avec :
- le numéro de suivi (ou son transporteur/format, si vous préférez ne pas partager le numéro exact)
- la réponse brute de l'endpoint si possible :
  ```bash
  curl -H "User-Agent: PostmanRuntime/7.49.1" "https://www.laposte.fr/ssu/sun/back/suivi-unifie/VOTRE_CODE?lang=fr"
  ```

Les mappings de statuts (`_COLISSIMO_MAP`/`_CHRONOPOST_MAP` dans `app/laposte_client.py`) sont construits empiriquement — un nouveau code de statut non reconnu est normal et bienvenu en issue.

## Style

- Pas de dépendance ajoutée sans raison concrète
- `ty` doit passer sans erreur
- Les commentaires expliquent le "pourquoi", pas le "quoi"
