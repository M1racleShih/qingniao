"""State directory handling: discovery file and single-gateway guard.

The gateway runs in the foreground. A per-state-directory advisory lock
prevents two gateways from sharing one state directory; the runtime
discovery file (0600) carries the loopback port, PID and control token and
only ever belongs to the process currently holding the lock.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
from pathlib import Path

from . import errors
from .config import atomic_write_json

LOCK_NAME = "gateway.lock"
DISCOVERY_NAME = "gateway.json"
CONFIG_NAME = "config.json"


def default_state_dir() -> Path:
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg) / "qingniao"
    return Path.home() / ".local" / "state" / "qingniao"


def prepare_state_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


class GatewayLock:
    """Advisory single-gateway guard for one state directory."""

    def __init__(self, state_dir: Path):
        self.path = state_dir / LOCK_NAME
        self._fd: int | None = None

    def acquire(self) -> None:
        self._fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(self._fd)
            self._fd = None
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise errors.ApiError(
                    2,
                    errors.GATEWAY_LOCKED,
                    f"another gateway is already running with state directory {self.path.parent}",
                ) from exc
            raise
        os.write(self._fd, b"")
        os.ftruncate(self._fd, 0)
        os.write(self._fd, str(os.getpid()).encode())

    def release(self) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None


def write_discovery(state_dir: Path, *, port: int, pid: int, control_token: str) -> None:
    atomic_write_json(
        state_dir / DISCOVERY_NAME,
        {
            "version": 1,
            "port": port,
            "pid": pid,
            "control_token": control_token,
        },
    )


def read_discovery(state_dir: Path) -> dict | None:
    path = state_dir / DISCOVERY_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if not isinstance(data.get("port"), int) or not isinstance(data.get("control_token"), str):
        return None
    return data


def remove_discovery(state_dir: Path, pid: int) -> None:
    """Remove the discovery file only when it still belongs to this PID."""
    path = state_dir / DISCOVERY_NAME
    data = read_discovery(state_dir)
    if data is not None and data.get("pid") == pid:
        try:
            path.unlink()
        except OSError:
            pass
