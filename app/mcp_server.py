"""Serveur MCP (streamable HTTP, sans état) monté sur /mcp, protégé par un jeton bearer.

Monté seulement si `MCP_TOKEN` est défini. Les outils réutilisent les fonctions de l'appli."""

from __future__ import annotations

import os
import secrets
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import FastAPI
from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from . import db
from .barcode import generate_barcode_png
from .packages import POSTMARK_LABELS, register_package

MCP_TOKEN = os.environ.get("MCP_TOKEN")

READ = ToolAnnotations(read_only_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False)
DESTROY = ToolAnnotations(read_only_hint=False, destructive_hint=True)

mcp = MCPServer(
    "colis-tracker",
    instructions=(
        "Suivi de colis Colissimo/Chronopost. Un colis se désigne par son id ou son numéro "
        "de suivi. Le statut est relevé automatiquement auprès de La Poste."
    ),
)


def _package(row) -> dict[str, Any]:
    data = dict(row)
    data["delivered"] = bool(data["delivered"])
    data["status_label"] = POSTMARK_LABELS.get(data["status"], data["status"])
    return data


def _find(conn, package: str):
    """Colis désigné par son id ou son numéro de suivi."""
    key = package.strip()
    if key.isdigit():
        row = conn.execute("SELECT * FROM packages WHERE id = ?", (int(key),)).fetchone()
        if row is not None:
            return row
    row = conn.execute(
        "SELECT * FROM packages WHERE tracking_code = ?", (key.upper(),)
    ).fetchone()
    if row is None:
        raise ToolError(f"Colis introuvable : {package}")
    return row


@mcp.tool(annotations=READ)
def list_packages(
    state: Literal["all", "in_progress", "delivered", "errored"] = "all",
) -> list[dict[str, Any]]:
    """Liste les colis suivis, du plus récemment mis à jour au plus ancien.

    state : in_progress (en cours), delivered (livrés), errored (en erreur), all (tous)."""
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM packages ORDER BY updated_at DESC, created_at DESC"
        ).fetchall()
    keep = {
        "all": lambda r: True,
        "in_progress": lambda r: not r["delivered"] and not r["error"],
        "delivered": lambda r: bool(r["delivered"]),
        "errored": lambda r: not r["delivered"] and bool(r["error"]),
    }[state]
    return [_package(r) for r in rows if keep(r)]


@mcp.tool(annotations=READ)
def get_package(package: str) -> dict[str, Any]:
    """Détail d'un colis (id ou numéro de suivi) avec l'historique de ses statuts."""
    with db.get_conn() as conn:
        row = _find(conn, package)
        events = conn.execute(
            "SELECT status, raw_status, created_at FROM events "
            "WHERE package_id = ? ORDER BY created_at DESC",
            (row["id"],),
        ).fetchall()
    data = _package(row)
    data["events"] = [dict(e) for e in events]
    return data


@mcp.tool(annotations=READ)
def get_barcode(package: str) -> Image:
    """Code-barres PNG du numéro de suivi, à présenter au point relais."""
    with db.get_conn() as conn:
        row = _find(conn, package)
    return Image(data=generate_barcode_png(row["tracking_code"]), format="png")


@mcp.tool(annotations=WRITE)
async def add_package(tracking_code: str, label: str = "") -> dict[str, Any]:
    """Ajoute un colis à suivre et relève son statut tout de suite.

    Sans effet si le numéro est déjà suivi (le colis existant est renvoyé)."""
    code = tracking_code.strip().upper()
    if not code:
        raise ToolError("tracking_code requis")
    await register_package(code, label)
    return get_package(code)


@mcp.tool(annotations=WRITE)
def rename_package(package: str, label: str) -> dict[str, Any]:
    """Change le libellé d'un colis (libellé vide : aucun libellé)."""
    with db.get_conn() as conn:
        row = _find(conn, package)
        conn.execute(
            "UPDATE packages SET label = ? WHERE id = ?", (label.strip() or None, row["id"])
        )
    return get_package(str(row["id"]))


@mcp.tool(annotations=DESTROY)
def delete_package(package: str) -> str:
    """Arrête le suivi d'un colis et efface son historique."""
    with db.get_conn() as conn:
        row = _find(conn, package)
        conn.execute("DELETE FROM packages WHERE id = ?", (row["id"],))
    return f"Colis {row['tracking_code']} supprimé."


class BearerAuth:
    """Refuse (401) toute requête dont l'en-tête Authorization n'est pas `Bearer <jeton>`."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            given = dict(scope["headers"]).get(b"authorization", b"")
            if not secrets.compare_digest(given, self.expected):
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"www-authenticate", b"Bearer"),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
                return
        await self.app(scope, receive, send)


def mount_mcp(app: FastAPI, token: str):
    """Ajoute la route /mcp à l'appli ; renvoie le contexte à ouvrir dans son lifespan."""
    # Pas de contrôle Host/Origin : l'appli tourne derrière Traefik et le jeton suffit.
    sub = mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    for route in sub.routes:
        assert isinstance(route, Route)
        app.router.routes.append(Route(route.path, endpoint=BearerAuth(route.endpoint, token)))

    @asynccontextmanager
    async def running():
        async with mcp.session_manager.run():
            yield

    return running
