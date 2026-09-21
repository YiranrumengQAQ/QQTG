"""FastAPI application: admin panel, JSON API and the OneBot reverse WebSocket."""
from __future__ import annotations

import asyncio
import hmac
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..core.app import BridgeApp
from ..logsys import get_logger
from ..models import PLATFORM_QQ
from .api import build_router

log = get_logger("system")
STATIC_DIR = Path(__file__).parent / "static"


def create_app(bridge: BridgeApp) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await bridge.start()
        try:
            yield
        finally:
            await bridge.stop()

    app = FastAPI(title="QQTG Bridge", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.bridge = bridge
    app.include_router(build_router(bridge))

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException):
        return JSONResponse({"ok": False, "error": exc.detail}, status_code=exc.status_code)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Cache-Control", "no-store" if request.url.path.startswith("/api") else "no-cache")
        return response

    # ---- OneBot v11 reverse WebSocket -------------------------------------
    async def onebot_ws(ws: WebSocket) -> None:
        adapter = bridge.engine.adapter(PLATFORM_QQ) if bridge.engine else None
        if adapter is None or getattr(adapter, "mode", "") != "reverse":
            await ws.close(code=1008, reason="reverse websocket not enabled")
            return
        expected = getattr(adapter, "access_token", "")
        if expected:
            supplied = ""
            auth = ws.headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                supplied = auth[7:].strip()
            elif auth.lower().startswith("token "):
                supplied = auth[6:].strip()
            supplied = supplied or ws.query_params.get("access_token", "")
            if not hmac.compare_digest(supplied, expected):
                log.warning("OneBot 反向连接认证失败 (%s)", ws.client.host if ws.client else "?")
                await ws.close(code=1008, reason="unauthorized")
                return
        await ws.accept()
        log.info("OneBot 反向 WebSocket 已连接 (%s)", ws.client.host if ws.client else "?")
        try:
            await adapter.attach_reverse(ws)  # type: ignore[attr-defined]
        except Exception as exc:
            log.debug("reverse ws closed: %s", exc)

    for path in ("/onebot/v11/ws", "/onebot/v11/ws/", "/onebot/v11", "/onebot/v11/", "/ws", "/"):
        app.add_api_websocket_route(path, onebot_ws)

    # ---- static SPA ---------------------------------------------------------
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        ok = await bridge.db.ping()
        return JSONResponse({"ok": ok, "version": __version__}, status_code=200 if ok else 503)

    return app


async def serve(bridge: BridgeApp) -> None:
    import uvicorn

    app = create_app(bridge)
    config = uvicorn.Config(app, host=bridge.cfg.bind, port=bridge.cfg.port, log_level="warning", proxy_headers=True,
                            forwarded_allow_ips="*", ws_max_size=64 * 1024 * 1024, timeout_graceful_shutdown=10)
    server = uvicorn.Server(config)
    log.info("Web 面板监听 http://%s:%d", bridge.cfg.bind, bridge.cfg.port)
    await server.serve()


def run(bridge: BridgeApp) -> None:
    try:
        asyncio.run(serve(bridge))
    except KeyboardInterrupt:  # pragma: no cover
        pass
