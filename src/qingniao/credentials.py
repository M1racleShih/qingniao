"""Private, immutable credential storage inside the state directory.

Secrets live in ``<state-dir>/credentials`` (0700) as one 0600 regular file
per immutable version id. Ids are always generated here (``cred_`` plus 32
hex characters) and never derived from user input, so they cannot carry path
traversal. Every access opens the private directory with O_NOFOLLOW and each
file through a directory handle with O_NOFOLLOW, then verifies owner, mode
and file type before any byte is read. There is deliberately no update
operation: rotating a credential creates a new version id.

This is a plain local file store with restrictive permissions, not an
encrypted vault; anyone who can read the user's files or backups can read
the secrets.
"""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

from . import errors
from .tokens import CREDENTIAL_ID_RE, new_credential_id

CREDENTIALS_DIR_NAME = "credentials"

_DIR_MODE = 0o700
_FILE_MODE = 0o600


def _invalid(message: str) -> errors.ApiError:
    return errors.ApiError(400, errors.CREDENTIAL_INVALID, message)


def _unreadable(reason: str) -> errors.ApiError:
    return errors.ApiError(
        500,
        errors.CREDENTIAL_UNREADABLE,
        f"private credential storage is not usable ({reason}); "
        "check the ownership and permissions of the credentials directory in the "
        "gateway state directory",
    )


class CredentialStore:
    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / CREDENTIALS_DIR_NAME

    # ------------------------------------------------------------ helpers

    def _validated_id(self, credential_id: object) -> str:
        if not isinstance(credential_id, str) or not CREDENTIAL_ID_RE.match(credential_id):
            raise _invalid(
                "credential id must be of the form cred_<hex> as issued by qingniao"
            )
        return credential_id

    def _open_dir_checked(self, *, create: bool) -> int:
        """Open the private directory refusing symlinks and unsafe modes."""
        if create and not os.path.lexists(self.path):
            try:
                self.path.mkdir(mode=_DIR_MODE)
                parent = os.open(str(self.state_dir), os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(parent)
                except OSError:
                    pass
                finally:
                    os.close(parent)
            except OSError as exc:
                raise _unreadable(f"cannot create: {errno.errorcode.get(exc.errno, 'OSError')}") from exc
        try:
            fd = os.open(str(self.path), os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise _unreadable("credentials path is a symlink or not a directory") from exc
            if exc.errno == errno.ENOENT:
                raise _unreadable("credentials directory does not exist") from exc
            raise _unreadable(f"cannot open: {errno.errorcode.get(exc.errno, 'OSError')}") from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISDIR(info.st_mode):
                raise _unreadable("credentials path is not a directory")
            if info.st_uid != os.geteuid():
                raise _unreadable("credentials directory is owned by another user")
            if stat.S_IMODE(info.st_mode) & 0o077:
                raise _unreadable("credentials directory permissions are wider than 0700")
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _open_secret_checked(self, dir_fd: int, cred_id: str) -> int:
        # O_NONBLOCK so a substituted FIFO cannot hang the gateway.
        try:
            fd = os.open(cred_id, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                raise errors.ApiError(
                    500,
                    errors.CREDENTIAL_MISSING,
                    f"private credential {cred_id} is not present in the store",
                    credential_id=cred_id,
                ) from exc
            if exc.errno == errno.ELOOP:
                raise _unreadable("credential file is a symlink") from exc
            raise _unreadable(
                f"cannot open: {errno.errorcode.get(exc.errno, 'OSError')}"
            ) from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise _unreadable("credential file is not a regular file")
            if info.st_uid != os.geteuid():
                raise _unreadable("credential file is owned by another user")
            if stat.S_IMODE(info.st_mode) & 0o077:
                raise _unreadable("credential file permissions are wider than 0600")
        except BaseException:
            os.close(fd)
            raise
        return fd

    # ------------------------------------------------------------ operations

    def create(self, secret: object) -> str:
        if not isinstance(secret, str) or not secret.strip():
            raise _invalid("credential value must be a non-empty string")
        dir_fd = self._open_dir_checked(create=True)
        cred_id = new_credential_id()
        try:
            try:
                fd = os.open(
                    cred_id,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    _FILE_MODE,
                    dir_fd=dir_fd,
                )
            except OSError as exc:
                raise _unreadable(
                    f"cannot create file: {errno.errorcode.get(exc.errno, 'OSError')}"
                ) from exc
            try:
                # fchmod, unlike the O_CREAT mode, is not masked by umask, so
                # the file is exactly 0600.
                os.fchmod(fd, _FILE_MODE)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(secret)
                    fh.flush()
                    os.fsync(fh.fileno())
            except OSError as exc:
                try:
                    os.unlink(cred_id, dir_fd=dir_fd)
                except OSError:
                    pass
                raise _unreadable(
                    f"cannot write: {errno.errorcode.get(exc.errno, 'OSError')}"
                ) from exc
            except BaseException:
                try:
                    os.unlink(cred_id, dir_fd=dir_fd)
                except OSError:
                    pass
                raise
        finally:
            _fsync_fd(dir_fd)
            os.close(dir_fd)
        return cred_id

    def read(self, credential_id: object) -> str:
        cred_id = self._validated_id(credential_id)
        if not os.path.lexists(self.path):
            raise errors.ApiError(
                500,
                errors.CREDENTIAL_MISSING,
                f"private credential {cred_id} is not present in the store",
                credential_id=cred_id,
            )
        dir_fd = self._open_dir_checked(create=False)
        try:
            fd = self._open_secret_checked(dir_fd, cred_id)
            try:
                with os.fdopen(fd, "rb") as fh:
                    raw = fh.read()
            except OSError as exc:
                raise _unreadable(
                    f"cannot read: {errno.errorcode.get(exc.errno, 'OSError')}"
                ) from exc
        finally:
            os.close(dir_fd)
        try:
            value = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _unreadable("credential file is not valid UTF-8 text") from exc
        if not value.strip():
            raise _unreadable("credential file is empty")
        return value

    def exists(self, credential_id: object) -> bool:
        if not isinstance(credential_id, str) or not CREDENTIAL_ID_RE.match(credential_id):
            return False
        cred_id = credential_id
        if not os.path.lexists(self.path):
            return False
        dir_fd = self._open_dir_checked(create=False)
        try:
            try:
                info = os.stat(cred_id, dir_fd=dir_fd, follow_symlinks=False)
                return stat.S_ISREG(info.st_mode)
            except FileNotFoundError:
                return False
        finally:
            os.close(dir_fd)

    def delete(self, credential_id: object) -> None:
        """Remove one version; used only for uncommitted staging cleanup."""
        if not isinstance(credential_id, str) or not CREDENTIAL_ID_RE.match(credential_id):
            return
        cred_id = credential_id
        if not self.path.exists():
            return
        dir_fd = self._open_dir_checked(create=False)
        try:
            try:
                os.unlink(cred_id, dir_fd=dir_fd)
                _fsync_fd(dir_fd)
            except FileNotFoundError:
                pass
        finally:
            os.close(dir_fd)


def _fsync_fd(fd: int) -> None:
    try:
        os.fsync(fd)
    except OSError:
        pass
