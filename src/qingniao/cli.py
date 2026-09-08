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

from . import state as state_mod
from .serve import run_serve

app = typer.Typer(add_completion=False, help="Qingniao gateway control")
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
    """CLI failure with a stable machine code and a human message."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@contextlib.contextmanager
def structured_cli_errors(json_output: bool):
    """With --json, CLI failures are machine-identifiable JSON on stdout."""
    try:
        yield
    except typer.BadParameter as exc:
        if not json_output:
            raise
        _print_json({"error": {"code": "cli_error", "message": str(exc)}})
        raise typer.Exit(code=1) from exc
    except CliError as exc:
        if not json_output:
            stderr.print(f"error: {exc.message}")
        else:
            _print_json({"error": {"code": exc.code, "message": exc.message}})
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
        stdout.print("providers:")
        for pid, p in config.get("providers", {}).items():
            stdout.print(f"  {pid}: {p.get('base_url')} ({p.get('auth')}, credential env {p.get('credential_env')})")
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


def _parse_route_pair(pair: str) -> tuple[str, str]:
    if "=" not in pair:
        raise typer.BadParameter(f"route must be REQUEST=DEST, got {pair!r}")
    left, right = pair.split("=", 1)
    left, right = left.strip(), right.strip()
    if not left or not right:
        raise typer.BadParameter(f"route must be REQUEST=DEST, got {pair!r}")
    return left, right


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
        config["defaults"] = {"model": model, "aux_model": aux_model, "routes": routes}
        payload = _require_applied(
            _check(_call(state_dir, "PUT", "/control/v1/config", json=config, unconfirmed_action="the defaults update")),
            "defaults update",
        )
        if json_output:
            _print_json(payload)
            return
        stdout.print(f"defaults applied (config revision {payload.get('revision')})")
        stdout.print("changes affect new instances only; existing instances keep their snapshots")


@app.command("run", context_settings={"ignore_unknown_options": True, "allow_extra_args": True})
def run(
    ctx: typer.Context,
    label: Optional[str] = typer.Option(None, "--label", help="display label for targeting"),
    model: Optional[str] = typer.Option(None, "--model", help="main request model (route key)"),
    aux_model: Optional[str] = typer.Option(None, "--aux-model", help="auxiliary request model (route key)"),
    route: list[str] = typer.Option([], "--route", help="REQUEST=DEST route override (repeatable)"),
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
            _print_json({"error": {"code": "cli_error", "message": message}})
        return 2
    return int(code or 0)


if __name__ == "__main__":
    raise SystemExit(main())
