"""Mise en forme des dates La Poste pour l'interface et les notifications."""

from __future__ import annotations

from datetime import date, datetime

_WEEKDAYS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
_MONTHS = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet",
           "août", "septembre", "octobre", "novembre", "décembre")


def french_date(value: str | None) -> str:
    """'2026-10-27T01:00:00+01:00' -> 'mardi 27 octobre' (date locale renvoyée par La Poste)."""
    if not value:
        return ""
    try:
        d = date.fromisoformat(value[:10])
    except ValueError:
        return value
    return f"{_WEEKDAYS[d.weekday()]} {d.day} {_MONTHS[d.month - 1]}"


def eta_days(value: str | None, now: datetime | None = None) -> int | None:
    """Jours restants avant la date prévue (négatif si dépassée).

    Le jour courant est pris dans le fuseau de la date La Poste, pour ne pas dépendre de celui du conteneur."""
    if not value:
        return None
    try:
        target = datetime.fromisoformat(value)
    except ValueError:
        return None
    today = (now or datetime.now(target.tzinfo)).astimezone(target.tzinfo).date()
    return (target.date() - today).days


def _countdown(days: int) -> str:
    if days < 0:
        return "En retard"
    return {0: "Aujourd'hui", 1: "Demain"}.get(days, f"Dans {days} jours")


def eta_label(value: str | None, now: datetime | None = None) -> str:
    """Texte des notifications : « Demain · mardi 27 octobre »."""
    days = eta_days(value, now)
    if days is None:
        return french_date(value)
    return f"{_countdown(days)} · {french_date(value)}"


def eta_readout(value: str | None, now: datetime | None = None) -> dict | None:
    """Compte à rebours de l'interface ; tone suit les voyants (warn le jour J, alarm en retard)."""
    days = eta_days(value, now)
    if days is None:
        return None
    tone = "alarm" if days < 0 else "warn" if days == 0 else None
    return {"countdown": _countdown(days), "date": french_date(value), "tone": tone}
