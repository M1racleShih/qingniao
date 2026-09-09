"""Foreground gateway process: loopback bind, guard, discovery, cleanup."""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

import uvicorn

from . import errors, state
from .app import build_app
from .config import ConfigStore
from .gateway import Gateway
from .tokens import new_control_token, token_hash


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 2


def run_serve(state_dir: Path, port: int) -> int:
    from . import transactions

    state.prepare_state_dir(state_dir)
    lock = state.GatewayLock(state_dir)
    try:
        lock.acquire()
    except errors.ApiError as exc:
        return _fail(exc.message)

    # Recovery runs before the gateway accepts anything: open or
    # unconfirmed import transactions are resolved (or startup refuses)
    # while this process alone owns the state directory.
    try:
        transactions.recover_open_transactions(state_dir)
    except errors.ApiError as exc:
        lock.release()
        return _fail(exc.message)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
        port = sock.getsockname()[1]
    except OSError as exc:
        sock.close()
        lock.release()
        return _fail(f"cannot bind 127.0.0.1:{port}: {exc}")

    store = ConfigStore(state_dir / state.CONFIG_NAME)
    try:
        config = store.load()
    except errors.ApiError as exc:
        sock.close()
        lock.release()
        return _fail(exc.message)

    control_token = new_control_token()
    gateway = Gateway(config, store)
    pid = os.getpid()

    async def _startup() -> None:
        state.write_discovery(state_dir, port=port, pid=pid, control_token=control_token)
        # Readiness is announced only after the lifespan startup has
        # succeeded and the discovery file exists — never before.
        print(f"qingniao gateway listening on http://127.0.0.1:{port}")
        print(f"state directory: {state_dir}")
        print(f"discovery file: {state_dir / state.DISCOVERY_NAME} (0600, includes control token)")

    async def _shutdown() -> None:
        state.remove_discovery(state_dir, pid)

    app = build_app(
        gateway,
        admin_token_hash=token_hash(control_token),
        startup_hook=_startup,
        shutdown_hook=_shutdown,
    )

    fd = sock.detach()
    server_config = uvicorn.Config(
        app,
        fd=fd,
        log_level="warning",
        access_log=False,
        lifespan="on",
    )
    server = uvicorn.Server(server_config)
    try:
        server.run()
    finally:
        state.remove_discovery(state_dir, pid)
        lock.release()
    return 0
