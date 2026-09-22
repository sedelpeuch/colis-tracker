from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime

import httpx
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db
from .barcode import generate_barcode_png
from .laposte_client import fetch_parcel
from .mail_watcher import mail_watcher_loop
from .poller import poller_loop

templates = Jinja2Templates(directory="app/templates")


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


app = FastAPI(title="Colis Tracker", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")


POSTMARK_LABELS = {
    "registered": "Enregistré",
    "in_transit": "En transit",
    "out_for_delivery": "En livraison",
    "at_pickup_point": "En point relais",
    "delivered": "Livré",
    "returning": "Retour",
    "problem": "Anomalie",
    "unknown": "Inconnu",
}


def _grouped_packages():
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM packages ORDER BY updated_at DESC, created_at DESC"
        ).fetchall()
    in_progress = [r for r in rows if not r["delivered"] and not r["error"]]
    delivered = [r for r in rows if r["delivered"]]
    errored = [r for r in rows if not r["delivered"] and r["error"]]
    return {
        "in_progress": in_progress,
        "delivered": delivered,
        "errored": errored,
        "postmark_labels": POSTMARK_LABELS,
    }


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html", _grouped_packages())


@app.get("/packages/table", response_class=HTMLResponse)
async def packages_table(request: Request):
    return templates.TemplateResponse(request, "partials/table.html", _grouped_packages())


@app.post("/packages")
async def add_package(tracking_code: str = Form(...), label: str = Form("")):
    tracking_code = tracking_code.strip().upper()
    if not tracking_code:
        raise HTTPException(status_code=400, detail="tracking_code requis")

    now = datetime.now(UTC).isoformat()
    with db.get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO packages (tracking_code, label, created_at, next_poll_at) VALUES (?, ?, ?, ?)",
            (tracking_code, label.strip() or None, now, now),
        )

    # Poll immédiat pour un premier retour sans attendre le prochain tick.
    async with httpx.AsyncClient() as client:
        try:
            await fetch_parcel(client, tracking_code)
        except Exception:
            pass
    from .poller import poll_due_packages

    await poll_due_packages()

    return RedirectResponse(url="/", status_code=303)


@app.post("/packages/{package_id}/delete")
async def delete_package(package_id: int):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM packages WHERE id = ?", (package_id,))
    return RedirectResponse(url="/", status_code=303)


@app.post("/packages/{package_id}/label")
async def rename_package(package_id: int, label: str = Form("")):
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE packages SET label = ? WHERE id = ?", (label.strip() or None, package_id)
        )
    return RedirectResponse(url=f"/packages/{package_id}", status_code=303)


@app.get("/packages/{package_id}/barcode.png")
async def package_barcode(package_id: int):
    with db.get_conn() as conn:
        package = conn.execute(
            "SELECT tracking_code FROM packages WHERE id = ?", (package_id,)
        ).fetchone()
        if package is None:
            raise HTTPException(status_code=404)
    png = generate_barcode_png(package["tracking_code"])
    return Response(content=png, media_type="image/png")


@app.get("/packages/{package_id}", response_class=HTMLResponse)
async def package_detail(request: Request, package_id: int):
    with db.get_conn() as conn:
        package = conn.execute("SELECT * FROM packages WHERE id = ?", (package_id,)).fetchone()
        if package is None:
            raise HTTPException(status_code=404)
        events = conn.execute(
            "SELECT * FROM events WHERE package_id = ? ORDER BY created_at DESC", (package_id,)
        ).fetchall()
    return templates.TemplateResponse(
        request, "detail.html", {"package": package, "events": events}
    )
