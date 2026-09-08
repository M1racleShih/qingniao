"""Root ASGI application: control routes, proxy routes, shared client."""

from __future__ import annotations

from contextlib import asynccontextmanager

import httpx
from starlette.applications import Starlette
from starlette.routing import Mount

from .control_api import build_control_routes
from .gateway import Gateway
from .proxy_api import build_proxy_routes

DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0)


def build_app(gateway: Gateway, *, admin_token_hash: str, startup_hook=None, shutdown_hook=None) -> Starlette:
    @asynccontextmanager
    async def lifespan(app: Starlette):
        app.state.http_client = httpx.AsyncClient(
            follow_redirects=False,
            timeout=DEFAULT_TIMEOUT,
        )
        try:
            if startup_hook is not None:
                await startup_hook()
            yield
        finally:
            if shutdown_hook is not None:
                await shutdown_hook()
            await app.state.http_client.aclose()

    app = Starlette(
        routes=[
            Mount("/control/v1", routes=build_control_routes()),
            Mount("/", routes=build_proxy_routes()),
        ],
        lifespan=lifespan,
    )
    app.state.gateway = gateway
    app.state.admin_token_hash = admin_token_hash
    return app
