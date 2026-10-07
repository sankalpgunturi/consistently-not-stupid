"""Dashboard and the small JSON API the page polls.

Mutating routes require the token from GET /api/state. A browser on another
origin is rejected. The server still binds to localhost. There is no login
and no route that sends an order.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from cst.engine import Engine

DASHBOARD = Path(__file__).resolve().parents[2] / "dashboard"
_LOCAL = {"127.0.0.1", "localhost"}


class PauseIn(BaseModel):
    paused: bool


class BlockIn(BaseModel):
    key: str


class CloseIn(BaseModel):
    id: str


class PairIn(BaseModel):
    pair_id: str
    note: str


class KnobIn(BaseModel):
    key: str


def create_app(engine: Engine, start_loop: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if start_loop:
            engine.start()
        yield
        engine.stop()

    app = FastAPI(title="The Common Sense Trade", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.engine = engine
    if DASHBOARD.exists():
        app.mount("/static", StaticFiles(directory=DASHBOARD), name="static")

    @app.middleware("http")
    async def csrf(request, call_next):
        if request.method == "POST" and request.url.path.startswith("/api/"):
            token = request.headers.get("x-csrf-token")
            if not token or token != engine.store.csrf():
                return JSONResponse({"error": "Missing or bad CSRF token."}, status_code=403)
            origin = request.headers.get("origin")
            if origin:
                host = urlparse(origin).hostname
                if host not in _LOCAL:
                    return JSONResponse({"error": "Foreign origin."}, status_code=403)
        return await call_next(request)

    @app.get("/")
    def index():
        return FileResponse(DASHBOARD / "index.html")

    @app.get("/api/health")
    def health():
        return {"ok": True, "mode": "paper", "live": "unavailable", "name": "The Common Sense Trade"}

    @app.get("/api/state")
    def state():
        return engine.snapshot()

    @app.post("/api/scan")
    def scan():
        result = engine.run_cycle()
        if result.get("status") == "busy":
            return JSONResponse({"status": "scanning"}, status_code=202)
        return result

    @app.post("/api/reset")
    def reset():
        return engine.reset()

    @app.post("/api/pause")
    def pause(body: PauseIn):
        return engine.set_pause(body.paused)

    @app.post("/api/block")
    def block(body: BlockIn):
        if not body.key.strip():
            return JSONResponse({"error": "A contract key is required."}, status_code=400)
        return engine.block_market(body.key.strip())

    @app.post("/api/close")
    def close(body: CloseIn):
        ok, detail = engine.close_position(body.id)
        if not ok:
            return JSONResponse({"error": detail}, status_code=409)
        return engine.snapshot()

    @app.post("/api/approve-pair")
    def approve_pair(body: PairIn):
        note = body.note.strip()
        if not body.pair_id.strip() or not note:
            return JSONResponse({"error": "A pair and a note on the resolution rules are required."}, status_code=400)
        return engine.approve_pair(body.pair_id.strip(), note)

    @app.post("/api/knob")
    def knob(body: KnobIn):
        state, error = engine.tighten(body.key)
        if error:
            return JSONResponse({"error": error, "state": state}, status_code=400)
        return state

    @app.websocket("/ws")
    async def ws(socket: WebSocket):
        await socket.accept()
        try:
            while True:
                await socket.send_json(engine.snapshot())
                await asyncio.sleep(1)
        except WebSocketDisconnect:
            return
        except Exception:
            return

    return app
