"""FastAPI application factory. The backend is the ONLY RPC client; the
browser never talks to the node directly."""

import os
import pathlib

from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.deps import AppState, create_app_state
from app.rpc import RPCNotAllowed


class SpaStaticApp(StaticFiles):
    """SPA fallback: non-/api paths that do not match a file serve index.html."""

    async def get_response(self, path: str, scope):
        try:
            response = await super().get_response(path, scope)
        except Exception:
            response = None
        if response is None or response.status_code == 404:
            if scope.get("path", "").startswith("/api/"):
                return response
            response = await super().get_response("index.html", scope)
        return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: initialize app state, then start the chain monitor.
    if not hasattr(app.state, "app_state") or app.state.app_state is None:
        app.state.app_state = create_app_state()
    from app.monitor import start_monitor, stop_monitor
    state: AppState = app.state.app_state
    start_monitor(state.settings, state.rpc)
    yield
    # Shutdown: stop the monitor.
    await stop_monitor()


def create_app(state: AppState | None = None) -> FastAPI:
    app = FastAPI(title="B3 Hive", docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)

    # If a state was injected (tests), set it before lifespan runs.
    if state is not None:
        app.state.app_state = state

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        # CSP: no external origins — the SPA is fully self-hosted (Alpine,
        # QR lib served same-origin). 'unsafe-inline' is required by
        # Alpine.js in-page templates and inline styles.
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; "
            "form-action 'self'"
        )
        return response

    from app.routers import auth, chain, wallet, wallet_extra, batch, alerts
    app.include_router(auth.router)
    app.include_router(chain.router)
    app.include_router(wallet.router)
    app.include_router(wallet_extra.router)
    app.include_router(batch.router)
    app.include_router(alerts.router)

    @app.get("/api/health")
    async def health():
        return {"ok": True}

    @app.exception_handler(RPCNotAllowed)
    async def _not_allowed(request: Request, exc: RPCNotAllowed):
        return JSONResponse(status_code=403, content={"detail": f"RPC not allowed: {exc}"})

    frontend = pathlib.Path(os.environ.get(
        "FRONTEND_DIR",
        str(pathlib.Path(__file__).resolve().parent.parent.parent / "frontend"),
    ))
    if (frontend / "index.html").is_file():
        app.mount("/", SpaStaticApp(directory=str(frontend)), name="frontend")

    return app


app = create_app()


def run() -> None:
    import uvicorn
    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.environ.get("WEB_PORT", "8080")))
