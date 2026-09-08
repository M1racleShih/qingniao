# Development

Qingniao is under active development. The package is not published to any
registry; all commands below reproduce the current development state from
this repository.

## Prerequisites

- Linux
- Python 3.12 or newer (the requirement for this development stage)
- [uv](https://docs.astral.sh/uv/) 0.9.x or newer

## Setup and tests

```
uv sync --locked --extra dev
uv run pytest
uv run qing --help
uv run python -m qingniao --help
```

The lockfile (`uv.lock`) pins exact dependency versions resolved with uv.
There is no published-package claim: installation happens from this
repository only.

## Running the gateway locally

Providers read their credentials from the gateway process environment at
request time. Export every credential the configuration references in the
same shell **before** starting the gateway — exporting a value later from
another shell does not update a running gateway; restart `qing serve` to
pick it up:

```
export QING_DEV_FIXTURE_TOKEN=...   # referenced by examples/qingniao-config.example.json
uv run qing serve
```

The gateway runs in the foreground, binds 127.0.0.1 only, picks a free port,
and writes a 0600 discovery file (`gateway.json`) with the port, PID and
control token into its state directory:

- `--state-dir DIR` overrides the default (`$XDG_STATE_HOME/qingniao`, else
  `~/.local/state/qingniao`); useful for reproducible test runs
- `--port N` binds a specific loopback port (default: any free port)

Only one gateway may run per state directory; a second `qing serve` exits
with an error instead of disturbing the running one.

## First configuration

Credentials live in the gateway process environment, never in files (see
above: export them before `qing serve`):

```
uv run qing config apply examples/qingniao-config.example.json
uv run qing config show
```

See `examples/README.md` for the schema walkthrough. `qing defaults set`
replaces the complete defaults and only affects instances created afterwards.

## Project layout

- `src/qingniao/config.py` — shared configuration schema, validation, atomic persistence
- `src/qingniao/gateway.py` — instances, immutable route snapshots, leases, CAS updates
- `src/qingniao/state.py` — state directory, discovery file, single-gateway guard
- `src/qingniao/control_api.py` — `/control/v1` administration endpoints
- `src/qingniao/proxy_api.py` — `/v1/messages` forwarding to upstreams
- `src/qingniao/app.py`, `src/qingniao/serve.py` — ASGI assembly and foreground server
- `src/qingniao/cli.py` — `qing` command line interface
- `tests/` — unit, ASGI-level and real-TCP end-to-end tests

## Test suite

`uv run pytest` covers:

- configuration validation and atomic apply (invalid input changes nothing)
- instance snapshots: shared catalog, per-instance resolution, defaults
  changes affecting only new instances, shared edits/deletions keeping old
  snapshots, explicit selection winning over defaults
- targeting: exact ID, unique label, ambiguity, missing/invalid/ended/expired
  targets failing without mutation
- leases with a controlled clock (no sleeps), renewal, end semantics
- control API auth separation (admin token vs instance tokens), pagination
  bounds, CAS route updates
- forwarding against real loopback upstreams: model rewrite, credential
  injection, local auth stripping, protocol header forwarding, gzip SSE
  decoding, unknown model / ended / expired / invalid token / missing
  credential all failing before any upstream call
- streaming: chunk-fragmented and CRLF/LF/CR SSE parsing, bounded parser
  state, cumulative (never summed) usage, missing/zero/invalid usage,
  incomplete streams without message_stop, SSE error events, HTTP errors,
  in-progress and final record states
- CLI acknowledgement semantics: applied-then-delayed timeout reporting
  unconfirmed with applied readback, malformed or mismatched
  acknowledgements never reported as success, CAS conflicts
- real TCP tests: `serve` subprocess, restart invalidation, the
  single-gateway guard, A1/A2/B1 snapshot integrity across a catalog edit
  and route switch, and connection release proven by upstream EOF
  observation for cancellations before headers, mid-stream, and during
  buffered/error body reads

## Running Claude Code through the launcher

```
# The example file references QING_DEV_FIXTURE_TOKEN (a loopback dev
# fixture) and QING_EXAMPLE_PROVIDER_TOKEN (a placeholder https endpoint).
# Export what your applied config references BEFORE `qing serve`; exporting
# later in another shell does not affect a running gateway. For real use,
# write your own config pointing at a verified Anthropic-compatible
# endpoint — the example's placeholder endpoints are not usable as-is.
export QING_DEV_FIXTURE_TOKEN=...   # or your own credential env names
uv run qing serve &
uv run qing config apply examples/qingniao-config.example.json
uv run qing run --label work        # registers an instance, launches `claude`
# Overrides use request-model keys and catalog model IDs that exist in the
# applied config; with the example, req-aux is a route key and example-main
# is a catalog destination:
uv run qing run --label debug --model req-aux --route req-aux=example-main
uv run qing run -- --resume "<session-id>" -p "continue" --output-format json
```

`qing run` gives every launch (including `--resume`) a fresh instance identity, injects the gateway transport and selected request models through a temporary 0600 `--settings` file (the token never appears in argv), neutralizes conflicting ambient/project credentials and provider switches, preserves your Claude settings sources, session history, tools and unrelated environment, renews the 30 s lease every 10 s, and forwards SIGINT/SIGTERM with bounded reaping. On normal exit and for signals the wrapper handles, the registration is ended and the temporary settings file is removed. If the wrapper itself dies abruptly (for example `SIGKILL` or a crash), the instance ends only when its lease expires and a residual temporary settings file may remain on disk; the token inside is invalid once the lease has expired, and the leftover file can be deleted manually. Status messages distinguish *registered*, *child started* and *first gateway-observed request*; upstream readiness is never claimed early. Native `--settings`, `--model` and `--bare` arguments are rejected because the launcher owns the transport.

The launcher is verified two ways: `tests/test_launcher.py` uses a fake client for lifecycle, cleanup, signal, renewal-failure and identity regressions, and `experiments/claude-launcher-verification/run_experiment.py` runs two real same-cwd Claude Code processes through the formal `qing run` entry against loopback synthetic upstreams (per-instance main/auxiliary/tool-result chains, distinct identities, settings-file precedence over conflicting project settings, cleanup and unchanged real HOME). That experiment is local synthetic evidence, not real-provider compatibility.

Real-provider validation (`experiments/real-provider-verification/run_validation.py`) additionally runs two persistent same-cwd Claude Code sessions through the formal `qing run` entry against the two authorized real upstreams. Verified on 2026-09-08 with Claude Code 2.1.251 and Python 3.13.9: distinct concurrent identities with 0600 temporary settings files, an `applied` route ACK received while the first request was still in flight, cross-model tool-result continuation (tool_use on MiniMax-M3, the tool-result continuation of the same Claude process on glm-5.3-flash — asserted from sanitized native stream-json tool facts: the `Read` tool_use matched to its `tool_result` by opaque ID hash with `is_error` false — plus the reply marker), an instance that inherited the previous defaults keeping its snapshot unchanged across both a live route update and a live defaults update, defaults affecting only new instances, and explicit `--model` precedence. Hold middleware delays one upstream connection for timing only; TLS content is never read or synthesized.

| Provider endpoint | Exact model ID | Credential env | Auth header |
| --- | --- | --- | --- |
| `https://api.minimaxi.com/anthropic` | `MiniMax-M3` | `MINIMAX_API_KEY` | `Authorization: Bearer` |
| `https://open.bigmodel.cn/api/anthropic` | `glm-5.3-flash` | `ZAI_API_KEY_TEAM` | `Authorization: Bearer` |

Reproduce with both credentials exported in the gateway shell: `uv run --locked python experiments/real-provider-verification/run_validation.py`. The intended request budget is 30 provider message requests with at most 2 concurrent, checked **between scenario stages** — it is a budget guard, not a hard per-message guarantee; the final acceptance run observed 6 provider message requests, all documented attempts together observed 24, and observed concurrency was 1. The script exits 77 without credentials and writes a sanitized JSON report (ids, models, revisions, outcomes, usage numbers; never prompts, replies or credentials). Fresh reproductions start from a zero request ledger; the 2026-09-08 acceptance itself spent 24 message requests cumulatively across all documented attempts (6 in the final successful run), and resumed attempts can pass an explicit prior ledger via `QING_REAL_PRIOR_LEDGER`. This is evidence for exactly the two configurations above — it does not imply compatibility with any other provider, endpoint, or model.

## Current limitations

- Real-provider evidence covers only the two validated configurations above; every other provider, endpoint and model is untested, and the loopback synthetic fixtures remain the regression baseline.
- Claude Code settings preview/backup/restore and a published installation
  path are not implemented (first-release items).
- The launcher has been exercised against Claude Code 2.1.251 on Linux only.
