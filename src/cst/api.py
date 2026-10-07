"""Dashboard and the small JSON API the page polls.

Mutating routes require the token from GET /api/state. Origin and Host have
to be localhost or the host the desk was bound to, including the state read
and the snapshot socket. There is no login. Live orders are sent only after
POST /api/live approves a whole-dollar amount from $1 to $5,000.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from cst.engine import Engine

# Shipped inside the package so `pip install` can serve the page.
DASHBOARD = Path(__file__).resolve().parent / "dashboard"
_LOCAL = {"127.0.0.1", "localhost", "::1"}


class PauseIn(BaseModel):
    paused: bool


class BlockIn(BaseModel):
    key: str


class CloseIn(BaseModel):
    id: str


class KnobIn(BaseModel):
    key: str


class LiveIn(BaseModel):
    amount: float


def _hostname(value: str | None) -> str:
    """Host from a bind address, a Host header, or an origin.

    An unbracketed IPv6 literal has more than one colon, so splitting on the
    first colon would keep only the first group. ``--host 2001:db8::5`` has
    to match a browser Host of ``[2001:db8::5]:8000``.
    """
    if not value:
        return ""
    text = value.strip().lower()
    if text.startswith("["):
        end = text.find("]")
        return text[1:end] if end > 1 else ""
    if text.count(":") > 1:
        try:
            ipaddress.ip_address(text)
            return text
        except ValueError:
            host, _, port = text.rpartition(":")
            if port.isdigit():
                try:
                    ipaddress.ip_address(host)
                    return host
                except ValueError:
                    pass
            return text
    return text.split(":")[0]


_WILDCARD = {"0.0.0.0", "::", "[::]"}


def allowed_names(bind_host: str) -> set[str]:
    """Loopback, plus the concrete host the process was bound to.

    ``0.0.0.0`` is not a name a browser sends. A wildcard bind trusts only
    loopback. Pass ``--host 192.168.1.5`` to open the page at that address.
    """
    names = set(_LOCAL)
    host = _hostname(bind_host)
    if host and host not in _WILDCARD:
        names.add(host)
    return names


def origin_allowed(origin: str | None, bind_host: str) -> bool:
    """A missing Origin is left to the Host check. A present one must be a name we bound."""
    if not origin:
        return True
    origin_host = (urlparse(origin).hostname or "").lower()
    return bool(origin_host) and origin_host in allowed_names(bind_host)


def host_allowed(host_header: str | None, bind_host: str) -> bool:
    """The Host header has to be the bind host, not whatever Origin agrees with.

    A DNS-rebinding page can make Origin and Host both say the attacker's name
    while the packet arrives on the local interface.
    """
    return _hostname(host_header) in allowed_names(bind_host)


def create_app(engine: Engine, start_loop: bool = True, bind_host: str | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if start_loop:
            engine.start()
        yield
        engine.stop()

    app = FastAPI(title="Consistently Not Stupid", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.engine = engine
    with engine.store.lock:
        engine.store.conn.execute("CREATE TABLE IF NOT EXISTS remote_commands (id TEXT PRIMARY KEY, received_at TEXT DEFAULT CURRENT_TIMESTAMP)")
        engine.store.conn.commit()
    app.state.bind_host = bind_host or engine.settings.host or "127.0.0.1"
    if DASHBOARD.exists():
        app.mount("/static", StaticFiles(directory=DASHBOARD), name="static")

    @app.middleware("http")
    async def csrf(request, call_next):
        if request.url.path.startswith("/api/"):
            bound = app.state.bind_host
            if not host_allowed(request.headers.get("host"), bound):
                return JSONResponse({"error": "Foreign host."}, status_code=403)
            if not origin_allowed(request.headers.get("origin"), bound):
                return JSONResponse({"error": "Foreign origin."}, status_code=403)
            if request.method == "POST":
                token = request.headers.get("x-csrf-token")
                if not token or token != engine.store.csrf():
                    return JSONResponse({"error": "Missing or bad CSRF token."}, status_code=403)
                command_id = request.headers.get("x-command-id")
                if command_id:
                    if len(command_id) > 128:
                        return JSONResponse({"error": "Invalid command id."}, status_code=400)
                    # Reserve before execution. A lost acknowledgement must never
                    # repeat reset, manual close or a knob tightening after restart.
                    with engine.store.lock:
                        inserted = engine.store.conn.execute("INSERT OR IGNORE INTO remote_commands(id) VALUES (?)", (command_id,)).rowcount
                        engine.store.conn.commit()
                    if not inserted:
                        return JSONResponse({"status": "already accepted"}, status_code=202)
        return await call_next(request)

    @app.get("/")
    def index():
        page = DASHBOARD / "index.html"
        if not page.is_file():
            return JSONResponse({"error": "Dashboard files are not installed."}, status_code=500)
        html = page.read_text()
        for asset in ("app.js", "styles.css"):
            digest = hashlib.sha256((DASHBOARD / asset).read_bytes()).hexdigest()[:16]
            html = html.replace(f"/static/{asset}", f"/static/{asset}?v={digest}")
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    @app.get("/api/health")
    def health():
        mode = engine.store.trading_mode()
        return {
            "ok": True,
            "mode": mode,
            "live": "on" if mode == "live" else "unavailable",
            "name": "Consistently Not Stupid",
        }

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
        if engine.store.trading_mode() == "live":
            return JSONResponse({"error": "Reset stays off while live trading is on."}, status_code=409)
        return engine.reset()

    @app.post("/api/live")
    def live(body: LiveIn):
        state, error = engine.arm_live(body.amount)
        if error:
            return JSONResponse({"error": error, "state": state}, status_code=400)
        return state

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

    @app.post("/api/knob")
    def knob(body: KnobIn):
        state, error = engine.tighten(body.key)
        if error:
            return JSONResponse({"error": error, "state": state}, status_code=400)
        return state

    @app.websocket("/ws")
    async def ws(socket: WebSocket):
        bound = app.state.bind_host
        if not host_allowed(socket.headers.get("host"), bound) or not origin_allowed(socket.headers.get("origin"), bound):
            await socket.close(code=1008)
            return
        await socket.accept()
        last = None
        try:
            while True:
                payload = await asyncio.to_thread(engine.snapshot)
                body = dict(payload)
                body.pop("server_time", None)
                encoded = json.dumps(body, sort_keys=True, default=str)
                if encoded != last:
                    await socket.send_text(json.dumps(payload, default=str))
                    last = encoded
                await asyncio.sleep(1)
        except WebSocketDisconnect:
            return
        except Exception:
            return

    return app
