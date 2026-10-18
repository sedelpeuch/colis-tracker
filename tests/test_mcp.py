import asyncio
import contextlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db, mcp_server

TOKEN = "secret"
HEADERS = {"Accept": "application/json, text/event-stream"}


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "colis.db")
    db.init_db()
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO packages (tracking_code, label, status, delivered) VALUES ('6A111', 'Livres', 'in_transit', 0)"
        )
        conn.execute(
            "INSERT INTO packages (tracking_code, status, delivered) VALUES ('6A222', 'delivered', 1)"
        )
        conn.execute("INSERT INTO events (package_id, status) VALUES (1, 'registered')")


def call(name, **arguments):
    return asyncio.run(mcp_server.mcp.call_tool(name, arguments))


def structured(name, **arguments):
    result = call(name, **arguments)
    assert not result.is_error, result
    return result.structured_content["result"]


def test_list_packages_filters_by_state():
    assert len(structured("list_packages")) == 2
    assert [p["tracking_code"] for p in structured("list_packages", state="delivered")] == ["6A222"]
    in_progress = structured("list_packages", state="in_progress")
    assert [p["status_label"] for p in in_progress] == ["En transit"]


def test_get_package_by_id_or_code():
    by_id = call("get_package", package="1").structured_content
    by_code = call("get_package", package="6a111").structured_content
    assert by_id == by_code
    assert by_id["label"] == "Livres"
    assert [e["status"] for e in by_id["events"]] == ["registered"]


def test_unknown_package_is_an_error():
    with pytest.raises(Exception, match="introuvable"):
        call("get_package", package="nope")


def test_add_rename_delete(monkeypatch):
    async def fake_register(code, label):
        with db.get_conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO packages (tracking_code, label) VALUES (?, ?)", (code, label or None)
            )

    monkeypatch.setattr(mcp_server, "register_package", fake_register)
    added = call("add_package", tracking_code=" 8r333 ", label="Chaussures").structured_content
    assert added["tracking_code"] == "8R333"
    renamed = call("rename_package", package="8R333", label="Bottes").structured_content
    assert renamed["label"] == "Bottes"
    call("delete_package", package="8R333")
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM packages").fetchone()[0] == 2


def test_barcode_is_png():
    result = call("get_barcode", package="6A111")
    assert result.content[0].mime_type == "image/png"


@contextlib.contextmanager
def client():
    app = FastAPI()
    running = mcp_server.mount_mcp(app, TOKEN)

    @contextlib.asynccontextmanager
    async def lifespan(_):
        async with running():
            yield

    app.router.lifespan_context = lifespan
    with TestClient(app) as c:
        yield c


def test_http_requires_bearer_token():
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    with client() as c:
        assert c.post("/mcp", json=body, headers=HEADERS).status_code == 401
        bad = {**HEADERS, "Authorization": "Bearer nope"}
        assert c.post("/mcp", json=body, headers=bad).status_code == 401


def test_http_lists_tools_with_token():
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
    }
    headers = {**HEADERS, "Authorization": f"Bearer {TOKEN}"}
    with client() as c:
        assert c.post("/mcp", json=init, headers=headers).status_code == 200
        r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=headers)
    assert r.status_code == 200
    names = {t["name"] for t in r.json()["result"]["tools"]}
    assert names == {"list_packages", "get_package", "get_barcode", "add_package", "rename_package", "delete_package"}
