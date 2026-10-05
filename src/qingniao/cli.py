"""Qingniao command line interface."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import contextlib
import os
import sys

import httpx
import typer
from rich import markup
from rich.console import Console
from rich.table import Table
from typer.main import get_command

try:  # newer Typer vendors its own click; older Typer uses the standalone package
    from typer._click import exceptions as _click_exceptions
except ImportError:  # pragma: no cover
    import click.exceptions as _click_exceptions

from . import errors, state as state_mod
from .serve import run_serve

def _qing_version() -> str:
    """Report the package version: installed package metadata when the
    distribution is installed, otherwise the in-tree ``__version__``."""
    try:
        from importlib.metadata import version as _metadata_version

        return _metadata_version("qingniao")
    except Exception:
        from . import __version__ as _version

        return _version


app = typer.Typer(
    add_completion=False,
    help="Qingniao gateway control",
)


def _version_callback(ctx: typer.Context, param: object, value: Optional[bool]) -> None:
    if not value or ctx.resilient_parsing:
        return
    print(_qing_version())
    raise typer.Exit()


@app.callback()
def _qing_main(
    ctx: typer.Context,
    version: Optional[bool] = typer.Option(
        None,
        "--version",
        "-V",
        help="Show the version and exit",
        is_eager=True,
        callback=_version_callback,
    ),
) -> None:
    pass


config_app = typer.Typer(help="Shared connection configuration")
defaults_app = typer.Typer(help="Default model selection for new instances")
instance_app = typer.Typer(help="Instance operations")
route_app = typer.Typer(help="Per-instance route operations")

app.add_typer(config_app, name="config")
app.add_typer(defaults_app, name="defaults")
app.add_typer(instance_app, name="instance")
app.add_typer(route_app, name="route")

def _plain_output() -> bool:
    return bool(os.environ.get("NO_COLOR")) or os.environ.get("TERM") == "dumb"


# In plain mode the consoles render as non-terminals, so no escape codes
# (including bold) are emitted at all; normal terminals keep full styling.
stderr = Console(stderr=True, force_terminal=False if _plain_output() else None, highlight=False)
stdout = Console(force_terminal=False if _plain_output() else None, highlight=False)

CLI_TIMEOUT = 15.0


class CliError(Exception):
    """CLI failure with a stable machine code, a human message and optional
    structured details (never secret material)."""

    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


@contextlib.contextmanager
def structured_cli_errors(json_output: bool):
    """With --json, CLI failures are machine-identifiable JSON on stdout."""
    try:
        yield
    except typer.BadParameter as exc:
        if not json_output:
            raise
        _print_json({"ok": False, "error": {"code": "cli_error", "message": str(exc)}})
        raise typer.Exit(code=1) from exc
    except CliError as exc:
        if not json_output:
            stderr.print(f"error: {exc.message}")
        else:
            error: dict = {"code": exc.code, "message": exc.message}
            error.update(exc.details)
            _print_json({"ok": False, "error": error})
        raise typer.Exit(code=1) from exc


@contextlib.contextmanager
def structured_catalog_errors(json_output: bool):
    """Entry-level catalog commands share the human/agent error contract.

    Usage errors (invalid arguments: bad values, mutually exclusive or
    missing sources) exit 2 in both modes with the stable
    ``invalid_argument`` code and structured ``errors[]`` detail;
    expected failures keep exit 1 with their own stable code.
    Parser-level missing options stay click errors (exit 2, ``cli_error``)
    handled by ``main()``.
    """
    try:
        yield
    except typer.BadParameter as exc:
        # Defensive: any remaining function-raised usage error stays a usage
        # error (exit 2) with the invalid_argument code.
        if not json_output:
            raise
        _print_json(
            {
                "ok": False,
                "error": {
                    "code": "invalid_argument",
                    "message": str(exc),
                    "errors": [{"path": "arguments", "message": str(exc)}],
                },
            }
        )
        raise typer.Exit(code=2) from exc
    except CliError as exc:
        if exc.code == errors.INVALID_ARGUMENT:
            # Invalid user-supplied arguments are usage errors: exit 2 in
            # every mode (human mode keeps click's usage rendering).
            if not json_output:
                raise typer.BadParameter(exc.message) from exc
            error: dict = {"code": exc.code, "message": exc.message}
            error.update(exc.details)
            _print_json({"ok": False, "error": error})
            raise typer.Exit(code=2) from exc
        if not json_output:
            stderr.print(f"error: {exc.message}")
        else:
            error = {"code": exc.code, "message": exc.message}
            error.update(exc.details)
            _print_json({"ok": False, "error": error})
        raise typer.Exit(code=1) from exc


def _state_dir(state_dir: Optional[Path]) -> Path:
    return Path(state_dir) if state_dir is not None else state_mod.default_state_dir()


def _endpoint(state_dir: Optional[Path]) -> tuple[str, str]:
    directory = _state_dir(state_dir)
    discovery = state_mod.read_discovery(directory)
    if discovery is None:
        raise typer.BadParameter(
            f"no running gateway found in state directory {directory}; start one with 'qing serve'"
        )
    port = discovery["port"]
    token = discovery["control_token"]
    return f"http://127.0.0.1:{port}", token


def _connection_failure(context: str, exc: Exception) -> CliError:
    return CliError(
        "gateway_unreachable",
        f"cannot reach gateway ({context}: {type(exc).__name__}); is 'qing serve' running? "
        "Any intended change remains unconfirmed.",
    )


def _call(state_dir: Optional[Path], method: str, path: str, *, timeout: Optional[float] = None, unconfirmed_action: Optional[str] = None, **kwargs) -> httpx.Response:
    base_url, token = _endpoint(state_dir)
    headers = kwargs.pop("headers", {})
    headers["authorization"] = f"Bearer {token}"
    effective_timeout = timeout if timeout is not None else CLI_TIMEOUT
    try:
        # trust_env=False: never send the control token through an ambient proxy.
        with httpx.Client(
            base_url=base_url, headers=headers, timeout=effective_timeout, trust_env=False
        ) as client:
            return client.request(method, path, **kwargs)
    except httpx.TimeoutException as exc:
        if unconfirmed_action is not None:
            raise CliError(
                "unconfirmed",
                f"gateway did not confirm {unconfirmed_action} within {effective_timeout}s; "
                "effect unconfirmed — read the target's current status before retrying",
            ) from exc
        raise _connection_failure(f"request to {path}", exc) from exc
    except httpx.HTTPError as exc:
        raise _connection_failure(f"request to {path}", exc) from exc


def _check(response: httpx.Response) -> dict:
    if response.status_code >= 400:
        try:
            payload = response.json()
        except ValueError:
            payload = {"error": {"code": "http_error", "message": response.text[:200]}}
        err = payload.get("error", payload)
        raise typer.BadParameter(f"{err.get('code', 'error')}: {err.get('message', response.text[:200])}")
    try:
        return response.json()
    except ValueError:
        return {}


def _require_applied(payload: dict, context: str) -> dict:
    """Only report success on a well-formed applied acknowledgement."""
    if not isinstance(payload, dict) or payload.get("applied") is not True:
        raise typer.BadParameter(f"gateway response for {context} was not a valid applied acknowledgement; nothing is reported as applied")
    if not isinstance(payload.get("revision"), int) or isinstance(payload.get("revision"), bool):
        raise typer.BadParameter(f"gateway response for {context} is missing the applied revision; nothing is reported as applied")
    return payload


def _print_json(payload: dict) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


JsonFlag = typer.Option(False, "--json", help="machine-readable JSON output")
StateDirOpt = typer.Option(None, "--state-dir", help="gateway state directory")


# ------------------------------------------------------------ catalog CRUD
#
# Entry-level catalog management (providers, credentials, models) for
# humans and agents alike: every command is non-interactive, accepts
# --json with a stable structure, distinguishes failure by stable machine
# codes and exit codes 0/1/2, and mutating commands preview the minimal
# change with --dry-run before any write. All edits are read-modify-write
# transactions through the running gateway (GET current config -> change
# exactly one entry -> conditional PUT with expected_generation); there is
# no offline write and no new control endpoint.

provider_app = typer.Typer(
    help=(
        "Manage provider entries (add/list/show/set/rm) without rewriting "
        "the whole configuration.\n\n"
        "Examples:\n"
        "  qing provider add my-provider --base-url https://api.example.com "
        "--auth bearer --credential-env MY_TOKEN\n"
        "  qing provider add p3 --base-url https://api.example.com "
        "--auth x-api-key --credential-id shared-key\n"
        "  qing provider rm my-provider\n"
        "\n"
        "All commands are non-interactive and accept --json; mutating "
        "commands accept --dry-run. Changes are applied by the running "
        "gateway and reported only when confirmed."
    )
)
credential_app = typer.Typer(
    help=(
        "Manage named credential sources (list/show/add/rm) without "
        "rewriting the whole configuration.\n\n"
        "Examples:\n"
        "  qing credential add shared-key --env MY_TOKEN\n"
        "  qing credential add --from-file /path/to/token   # private store\n"
        "  qing credential add --from-stdin                 # private store\n"
        "  qing credential rm shared-key\n"
        "\n"
        "Credential values are only accepted on --from-stdin or --from-file "
        "(never as command-line arguments) and never appear in output, "
        "errors or logs. show/list return metadata only. All commands are "
        "non-interactive and accept --json; mutating commands accept "
        "--dry-run."
    )
)
model_app = typer.Typer(
    help=(
        "Manage model entries (add/list/show/set/rm) without rewriting the "
        "whole configuration.\n\n"
        "Examples:\n"
        "  qing model add my-model --provider my-provider "
        "--upstream-model vendor/model\n"
        "  qing model rm my-model\n"
        "\n"
        "All commands are non-interactive and accept --json; mutating "
        "commands accept --dry-run. Changes are applied by the running "
        "gateway and reported only when confirmed."
    )
)

app.add_typer(provider_app, name="provider")
app.add_typer(credential_app, name="credential")
app.add_typer(model_app, name="model")


def _check_cr(response: httpx.Response) -> dict:
    """Like _check, but preserves the gateway's stable machine code and
    details so catalog commands can surface them verbatim."""
    if response.status_code >= 400:
        try:
            payload = response.json()
        except ValueError:
            payload = {"error": {"code": "http_error", "message": response.text[:200]}}
        err = payload.get("error") if isinstance(payload, dict) else None
        if not isinstance(err, dict):
            err = {"code": "http_error", "message": str(payload)[:200]}
        details = {k: v for k, v in err.items() if k not in ("code", "message")}
        raise CliError(
            err.get("code") or "http_error", err.get("message") or response.text[:200], **details
        )
    try:
        return response.json()
    except ValueError:
        return {}


def _get_config(state_dir: Optional[Path]) -> tuple[dict, int]:
    payload = _check_cr(_call(state_dir, "GET", "/control/v1/config"))
    config = payload.get("config")
    generation = payload.get("generation")
    if not isinstance(config, dict):
        raise CliError(errors.INVALID_CONFIG, "the gateway returned a malformed configuration")
    if isinstance(generation, bool) or not isinstance(generation, int):
        raise CliError(errors.INVALID_CONFIG, "the gateway returned no readable generation")
    return config, generation


def _put_config(
    state_dir: Optional[Path], config: dict, expected_generation: int, context: str
) -> dict:
    params = (
        {"expected_generation": expected_generation}
        if isinstance(expected_generation, int)
        else {}
    )
    return _require_applied(
        _check_cr(
            _call(
                state_dir,
                "PUT",
                "/control/v1/config",
                params=params,
                json=config,
                unconfirmed_action=f"the {context}",
            )
        ),
        context,
    )


def _local_validate(config: dict, context: str) -> None:
    from .catalog import validate_edited_config

    problems = validate_edited_config(config)
    if problems:
        raise CliError(
            errors.INVALID_ARGUMENT,
            f"{context} is invalid: {problems[0][0]}: {problems[0][1]}",
            errors=[{"path": p, "message": m} for p, m in problems],
        )


def _render_ok(payload: dict) -> dict:
    return {
        "ok": True,
        "applied": payload.get("applied") is True,
        "revision": payload.get("revision"),
        "generation": payload.get("generation"),
    }


def _catalog_commit(
    state_dir: Optional[Path],
    config: dict,
    expected_generation: int,
    context: str,
    dry_run: bool,
    json_output: bool,
    changes: dict,
) -> dict | None:
    """Commit the edited config or print a zero-write dry-run preview.

    Returns the applied payload (None after a dry-run preview). A dry run
    only reads the current config; it never writes files and never calls a
    change endpoint."""
    if dry_run:
        payload = {
            "ok": True,
            "dry_run": True,
            "generation": expected_generation,
            "changes": changes,
        }
        if json_output:
            _print_json(payload)
        else:
            for line in changes.get("lines", []):
                stdout.print(line, markup=False)
            stdout.print(
                "dry run: nothing was written and no change interface was called",
                markup=False,
            )
        return None
    return _put_config(state_dir, config, expected_generation, context)


def _require_credential_option(env_opt: object, id_opt: object, *, required: bool) -> None:
    given = [
        name
        for name, value in (("--credential-env", env_opt), ("--credential-id", id_opt))
        if value is not None
    ]
    if len(given) > 1:
        raise CliError(
            errors.INVALID_ARGUMENT,
            "only one of --credential-env or --credential-id may be given; they are mutually exclusive",
            errors=[
                {
                    "path": "--credential-env, --credential-id",
                    "message": "mutually exclusive options; give exactly one",
                }
            ],
        )
    if required and not given:
        raise CliError(
            errors.INVALID_ARGUMENT,
            "missing required option: exactly one of --credential-env or --credential-id is required",
            errors=[
                {
                    "path": "--credential-env, --credential-id",
                    "message": "exactly one credential source is required",
                }
            ],
        )


def _provider_entry(
    *, base_url: str, auth: str, credential_env: str | None = None, credential_id: str | None = None
) -> dict:
    entry: dict = {"base_url": base_url, "auth": auth}
    if credential_id is not None:
        entry["credential_id"] = credential_id
    elif credential_env is not None:
        entry["credential_env"] = credential_env
    return entry


def _credential_display(config: dict, entry: dict) -> str:
    from .catalog import credential_label

    if entry.get("credential_env") is not None:
        return f"env {entry['credential_env']}"
    cid = entry.get("credential_id")
    if cid is None:
        return "(no credential)"
    return credential_label(config, cid)


@provider_app.command("add")
def provider_add(
    pid: str = typer.Argument(..., help="provider id"),
    base_url: str = typer.Option(..., "--base-url", help="upstream base URL (http or https, no userinfo)"),
    auth: str = typer.Option(..., "--auth", help="bearer | x-api-key"),
    credential_env: Optional[str] = typer.Option(None, "--credential-env", help="environment variable holding the credential in the gateway process"),
    credential_id: Optional[str] = typer.Option(None, "--credential-id", help="credential catalog id (registered with 'qing credential add') or a private cred_<hex> id"),
    dry_run: bool = typer.Option(False, "--dry-run", help="preview the minimal change without writing"),
    state_dir: Optional[Path] = StateDirOpt,
    json_output: bool = JsonFlag,
) -> None:
    """Add one provider entry; a same-name entry fails with entry_exists.
    Changes are applied by the running gateway and never mean a connection
    was verified."""
    from . import catalog as cat

    with structured_catalog_errors(json_output):
        _require_credential_option(credential_env, credential_id, required=True)
        if auth not in cat.AUTH_MODES:
            raise CliError(
                errors.INVALID_ARGUMENT,
                f"--auth must be one of: {', '.join(cat.AUTH_MODES)}",
                errors=[
                    {
                        "path": "--auth",
                        "message": f"must be one of: {', '.join(cat.AUTH_MODES)}",
                    }
                ],
            )
        config, generation = _get_config(state_dir)
        if pid in config.get("providers", {}):
            raise CliError(
                errors.ENTRY_EXISTS,
                f"provider {pid!r} already exists; update it with 'qing provider set {pid}' instead",
            )
        if credential_id is not None and cat.resolve_credential_target(config, credential_id) is None:
            raise CliError(
                errors.ENTRY_NOT_FOUND,
                f"unknown credential {credential_id!r}; register it with "
                f"'qing credential add {credential_id} --env NAME' or reference an env variable "
                "with --credential-env",
            )
        entry = _provider_entry(
            base_url=base_url, auth=auth, credential_env=credential_env, credential_id=credential_id
        )
        config["providers"][pid] = entry
        _local_validate(config, "the provider edit")
        changes = {
            "action": "add provider",
            "id": pid,
            "add": {"id": pid, **entry},
            "lines": [
                f"provider {pid}:",
                "  action: add",
                f"  base url: {entry['base_url']}",
                f"  auth: {entry['auth']}",
                f"  credential: {_credential_display(config, entry)}",
            ],
        }
        payload = _catalog_commit(
            state_dir, config, generation, "provider add", dry_run, json_output, changes
        )
        if payload is None:
            return
        if json_output:
            _print_json({**_render_ok(payload), "provider": {"id": pid, **entry}})
            return
        stdout.print(
            f"provider {pid} added (applied, config revision {payload.get('revision')})", markup=False
        )
        stdout.print(f"  base url: {entry['base_url']}", markup=False)
        stdout.print(f"  auth: {entry['auth']}", markup=False)
        stdout.print(f"  credential: {_credential_display(config, entry)}", markup=False)
        stdout.print("saved or applied never means the connection was verified", markup=False)


@provider_app.command("list")
def provider_list(
    state_dir: Optional[Path] = StateDirOpt,
    json_output: bool = JsonFlag,
) -> None:
    """List provider entries (metadata only)."""
    from . import catalog as cat

    with structured_catalog_errors(json_output):
        config, _ = _get_config(state_dir)
        entries = cat.list_providers(config)
        if json_output:
            _print_json({"ok": True, "providers": entries})
            return
        if not entries:
            stdout.print("no providers configured", markup=False)
            return
        for entry in entries:
            stdout.print(f"provider {entry['id']}", markup=False)
            stdout.print(f"  base url: {entry.get('base_url')}", markup=False)
            stdout.print(f"  auth: {entry.get('auth')}", markup=False)
            stdout.print(f"  credential: {_credential_display(config, entry)}", markup=False)
        stdout.print(f"total: {len(entries)}", markup=False)


@provider_app.command("show")
def provider_show(
    pid: str = typer.Argument(..., help="provider id"),
    state_dir: Optional[Path] = StateDirOpt,
    json_output: bool = JsonFlag,
) -> None:
    """Show one provider entry (metadata only)."""
    from . import catalog as cat

    with structured_catalog_errors(json_output):
        config, _ = _get_config(state_dir)
        entry = cat.provider_view(config, pid)
        if entry is None:
            raise CliError(errors.ENTRY_NOT_FOUND, f"no provider {pid!r} in the catalog")
        if json_output:
            _print_json({"ok": True, "provider": entry})
            return
        stdout.print(f"provider {pid}", markup=False)
        stdout.print(f"  base url: {entry.get('base_url')}", markup=False)
        stdout.print(f"  auth: {entry.get('auth')}", markup=False)
        stdout.print(f"  credential: {_credential_display(config, entry)}", markup=False)


@provider_app.command("set")
def provider_set(
    pid: str = typer.Argument(..., help="provider id"),
    base_url: Optional[str] = typer.Option(None, "--base-url", help="upstream base URL"),
    auth: Optional[str] = typer.Option(None, "--auth", help="bearer | x-api-key"),
    credential_env: Optional[str] = typer.Option(None, "--credential-env", help="environment variable holding the credential"),
    credential_id: Optional[str] = typer.Option(None, "--credential-id", help="credential catalog id or private cred_<hex> id"),
    dry_run: bool = typer.Option(False, "--dry-run", help="preview the minimal change without writing"),
    state_dir: Optional[Path] = StateDirOpt,
    json_output: bool = JsonFlag,
) -> None:
    """Update one provider entry; a missing entry fails with
    entry_not_found and is never created implicitly."""
    from . import catalog as cat

    with structured_catalog_errors(json_output):
        if base_url is None and auth is None and credential_env is None and credential_id is None:
            raise CliError(
                errors.INVALID_ARGUMENT,
                "nothing to change; specify at least one of --base-url, --auth, --credential-env or --credential-id",
                errors=[
                    {
                        "path": "options",
                        "message": "at least one of --base-url, --auth, --credential-env or --credential-id is required",
                    }
                ],
            )
        _require_credential_option(credential_env, credential_id, required=False)
        if auth is not None and auth not in cat.AUTH_MODES:
            raise CliError(
                errors.INVALID_ARGUMENT,
                f"--auth must be one of: {', '.join(cat.AUTH_MODES)}",
                errors=[
                    {
                        "path": "--auth",
                        "message": f"must be one of: {', '.join(cat.AUTH_MODES)}",
                    }
                ],
            )
        config, generation = _get_config(state_dir)
        providers = config.get("providers", {})
        if pid not in providers:
            raise CliError(
                errors.ENTRY_NOT_FOUND,
                f"no provider {pid!r} in the catalog; add it with 'qing provider add {pid} ...'",
            )
        if credential_id is not None and cat.resolve_credential_target(config, credential_id) is None:
            raise CliError(
                errors.ENTRY_NOT_FOUND,
                f"unknown credential {credential_id!r}; register it with "
                f"'qing credential add {credential_id} --env NAME'",
            )
        current = dict(providers[pid])
        entry = dict(current)
        if base_url is not None:
            entry["base_url"] = base_url
        if auth is not None:
            entry["auth"] = auth
        if credential_env is not None:
            entry.pop("credential_env", None)
            entry.pop("credential_id", None)
            entry["credential_env"] = credential_env
        if credential_id is not None:
            entry.pop("credential_env", None)
            entry.pop("credential_id", None)
            entry["credential_id"] = credential_id
        config["providers"][pid] = entry
        _local_validate(config, "the provider edit")
        if entry == current:
            if json_output:
                _print_json({"ok": True, "applied": False, "changed": False, "provider": {"id": pid, **entry}})
            else:
                stdout.print(f"provider {pid}: no changes (already as requested)", markup=False)
            return
        changes = {
            "action": "set provider",
            "id": pid,
            "set": {"id": pid, **entry},
            "lines": [
                f"provider {pid}:",
                "  action: set",
                *[
                    f"  {key}: {entry[key]}"
                    for key in ("base_url", "auth", "credential_env", "credential_id")
                    if key in entry
                ],
            ],
        }
        payload = _catalog_commit(
            state_dir, config, generation, "provider update", dry_run, json_output, changes
        )
        if payload is None:
            return
        if json_output:
            _print_json({**_render_ok(payload), "provider": {"id": pid, **entry}})
            return
        stdout.print(
            f"provider {pid} updated (applied, config revision {payload.get('revision')})", markup=False
        )
        stdout.print("saved or applied never means the connection was verified", markup=False)


@provider_app.command("rm")
def provider_rm(
    pid: str = typer.Argument(..., help="provider id"),
    dry_run: bool = typer.Option(False, "--dry-run", help="preview the minimal change without writing"),
    state_dir: Optional[Path] = StateDirOpt,
    json_output: bool = JsonFlag,
) -> None:
    """Remove one provider entry; removal fails with entry_in_use when a
    model references it (no cascade, no silent rewrite)."""
    from . import catalog as cat

    with structured_catalog_errors(json_output):
        config, generation = _get_config(state_dir)
        if pid not in config.get("providers", {}):
            raise CliError(
                errors.ENTRY_NOT_FOUND,
                f"no provider {pid!r} in the catalog",
            )
        references = cat.provider_referenced_by_models(config, pid)
        if references:
            raise CliError(
                errors.ENTRY_IN_USE,
                f"provider {pid!r} is referenced by model(s): {', '.join(references)}",
                references=references,
            )
        del config["providers"][pid]
        _local_validate(config, "the provider edit")
        changes = {
            "action": "remove provider",
            "id": pid,
            "remove": pid,
            "lines": [f"provider {pid}:", "  action: remove"],
        }
        payload = _catalog_commit(
            state_dir, config, generation, "provider removal", dry_run, json_output, changes
        )
        if payload is None:
            return
        if json_output:
            _print_json({**_render_ok(payload), "removed": pid})
            return
        stdout.print(
            f"provider {pid} removed (applied, config revision {payload.get('revision')})", markup=False
        )


@credential_app.command("list")
def credential_list(
    state_dir: Optional[Path] = StateDirOpt,
    json_output: bool = JsonFlag,
) -> None:
    """List credential catalog entries (metadata only; never values)."""
    from . import catalog as cat

    with structured_catalog_errors(json_output):
        config, _ = _get_config(state_dir)
        entries = cat.list_credentials(config)
        if json_output:
            _print_json({"ok": True, "credentials": entries})
            return
        if not entries:
            stdout.print("no credentials in the catalog", markup=False)
            return
        for entry in entries:
            source = entry.get("source")
            if source == "env":
                source_text = f"env {entry.get('env')}"
            else:
                source_text = "private"
            stdout.print(f"credential {entry['id']}", markup=False)
            stdout.print(f"  source: {source_text}", markup=False)
            refs = entry.get("referenced_by") or []
            if refs:
                stdout.print(f"  referenced by: {', '.join(refs)}", markup=False)
        stdout.print(f"total: {len(entries)}", markup=False)


@credential_app.command("show")
def credential_show(
    cid: str = typer.Argument(..., help="credential id"),
    state_dir: Optional[Path] = StateDirOpt,
    json_output: bool = JsonFlag,
) -> None:
    """Show one credential catalog entry (metadata only; never the value)."""
    from . import catalog as cat

    with structured_catalog_errors(json_output):
        config, _ = _get_config(state_dir)
        entry = cat.credential_view(config, cid)
        if entry is None:
            raise CliError(errors.ENTRY_NOT_FOUND, f"no credential {cid!r} in the catalog")
        if json_output:
            _print_json({"ok": True, "credential": entry})
            return
        source = entry.get("source")
        source_text = f"env {entry.get('env')}" if source == "env" else "private"
        stdout.print(f"credential {cid}", markup=False)
        stdout.print(f"  source: {source_text}", markup=False)
        refs = entry.get("referenced_by") or []
        if refs:
            stdout.print(f"  referenced by: {', '.join(refs)}", markup=False)


@credential_app.command("add")
def credential_add(
    cid: Optional[str] = typer.Argument(
        None,
        help="credential id (required for --env; generated by the gateway for private credentials)",
    ),
    env_name: Optional[str] = typer.Option(None, "--env", help="environment variable holding the credential value"),
    from_stdin: bool = typer.Option(False, "--from-stdin", help="read the credential value from standard input"),
    from_file: Optional[Path] = typer.Option(None, "--from-file", help="read the credential value from a file"),
    dry_run: bool = typer.Option(False, "--dry-run", help="preview the minimal change without writing"),
    state_dir: Optional[Path] = StateDirOpt,
    json_output: bool = JsonFlag,
) -> None:
    """Register a credential source. Values only enter through --env, stdio
    or a file — never as command-line arguments — and never appear in
    output, errors or logs. A same-name entry fails with entry_exists."""
    import re as _re

    from . import catalog as cat

    with structured_catalog_errors(json_output):
        source_count = sum(
            (
                env_name is not None,
                from_stdin,
                from_file is not None,
            )
        )
        if source_count != 1:
            raise CliError(
                errors.INVALID_ARGUMENT,
                "exactly one credential source is required: --env NAME, --from-stdin or --from-file PATH",
                errors=[
                    {
                        "path": "credential source",
                        "message": "exactly one of --env, --from-stdin or --from-file is required",
                    }
                ],
            )
        if from_stdin or from_file is not None:
            if cid is not None:
                raise CliError(
                    errors.INVALID_ARGUMENT,
                    "private credential ids are generated by the gateway; do not pass an id with --from-stdin or --from-file",
                    errors=[
                        {
                            "path": "credential id",
                            "message": "ids are generated by the gateway for private credentials",
                        }
                    ],
                )
            if dry_run:
                config, generation = _get_config(state_dir)
                changes = {
                    "action": "add private credential",
                    "id": None,
                    "lines": [
                        "credential (private):",
                        "  action: add",
                        "  source: read from " + (str(from_file) if from_file is not None else "standard input"),
                        "  the gateway generates the immutable cred_<hex> id",
                        "  the value is committed only into the private store (0600), never into the configuration or any output",
                    ],
                }
                _catalog_commit(
                    state_dir, config, generation, "credential store", dry_run, json_output, changes
                )
                return
            try:
                if from_file is not None:
                    value = from_file.read_text(encoding="utf-8")
                else:
                    value = sys.stdin.read()
            except OSError as exc:
                raise CliError(
                    errors.INVALID_ARGUMENT,
                    f"cannot read the credential value: {exc}",
                    errors=[{"path": "--from-file", "message": f"cannot read: {exc}"}],
                ) from exc
            secret = value.rstrip("\r\n")
            if not secret:
                raise CliError(
                    errors.INVALID_ARGUMENT,
                    "the credential value is empty",
                    errors=[
                        {"path": "credential value", "message": "must be a non-empty value"}
                    ],
                )
            config, generation = _get_config(state_dir)
            result = _submit_private_credential(state_dir, config, generation, secret)
            status = result.get("status")
            if status == "unconfirmed":
                if json_output:
                    _print_json(
                        {
                            "ok": True,
                            "status": "unconfirmed",
                            "operation_id": result.get("operation_id"),
                            "message": result.get("message"),
                        }
                    )
                else:
                    stdout.print(result.get("message") or "effect unconfirmed", markup=False)
                return
            if status != "committed" or not result.get("applied"):
                raise CliError(
                    "unconfirmed",
                    f"the gateway did not confirm the private credential store ({status}); the effect is unconfirmed — query the operation before retrying",
                )
            cred_id = result.get("credential_id")
            if json_output:
                _print_json(
                    {
                        "ok": True,
                        "applied": True,
                        "generation": result.get("generation"),
                        "credential": {"id": cred_id, "source": "private", "referenced_by": []},
                    }
                )
                return
            stdout.print(f"private credential {cred_id} stored (applied by the running gateway)", markup=False)
            stdout.print("the value never appears in the configuration, output or logs", markup=False)
            stdout.print(
                f"reference it with: qing provider add <id> --base-url ... --credential-id {cred_id}",
                markup=False,
            )
            return

        # --env: a named environment-variable credential source.
        assert env_name is not None
        if cid is None:
            raise CliError(
                errors.INVALID_ARGUMENT,
                "a credential id is required with --env NAME",
                errors=[
                    {
                        "path": "credential id",
                        "message": "required when the credential source is --env",
                    }
                ],
            )
        if _re.match(r"\Acred_[0-9a-f]{32}\Z", cid):
            raise CliError(
                errors.INVALID_ARGUMENT,
                "cred_<hex> ids are reserved for private credentials; choose a plain id for an env-sourced credential",
                errors=[
                    {
                        "path": "credential id",
                        "message": "cred_<hex> ids are reserved for private credentials",
                    }
                ],
            )
        config, generation = _get_config(state_dir)
        if cid in config.get("credentials", {}):
            raise CliError(
                errors.ENTRY_EXISTS,
                f"credential {cid!r} already exists; update the provider reference or remove it with 'qing credential rm {cid}'",
            )
        if cat.credential_referenced_by(config, cid):
            raise CliError(
                errors.ENTRY_EXISTS,
                f"credential {cid!r} is already referenced by a provider; remove that reference first or use 'qing credential show {cid}'",
            )
        config.setdefault("credentials", {})[cid] = {"source": "env", "env": env_name}
        _local_validate(config, "the credential catalog edit")
        changes = {
            "action": "add env credential",
            "id": cid,
            "add": {"source": "env", "env": env_name},
            "lines": [
                f"credential {cid}:",
                "  action: add",
                f"  source: env {env_name}",
            ],
        }
        payload = _catalog_commit(
            state_dir, config, generation, "credential catalog add", dry_run, json_output, changes
        )
        if payload is None:
            return
        if json_output:
            _print_json(
                {
                    **_render_ok(payload),
                    "credential": {"id": cid, "source": "env", "env": env_name, "referenced_by": []},
                }
            )
            return
        stdout.print(
            f"credential {cid} added (env {env_name}, applied, config revision {payload.get('revision')})",
            markup=False,
        )
        stdout.print(
            f"reference it with: qing provider add <id> --base-url ... --credential-id {cid}",
            markup=False,
        )


@credential_app.command("rm")
def credential_rm(
    cid: str = typer.Argument(..., help="credential id"),
    dry_run: bool = typer.Option(False, "--dry-run", help="preview the minimal change without writing"),
    state_dir: Optional[Path] = StateDirOpt,
    json_output: bool = JsonFlag,
) -> None:
    """Remove a credential catalog entry; removal fails with entry_in_use
    when a provider references it (no cascade, no silent rewrite). Private
    store versions are immutable and are never deleted by this command."""
    from . import catalog as cat

    with structured_catalog_errors(json_output):
        config, generation = _get_config(state_dir)
        entry = cat.credential_view(config, cid)
        if entry is None:
            raise CliError(errors.ENTRY_NOT_FOUND, f"no credential {cid!r} in the catalog")
        references = entry.get("referenced_by") or []
        if references:
            raise CliError(
                errors.ENTRY_IN_USE,
                f"credential {cid!r} is referenced by provider(s): {', '.join(references)}",
                references=references,
            )
        if entry.get("derived"):
            raise CliError(
                errors.ENTRY_IN_USE,
                f"credential {cid!r} is a private store version owned by an import; it cannot be removed with this command",
                references=references,
            )
        config.get("credentials", {}).pop(cid, None)
        _local_validate(config, "the credential catalog edit")
        changes = {
            "action": "remove credential",
            "id": cid,
            "remove": cid,
            "lines": [f"credential {cid}:", "  action: remove"],
        }
        payload = _catalog_commit(
            state_dir, config, generation, "credential catalog removal", dry_run, json_output, changes
        )
        if payload is None:
            return
        if json_output:
            _print_json({**_render_ok(payload), "removed": cid})
            return
        stdout.print(
            f"credential {cid} removed (applied, config revision {payload.get('revision')})", markup=False
        )
        if entry.get("source") == "private":
            stdout.print(
                "private store versions are immutable; the stored value is kept per the existing version rules",
                markup=False,
            )


@model_app.command("add")
def model_add(
    mid: str = typer.Argument(..., help="model id"),
    provider: str = typer.Option(..., "--provider", help="provider id that must already exist"),
    upstream_model: str = typer.Option(..., "--upstream-model", help="exact upstream model string sent to the provider"),
    dry_run: bool = typer.Option(False, "--dry-run", help="preview the minimal change without writing"),
    state_dir: Optional[Path] = StateDirOpt,
    json_output: bool = JsonFlag,
) -> None:
    """Add one model entry; a same-name entry fails with entry_exists and
    an unknown provider fails with entry_not_found."""
    from . import catalog as cat

    with structured_catalog_errors(json_output):
        config, generation = _get_config(state_dir)
        if mid in config.get("models", {}):
            raise CliError(
                errors.ENTRY_EXISTS,
                f"model {mid!r} already exists; update it with 'qing model set {mid}' instead",
            )
        if provider not in config.get("providers", {}):
            raise CliError(
                errors.ENTRY_NOT_FOUND,
                f"provider {provider!r} does not exist; add it with 'qing provider add {provider} ...' first",
            )
        entry = {"provider": provider, "upstream_model": upstream_model}
        config["models"][mid] = entry
        _local_validate(config, "the model edit")
        changes = {
            "action": "add model",
            "id": mid,
            "add": {"id": mid, **entry},
            "lines": [
                f"model {mid}:",
                "  action: add",
                f"  provider: {provider}",
                f"  upstream model: {upstream_model}",
            ],
        }
        payload = _catalog_commit(state_dir, config, generation, "model add", dry_run, json_output, changes)
        if payload is None:
            return
        if json_output:
            _print_json({**_render_ok(payload), "model": {"id": mid, **entry}})
            return
        stdout.print(
            f"model {mid} added (applied, config revision {payload.get('revision')})", markup=False
        )


@model_app.command("list")
def model_list(
    state_dir: Optional[Path] = StateDirOpt,
    json_output: bool = JsonFlag,
) -> None:
    """List model entries."""
    from . import catalog as cat

    with structured_catalog_errors(json_output):
        config, _ = _get_config(state_dir)
        entries = cat.list_models(config)
        if json_output:
            _print_json({"ok": True, "models": entries})
            return
        if not entries:
            stdout.print("no models configured", markup=False)
            return
        for entry in entries:
            stdout.print(f"model {entry['id']}", markup=False)
            stdout.print(f"  provider: {entry.get('provider')}", markup=False)
            stdout.print(f"  upstream model: {entry.get('upstream_model')}", markup=False)
        stdout.print(f"total: {len(entries)}", markup=False)


@model_app.command("show")
def model_show(
    mid: str = typer.Argument(..., help="model id"),
    state_dir: Optional[Path] = StateDirOpt,
    json_output: bool = JsonFlag,
) -> None:
    """Show one model entry."""
    from . import catalog as cat

    with structured_catalog_errors(json_output):
        config, _ = _get_config(state_dir)
        entry = cat.model_view(config, mid)
        if entry is None:
            raise CliError(errors.ENTRY_NOT_FOUND, f"no model {mid!r} in the catalog")
        if json_output:
            _print_json({"ok": True, "model": entry})
            return
        stdout.print(f"model {mid}", markup=False)
        stdout.print(f"  provider: {entry.get('provider')}", markup=False)
        stdout.print(f"  upstream model: {entry.get('upstream_model')}", markup=False)


@model_app.command("set")
def model_set(
    mid: str = typer.Argument(..., help="model id"),
    provider: Optional[str] = typer.Option(None, "--provider", help="provider id that must already exist"),
    upstream_model: Optional[str] = typer.Option(None, "--upstream-model", help="exact upstream model string"),
    dry_run: bool = typer.Option(False, "--dry-run", help="preview the minimal change without writing"),
    state_dir: Optional[Path] = StateDirOpt,
    json_output: bool = JsonFlag,
) -> None:
    """Update one model entry; a missing entry fails with entry_not_found
    and is never created implicitly."""
    from . import catalog as cat

    with structured_catalog_errors(json_output):
        if provider is None and upstream_model is None:
            raise CliError(
                errors.INVALID_ARGUMENT,
                "nothing to change; specify at least one of --provider or --upstream-model",
                errors=[
                    {
                        "path": "options",
                        "message": "at least one of --provider or --upstream-model is required",
                    }
                ],
            )
        config, generation = _get_config(state_dir)
        models = config.get("models", {})
        if mid not in models:
            raise CliError(
                errors.ENTRY_NOT_FOUND,
                f"no model {mid!r} in the catalog; add it with 'qing model add {mid} ...'",
            )
        if provider is not None and provider not in config.get("providers", {}):
            raise CliError(
                errors.ENTRY_NOT_FOUND,
                f"provider {provider!r} does not exist; add it with 'qing provider add {provider} ...' first",
            )
        current = dict(models[mid])
        entry = dict(current)
        if provider is not None:
            entry["provider"] = provider
        if upstream_model is not None:
            entry["upstream_model"] = upstream_model
        config["models"][mid] = entry
        _local_validate(config, "the model edit")
        if entry == current:
            if json_output:
                _print_json({"ok": True, "applied": False, "changed": False, "model": {"id": mid, **entry}})
            else:
                stdout.print(f"model {mid}: no changes (already as requested)", markup=False)
            return
        changes = {
            "action": "set model",
            "id": mid,
            "set": {"id": mid, **entry},
            "lines": [
                f"model {mid}:",
                "  action: set",
                f"  provider: {entry['provider']}",
                f"  upstream model: {entry['upstream_model']}",
            ],
        }
        payload = _catalog_commit(state_dir, config, generation, "model update", dry_run, json_output, changes)
        if payload is None:
            return
        if json_output:
            _print_json({**_render_ok(payload), "model": {"id": mid, **entry}})
            return
        stdout.print(
            f"model {mid} updated (applied, config revision {payload.get('revision')})", markup=False
        )


@model_app.command("rm")
def model_rm(
    mid: str = typer.Argument(..., help="model id"),
    dry_run: bool = typer.Option(False, "--dry-run", help="preview the minimal change without writing"),
    state_dir: Optional[Path] = StateDirOpt,
    json_output: bool = JsonFlag,
) -> None:
    """Remove one model entry; removal fails with entry_in_use when the
    defaults or a route reference it (no cascade, no silent rewrite)."""
    from . import catalog as cat

    with structured_catalog_errors(json_output):
        config, generation = _get_config(state_dir)
        if mid not in config.get("models", {}):
            raise CliError(
                errors.ENTRY_NOT_FOUND,
                f"no model {mid!r} in the catalog",
            )
        references = cat.model_referenced_by(config, mid)
        if references:
            raise CliError(
                errors.ENTRY_IN_USE,
                f"model {mid!r} is referenced by: {', '.join(references)}",
                references=references,
            )
        del config["models"][mid]
        _local_validate(config, "the model edit")
        changes = {
            "action": "remove model",
            "id": mid,
            "remove": mid,
            "lines": [f"model {mid}:", "  action: remove"],
        }
        payload = _catalog_commit(state_dir, config, generation, "model removal", dry_run, json_output, changes)
        if payload is None:
            return
        if json_output:
            _print_json({**_render_ok(payload), "removed": mid})
            return
        stdout.print(
            f"model {mid} removed (applied, config revision {payload.get('revision')})", markup=False
        )


def _submit_private_credential(
    state_dir: Optional[Path], config: dict, generation: int, secret: str
) -> dict:
    """Store a private credential through the authenticated gateway.

    Reuses the existing single-transaction import channel (the only path
    that may carry a secret over the authenticated control channel); the
    plan is credential-only and commits the private store version plus its
    catalog entry atomically. No offline write, no new endpoint.
    """
    from .importing import ImportPlan, plan_to_wire
    from .tokens import new_operation_id

    plan = ImportPlan(
        operation_id=new_operation_id(),
        source=Path("<private-credential>"),
        source_digest="0" * 64,
        expected_generation=generation,
        status="apply",
        secret=secret,
    )
    try:
        response = _call(
            state_dir,
            "POST",
            "/control/v1/imports",
            json={"operation_id": plan.operation_id, "plan": plan_to_wire(plan)},
            timeout=30.0,
            unconfirmed_action="the private credential store",
        )
    except CliError as exc:
        if exc.code != "unconfirmed":
            raise
        try:
            query = _check_cr(
                _call(state_dir, "GET", f"/control/v1/operations/{plan.operation_id}")
            )
        except (CliError, typer.BadParameter):
            query = {}
        status = query.get("status")
        if status in ("committed", "aborted"):
            return {
                "status": status,
                "operation_id": plan.operation_id,
                "generation": query.get("generation"),
                "applied": status == "committed",
                "recovered": True,
                "credential_id": query.get("credential_id"),
            }
        return {
            "status": "unconfirmed",
            "operation_id": plan.operation_id,
            "message": "the gateway did not confirm the private credential store in time; "
            "check again with 'qing config operation " + plan.operation_id + "' before retrying",
        }
    return _check_cr(response)


@app.command()
def serve(
    state_dir: Optional[Path] = StateDirOpt,
    port: int = typer.Option(0, "--port", help="loopback port (0 picks a free port)"),
) -> None:
    """Run the gateway in the foreground (loopback only)."""
    raise typer.Exit(run_serve(_state_dir(state_dir), port))


@config_app.command("show")
def config_show(
    state_dir: Optional[Path] = StateDirOpt,
    json_output: bool = JsonFlag,
) -> None:
    """Show the applied shared configuration."""
    with structured_cli_errors(json_output):
        payload = _check(_call(state_dir, "GET", "/control/v1/config"))
        if json_output:
            _print_json(payload)
            return
        config = payload.get("config", {})
        stdout.print(f"config revision: {payload.get('revision')}")
        if isinstance(payload.get("generation"), int):
            stdout.print(f"generation: {payload.get('generation')}")
        stdout.print("providers:")
        from . import catalog as _cat

        for pid, p in config.get("providers", {}).items():
            if p.get("credential_env") is not None:
                credential = f"credential env {p.get('credential_env')}"
            else:
                cid = p.get("credential_id")
                credential = f"credential {_cat.credential_label(config, cid) if cid else '(none)'}"
            stdout.print(f"  {pid}: {p.get('base_url')} ({p.get('auth')}, {credential})", markup=False)
        credentials = config.get("credentials", {})
        if credentials:
            stdout.print("credentials:")
            for cid in sorted(credentials):
                entry = credentials[cid]
                source = f"env {entry['env']}" if entry.get("source") == "env" else "private"
                stdout.print(f"  {cid}: {source}", markup=False)
        stdout.print("models:")
        for mid, m in config.get("models", {}).items():
            stdout.print(f"  {mid}: provider {m.get('provider')}, upstream model {m.get('upstream_model')}")
        defaults = config.get("defaults", {})
        stdout.print("defaults:")
        stdout.print(f"  model: {defaults.get('model')}")
        stdout.print(f"  aux_model: {defaults.get('aux_model')}")
        stdout.print("  routes:")
        for request_model, dest in defaults.get("routes", {}).items():
            stdout.print(f"    {request_model} -> {dest}")


@config_app.command("apply")
def config_apply(
    file: Path = typer.Argument(..., help="JSON file with the complete shared configuration"),
    state_dir: Optional[Path] = StateDirOpt,
    json_output: bool = JsonFlag,
) -> None:
    """Submit a complete shared configuration (validated and applied atomically)."""
    with structured_cli_errors(json_output):
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise typer.BadParameter(f"cannot read configuration file {file}: {exc}") from exc
        payload = _require_applied(
            _check(_call(state_dir, "PUT", "/control/v1/config", json=data, unconfirmed_action="the configuration apply")),
            "config apply",
        )
        if json_output:
            _print_json(payload)
            return
        stdout.print(f"config applied (revision {payload.get('revision')})")
        stdout.print("catalog and defaults replaced; existing instances keep their snapshots")


_AUTH_CHOICES = ("bearer", "x-api-key")
_PRIMARY_CHOICES = {
    "settings": "settings.model",
    "env": "env.ANTHROPIC_MODEL",
    "settings.model": "settings.model",
    "env.ANTHROPIC_MODEL": "env.ANTHROPIC_MODEL",
}
_CONFLICT_CHOICES = ("fail", "skip", "update", "new")


def _print_import_preview(preview: dict) -> None:
    stdout.print(f"source: {preview['source']}", markup=False)
    stdout.print(f"status: {preview['status']}", markup=False)
    provider = preview.get("provider")
    if provider is not None:
        stdout.print(f"provider {provider['id']}:", markup=False)
        stdout.print(f"  base url: {provider['base_url']}", markup=False)
        stdout.print(f"  auth: {provider['auth']}", markup=False)
        stdout.print(f"  credential: {provider['credential']}", markup=False)
    if preview["models"]:
        stdout.print("models:")
        for model in preview["models"]:
            keys = ", ".join(model["request_keys"]) or "(no request keys)"
            stdout.print(
                f"  {model['id']} -> upstream {model['upstream_model']} (requests: {keys})",
                markup=False,
            )
    defaults = preview.get("defaults")
    if defaults is not None:
        stdout.print("defaults:")
        stdout.print(f"  model: {defaults['model']}", markup=False)
        stdout.print(f"  aux model: {defaults['aux_model']}", markup=False)
        for key, dest in defaults["routes"].items():
            stdout.print(f"  route {key} -> {dest}", markup=False)
    for note in preview["incomplete"]:
        stdout.print(f"incomplete: {note}", markup=False)
    for note in preview["unsupported_auth"]:
        stdout.print(f"unsupported auth: {note}", markup=False)
    for name in preview["ignored"]:
        stdout.print(f"ignored: {name} (not migrated)", markup=False)
    for note in preview["notes"]:
        stdout.print(f"note: {note}", markup=False)
    stdout.print(
        "existing instances keep their snapshots; only new instances use new defaults",
        markup=False,
    )


@config_app.command("import-claude")
def config_import_claude(
    source: Optional[Path] = typer.Option(
        None, "--source", help="Claude settings file (default: CLAUDE_CONFIG_DIR or HOME settings)"
    ),
    apply: bool = typer.Option(
        False, "--apply", help="apply the plan; without it the command only previews and writes nothing"
    ),
    auth: Optional[str] = typer.Option(
        None, "--auth", help="credential to import when the source has both: bearer | x-api-key"
    ),
    primary_model: Optional[str] = typer.Option(
        None, "--primary-model", help="primary model source when both differ: settings | env"
    ),
    aux_same_as_primary: bool = typer.Option(
        False, "--aux-same-as-primary", help="explicitly use the primary model as the aux model"
    ),
    conflict: str = typer.Option(
        "fail", "--conflict", help="same-name conflict handling: fail | skip | update | new"
    ),
    new_provider_id: Optional[str] = typer.Option(
        None, "--new-provider-id", help="provider id for --conflict new"
    ),
    state_dir: Optional[Path] = StateDirOpt,
    json_output: bool = JsonFlag,
) -> None:
    """Import a Claude settings file into the shared configuration.

    Preview by default: nothing is written and no provider is contacted.
    Applying stores the credential in the private store and commits one
    configuration transaction; saved or applied never means verified.
    """
    from . import claude_source, transactions
    from .config import ConfigStore
    from .importing import (
        ImportDecisions,
        PlanError,
        build_plan,
        plan_preview,
        source_digest,
    )
    from .tokens import new_operation_id

    with structured_cli_errors(json_output):
        if auth is not None and auth not in _AUTH_CHOICES:
            raise typer.BadParameter(f"--auth must be one of: {', '.join(_AUTH_CHOICES)}")
        if primary_model is not None and primary_model not in _PRIMARY_CHOICES:
            raise typer.BadParameter("--primary-model must be one of: settings, env")
        if conflict not in _CONFLICT_CHOICES:
            raise typer.BadParameter(f"--conflict must be one of: {', '.join(_CONFLICT_CHOICES)}")

        directory = _state_dir(state_dir)
        path = claude_source.resolve_source(source)
        try:
            raw = claude_source.read_source(path)
            parsed = claude_source.parse_claude_settings(raw, source=path)
        except claude_source.SourceError as exc:
            raise CliError(exc.code, exc.message) from exc
        discovery = state_mod.read_discovery(directory)
        gateway_endpoint = f"http://127.0.0.1:{discovery['port']}" if discovery else None
        store = ConfigStore(directory / state_mod.CONFIG_NAME)
        try:
            current = store.load()
        except errors.ApiError as exc:
            raise CliError(exc.code, exc.message) from exc
        decisions = ImportDecisions(
            auth_kind=auth,
            primary_source=_PRIMARY_CHOICES.get(primary_model) if primary_model else None,
            aux_same_as_primary=aux_same_as_primary,
            conflict=conflict,
            new_provider_id=new_provider_id,
        )
        try:
            plan = build_plan(
                parsed,
                source_digest=source_digest(raw),
                current=current,
                credential_store=transactions.CredentialStore(directory),
                decisions=decisions,
                operation_id=new_operation_id(),
                gateway_endpoint=gateway_endpoint,
            )
        except PlanError as exc:
            raise CliError(exc.code, exc.message) from exc
        preview = plan_preview(plan)
        if not apply:
            preview["written"] = False
            if json_output:
                _print_json(preview)
                return
            _print_import_preview(preview)
            stdout.print("preview only: nothing was written, no provider was contacted", markup=False)
            return

        if sys.stdin.isatty():
            confirmed = typer.confirm("Apply this import plan?")
            if not confirmed:
                if json_output:
                    _print_json({"status": "cancelled", "written": False})
                else:
                    stdout.print("cancelled: nothing was written", markup=False)
                return

        online = False
        try:
            result = transactions.commit_import_offline(
                state_dir=directory, plan=plan, raw_source=raw
            )
        except errors.ApiError as exc:
            if exc.code != errors.GATEWAY_LOCKED:
                raise CliError(exc.code, exc.message) from exc
            if discovery is None:
                raise CliError(
                    "gateway_unreachable",
                    "the state directory is locked but no gateway discovery is available; "
                    "nothing was written",
                ) from exc
            # A gateway owns the directory: submit the plan to it instead of
            # ever writing under a running gateway.
            from .importing import verify_source_unchanged

            try:
                verify_source_unchanged(plan.source_digest, claude_source.read_source(path))
            except Exception as exc2:
                raise CliError("source_changed", str(exc2)) from exc2
            result = _submit_import_online(directory, plan)
            online = True
        if json_output:
            _print_json({"result": result, "preview": preview})
            return
        _print_import_result(result, online=online)


def _parse_route_pair(pair: str) -> tuple[str, str]:
    if "=" not in pair:
        raise typer.BadParameter(f"route must be REQUEST=DEST, got {pair!r}")
    left, right = pair.split("=", 1)
    left, right = left.strip(), right.strip()
    if not left or not right:
        raise typer.BadParameter(f"route must be REQUEST=DEST, got {pair!r}")
    return left, right


def _submit_import_online(state_dir: Path, plan) -> dict:
    """Submit a plan to the running gateway; never fall back to offline writes.

    A timeout leaves the effect unconfirmed: the operation is queried once
    by id, and an unresolved outcome is reported as unconfirmed instead of
    being retried blindly or claimed as failed.
    """
    from .importing import plan_to_wire

    try:
        response = _call(
            state_dir,
            "POST",
            "/control/v1/imports",
            json={"operation_id": plan.operation_id, "plan": plan_to_wire(plan)},
            timeout=30.0,
            unconfirmed_action="the import submit",
        )
    except CliError as exc:
        if exc.code == "unconfirmed":
            try:
                query = _check(
                    _call(state_dir, "GET", f"/control/v1/operations/{plan.operation_id}")
                )
            except (CliError, typer.BadParameter):
                query = {}
            status = query.get("status")
            if status in ("committed", "aborted"):
                return {
                    "status": status,
                    "operation_id": plan.operation_id,
                    "generation": query.get("generation"),
                    "applied": status == "committed",
                    "recovered": True,
                }
            return {
                "status": "unconfirmed",
                "operation_id": plan.operation_id,
                "message": "the gateway did not confirm the import in time; check again "
                f"with 'qing config operation {plan.operation_id}' before retrying",
            }
        raise
    return _check(response)


def _print_import_result(result: dict, *, online: bool) -> None:
    status = result.get("status")
    if status == "unchanged":
        stdout.print("no changes: the same connection is already present", markup=False)
    elif status == "skipped":
        stdout.print("skipped: no changes were made", markup=False)
    elif status == "aborted":
        stdout.print(
            f"import aborted by recovery (operation {result.get('operation_id')}); "
            "nothing was left behind",
            markup=False,
        )
    elif status == "unconfirmed":
        stdout.print(
            f"import unconfirmed (operation {result.get('operation_id')}); "
            "do not assume it failed — query it again before retrying",
            markup=False,
        )
    else:
        stdout.print(
            f"import committed (generation {result.get('generation')})",
            markup=False,
        )
        if result.get("duplicate"):
            stdout.print("this repeats an operation that already completed", markup=False)
        stdout.print(
            f"provider {result.get('provider_id')} saved with a private credential",
            markup=False,
        )
        if online:
            stdout.print("applied by the running gateway", markup=False)
        else:
            stdout.print("configuration saved; start or restart the gateway to use it", markup=False)
    stdout.print(
        "connection not verified: importing never contacts the provider",
        markup=False,
    )


@config_app.command("operation")
def config_operation(
    operation_id: str = typer.Argument(..., help="operation id from an import"),
    state_dir: Optional[Path] = StateDirOpt,
    json_output: bool = JsonFlag,
) -> None:
    """Query an import operation: id, commit status and generation only."""
    with structured_cli_errors(json_output):
        payload = _check(_call(state_dir, "GET", f"/control/v1/operations/{operation_id}"))
        if json_output:
            _print_json(payload)
            return
        stdout.print(f"operation: {payload.get('operation_id', operation_id)}", markup=False)
        stdout.print(f"status: {payload.get('status')}", markup=False)
        stdout.print(f"generation: {payload.get('generation')}", markup=False)


@defaults_app.command("set")
def defaults_set(
    model: str = typer.Option(..., "--model", help="default main request model"),
    aux_model: str = typer.Option(..., "--aux-model", help="default auxiliary request model"),
    route: list[str] = typer.Option(..., "--route", help="REQUEST=DEST route entry (repeatable)"),
    state_dir: Optional[Path] = StateDirOpt,
    json_output: bool = JsonFlag,
) -> None:
    """Replace the complete defaults; affects new instances only."""
    with structured_cli_errors(json_output):
        routes = dict(_parse_route_pair(pair) for pair in route)
        current = _check(_call(state_dir, "GET", "/control/v1/config"))
        config = dict(current.get("config", {}))
        expected_generation = current.get("generation")
        config["defaults"] = {"model": model, "aux_model": aux_model, "routes": routes}
        params = {"expected_generation": expected_generation} if isinstance(expected_generation, int) else {}
        payload = _require_applied(
            _check(
                _call(
                    state_dir,
                    "PUT",
                    "/control/v1/config",
                    params=params,
                    json=config,
                    unconfirmed_action="the defaults update",
                )
            ),
            "defaults update",
        )
        if json_output:
            _print_json(payload)
            return
        stdout.print(f"defaults applied (config revision {payload.get('revision')})")
        stdout.print("changes affect new instances only; existing instances keep their snapshots")


def _print_run_preview(*, state_dir, model, aux_model, route_overrides) -> None:
    """Onboarding preview: no registration, no files, no client, no tokens."""
    payload = _check(_call(state_dir, "GET", "/control/v1/config"))
    endpoint, _ = _endpoint(state_dir)
    defaults = payload.get("config", {}).get("defaults", {})
    chosen_model = model if model is not None else defaults.get("model")
    chosen_aux = aux_model if aux_model is not None else defaults.get("aux_model")
    stdout.print("qing run onboarding preview", markup=False)
    stdout.print(f"  gateway address: {endpoint}", markup=False)
    stdout.print(f"  main model: {chosen_model if chosen_model is not None else '(none configured)'}", markup=False)
    stdout.print(f"  aux model: {chosen_aux if chosen_aux is not None else '(none configured)'}", markup=False)
    for request_key, destination in route_overrides.items():
        stdout.print(f"  route override: {request_key} -> {destination}", markup=False)
    stdout.print("  original Claude settings: untouched; hooks and permissions keep working", markup=False)
    stdout.print(
        "  during the run: a temporary 0600 settings file carries the gateway transport "
        "and is deleted on every exit path",
        markup=False,
    )
    stdout.print(
        "  after exit: plain 'claude' connects directly again with your original configuration",
        markup=False,
    )
    stdout.print(
        "preview only: no instance is registered, no files are written, no client starts",
        markup=False,
    )


@app.command("run", context_settings={"ignore_unknown_options": True, "allow_extra_args": True})
def run(
    ctx: typer.Context,
    label: Optional[str] = typer.Option(None, "--label", help="display label for targeting"),
    model: Optional[str] = typer.Option(None, "--model", help="main request model (route key)"),
    aux_model: Optional[str] = typer.Option(None, "--aux-model", help="auxiliary request model (route key)"),
    route: list[str] = typer.Option([], "--route", help="REQUEST=DEST route override (repeatable)"),
    preview: bool = typer.Option(
        False,
        "--preview",
        help="show the onboarding impact without registering, writing files or starting a client",
    ),
    state_dir: Optional[Path] = StateDirOpt,
    json_output: bool = typer.Option(
        False,
        "--json",
        help="emit launcher diagnostics as NDJSON events on stderr; Claude's stdout and native --output-format are never modified",
    ),
) -> None:
    """Register an instance and run Claude attached to the gateway.

    Native Claude arguments go after '--'; --resume and other
    non-conflicting arguments are passed through. Native --settings,
    --model and --bare are rejected because the launcher owns the
    transport. Claude's stdout is passed through untouched; launcher
    messages go to stderr.
    """
    from . import launcher

    try:
        route_overrides = {}
        for pair in route:
            request_model, destination = _parse_route_pair(pair)
            route_overrides[request_model] = destination
        if preview:
            _print_run_preview(
                state_dir=state_dir,
                model=model,
                aux_model=aux_model,
                route_overrides=route_overrides,
            )
            return
        code = launcher.run_launch(
            state_dir=_state_dir(state_dir),
            label=label,
            model=model,
            aux_model=aux_model,
            route_overrides=route_overrides,
            native_args=list(ctx.args),
            machine_events=json_output,
        )
    except launcher.LaunchError as exc:
        if json_output:
            # wrapper-originated errors honour machine mode: NDJSON on
            # stderr; the child was never started, so its stdout is absent.
            print(
                json.dumps({"event": "error", "code": exc.code, "message": exc.message}, sort_keys=True),
                file=sys.stderr,
                flush=True,
            )
        else:
            stderr.print(f"error: {exc.message}")
        raise typer.Exit(code=exc.exit_code) from exc
    except typer.BadParameter as exc:
        if json_output:
            print(
                json.dumps({"event": "error", "code": "cli_error", "message": str(exc)}, sort_keys=True),
                file=sys.stderr,
                flush=True,
            )
            raise typer.Exit(code=1) from exc
        raise
    raise typer.Exit(code=code)


@app.command("instances")
def instances(
    state_dir: Optional[Path] = StateDirOpt,
    offset: int = typer.Option(0, "--offset"),
    limit: int = typer.Option(50, "--limit"),
    json_output: bool = JsonFlag,
) -> None:
    """List gateway instances."""
    with structured_cli_errors(json_output):
        payload = _check(_call(state_dir, "GET", "/control/v1/instances", params={"offset": offset, "limit": limit}))
        if json_output:
            _print_json(payload)
            return
        # Vertical blocks: every value stays complete at any width and is
        # printed literally (no markup interpretation, no column folding).
        stale = False
        for inst in payload.get("instances", []):
            route = (inst.get("routes") or {}).get(inst.get("model") or "")
            stdout.print(f"instance {inst.get('id', '')}", markup=False)
            stdout.print(f"  label: {inst.get('label') or ''}", markup=False)
            stdout.print(f"  state: {inst.get('state', '')}", markup=False)
            stdout.print(f"  model: {inst.get('model', '')}", markup=False)
            stdout.print(f"  aux: {inst.get('aux_model', '')}", markup=False)
            stdout.print(f"  revision: {inst.get('revision', '')}", markup=False)
            if route:
                stdout.print(
                    f"  route: {route.get('provider')}/{route.get('upstream_model')}", markup=False
                )
                if route.get("catalog_present") is False:
                    stale = True
                    stdout.print("  route catalog: stale (*)", markup=False)
        if stale:
            stdout.print("* catalog entry changed or was removed after this instance resolved it")
        total = payload.get("total")
        stdout.print(f"total: {total}, offset: {payload.get('offset')}, limit: {payload.get('limit')}")


@instance_app.command("end")
def instance_end(
    target: str = typer.Argument(..., help="full instance ID"),
    state_dir: Optional[Path] = StateDirOpt,
    json_output: bool = JsonFlag,
) -> None:
    """End an instance; new requests are rejected, in-flight requests complete."""
    with structured_cli_errors(json_output):
        payload = _check(_call(state_dir, "DELETE", f"/control/v1/instances/{target}"))
        if json_output:
            _print_json(payload)
            return
        inst = payload.get("instance", {})
        stdout.print(f"instance {inst.get('id')} ended", markup=False)
        stdout.print("in-flight requests keep their original snapshot until completion")


def _resolve_instance(base: tuple[str, str], target: str) -> dict:
    """Resolve a target (full ID or unique label) to one instance status."""
    url, token = base
    matches: list[dict] = []
    offset = 0
    try:
        # trust_env=False: never send the control token through an ambient proxy.
        with httpx.Client(
            base_url=url,
            headers={"authorization": f"Bearer {token}"},
            timeout=15.0,
            trust_env=False,
        ) as client:
            while True:
                response = client.get("/control/v1/instances", params={"offset": offset, "limit": 100})
                if response.status_code >= 400:
                    raise typer.BadParameter(f"cannot list instances: {response.text[:200]}")
                payload = response.json()
                matches.extend(
                    i for i in payload.get("instances", []) if i.get("id") == target or i.get("label") == target
                )
                next_offset = payload.get("next_offset")
                if next_offset is None:
                    break
                offset = next_offset
    except httpx.HTTPError as exc:
        raise _connection_failure("instance listing", exc) from exc
    exact = [i for i in matches if i.get("id") == target]
    if exact:
        return exact[0]
    if len(matches) > 1:
        ids = ", ".join(sorted(i.get("id", "?") for i in matches))
        raise typer.BadParameter(f"label {target!r} matches multiple instances ({ids}); use the full instance ID")
    if not matches:
        raise typer.BadParameter(f"no instance or label matches {target!r}")
    return matches[0]


@route_app.command("set")
def route_set(
    request_model: str = typer.Argument(..., help="exact request model string"),
    destination: str = typer.Argument(..., help="catalog model ID"),
    instance: str = typer.Option(..., "--instance", help="full instance ID or unique label"),
    state_dir: Optional[Path] = StateDirOpt,
    json_output: bool = JsonFlag,
) -> None:
    """Switch one request model for one instance (compare-and-swap on revision)."""
    with structured_cli_errors(json_output):
        base = _endpoint(state_dir)
        target = _resolve_instance(base, instance)
        expected_revision = target.get("revision")
        before = target.get("routes", {}).get(request_model)
        payload = _require_applied(
            _check(
                _call(
                    state_dir,
                    "PUT",
                    f"/control/v1/instances/{target['id']}/routes",
                    json={
                        "request_model": request_model,
                        "model": destination,
                        "expected_revision": expected_revision,
                    },
                    unconfirmed_action=f"the route change for instance {target['id']}",
                )
            ),
            "route change",
        )
        route_ack = payload.get("route") if isinstance(payload.get("route"), dict) else {}
        instance_ack = payload.get("instance") if isinstance(payload.get("instance"), dict) else {}
        mismatches = []
        if instance_ack.get("id") != target["id"]:
            mismatches.append("instance")
        if route_ack.get("request_model") != request_model:
            mismatches.append("request model")
        if route_ack.get("catalog_model") != destination:
            mismatches.append("destination")
        if not isinstance(expected_revision, int) or payload.get("revision") != expected_revision + 1:
            mismatches.append("revision")
        if mismatches:
            raise typer.BadParameter(
                "route acknowledgement mismatched the intended change "
                f"({', '.join(mismatches)}); effect unconfirmed — read the instance status before retrying"
            )
        if json_output:
            _print_json(payload)
            return
        route = payload.get("route", {})
        before_text = (
            f"provider {before.get('provider')} (upstream {before.get('upstream_model')})"
            if before
            else "unrouted"
        )
        stdout.print(f"instance {target['id']} route {request_model!r} switched", markup=False)
        stdout.print(f"  before: {before_text}", markup=False)
        stdout.print(f"  after:  provider {route.get('provider')} (upstream {route.get('upstream_model')})", markup=False)
        stdout.print(f"  applied revision: {payload.get('revision')}")
        stdout.print("in-flight requests keep the previous snapshot until completion")


@app.command("requests")
def requests_cmd(
    state_dir: Optional[Path] = StateDirOpt,
    offset: int = typer.Option(0, "--offset"),
    limit: int = typer.Option(50, "--limit"),
    instance: Optional[str] = typer.Option(None, "--instance", help="filter by exact instance ID"),
    json_output: bool = JsonFlag,
) -> None:
    """List bounded request metadata."""
    with structured_cli_errors(json_output):
        params: dict = {"offset": offset, "limit": limit}
        if instance is not None:
            params["instance_id"] = instance
        payload = _check(_call(state_dir, "GET", "/control/v1/requests", params=params))
        if json_output:
            _print_json(payload)
            return
        # Vertical blocks keep IDs, providers, upstream models and usage
        # complete and literal at 40/80/120 columns.
        for record in payload.get("requests", []):
            usage = record.get("usage") or {}
            usage_text = "unknown" if not usage else " ".join(
                f"{key.split('_')[0]}={value if value is not None else 'unknown'}"
                for key, value in sorted(usage.items())
            )
            stdout.print(f"request {record.get('id', '')}", markup=False)
            stdout.print(f"  instance: {record.get('instance_id', '')}", markup=False)
            stdout.print(f"  model: {record.get('request_model', '')}", markup=False)
            stdout.print(f"  provider: {record.get('provider_id') or ''}", markup=False)
            stdout.print(f"  upstream: {record.get('upstream_model') or ''}", markup=False)
            stdout.print(f"  revision: {record.get('route_revision') if record.get('route_revision') is not None else ''}", markup=False)
            stdout.print(f"  outcome: {record.get('outcome', '')}", markup=False)
            stdout.print(f"  status: {record.get('status_code') if record.get('status_code') is not None else ''}", markup=False)
            stdout.print(f"  usage: {usage_text}", markup=False)
        stdout.print(f"total: {payload.get('total')}, offset: {payload.get('offset')}, limit: {payload.get('limit')}")


def _qing_owned_tokens(argv: list[str]) -> list[str]:
    """qing-owned parser arguments: everything before the first native
    '--'. Tokens after it are native child arguments and are never
    interpreted as wrapper flags."""
    owned: list[str] = []
    for token in argv:
        if token == "--":
            break
        owned.append(token)
    return owned


def _machine_mode_requested(argv: list[str]) -> bool:
    return any(
        token == "--json" or token.startswith("--json=")
        for token in _qing_owned_tokens(argv)
    )


def _invokes_run_command(argv: list[str]) -> bool:
    """True when the first non-option token selects the `run` command."""
    for token in _qing_owned_tokens(argv):
        if not token.startswith("-"):
            return token == "run"
    return False


def main() -> int:
    # standalone_mode=False lets parser errors reach the handler below so
    # they can honour machine mode. Normal parser semantics are preserved:
    # human mode keeps click's usage output and exit code 2, and
    # post-parse command behaviour (including typer.Exit codes) is
    # returned unchanged.
    argv = sys.argv[1:]
    try:
        code = get_command(app).main(args=argv, standalone_mode=False, complete_var=None)
    except _click_exceptions.UsageError as exc:
        if not _machine_mode_requested(argv):
            exc.show()  # unchanged human output: usage and message on stderr
            return 2
        message = exc.format_message()
        if _invokes_run_command(argv):
            print(
                json.dumps({"event": "error", "code": "cli_error", "message": message}, sort_keys=True),
                file=sys.stderr,
                flush=True,
            )
        else:
            _print_json({"ok": False, "error": {"code": "cli_error", "message": message}})
        return 2
    return int(code or 0)


if __name__ == "__main__":
    raise SystemExit(main())
