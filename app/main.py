from __future__ import annotations

import asyncio
import contextlib

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db
from .barcode import generate_barcode_png
from .mail_watcher import mail_watcher_loop
from .mcp_server import MCP_TOKEN, mount_mcp
from .packages import POSTMARK_LABELS, register_package
from .poller import poller_loop

templates = Jinja2Templates(directory="app/templates")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    poller_task = asyncio.create_task(poller_loop())
    mail_task = asyncio.create_task(mail_watcher_loop())
    async with mcp_running() if mcp_running else contextlib.nullcontext():
        yield
    poller_task.cancel()
    mail_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await poller_task
    with contextlib.suppress(asyncio.CancelledError):
        await mail_task


app = FastAPI(title="Colis Tracker", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
mcp_running = mount_mcp(app, MCP_TOKEN) if MCP_TOKEN else None


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
    await register_package(tracking_code, label)

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
        request,
        "detail.html",
        {"package": package, "events": events, "postmark_labels": POSTMARK_LABELS},
    )
