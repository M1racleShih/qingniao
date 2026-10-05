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
repository (or from locally built artifacts — see below).

## Building and installing release artifacts

End users follow the README's [install-from-artifact path](../README.md#install-from-a-release-artifact);
this page is the developer path. To produce the release artifacts from a
clean checkout:

```
uv build            # produces dist/qingniao-<version>.tar.gz and .whl
```

Version consistency is enforced: `qing --version` (new in this release)
reports the package version through installed metadata, falling back to
`qingniao.__version__`; the wheel/sdist version, the package metadata and
`qing --version` must all agree (locked by `tests/test_cli.py::test_version_consistent_with_package_metadata`).

The sdist contains only public code and documentation; tests, experiments
and every personal path (agent skills, the private spec workflow) are
excluded by the packaging config in `pyproject.toml`. The include
patterns are root-anchored (gitignore semantics — a bare `README.md`
would match recursively) and the personal/agent-tool directories
(`.agents`, `.claude`, `.kimi`, `.pi`, `.maintainer`, `**/skills`) are
also listed in the exclude set, so even building from a working tree that
contains gitignored personal directories packages none of them.
`tests/test_distribution.py` rebuilds the sdist in-process with sentinel
personal READMEs and asserts none of them reach the artifact. A content
audit is part of the release acceptance (see
`experiments/clean-install-verification/` and the verification sections
in the README).

A clean Linux install from the wheel (no source checkout) was verified on
2026-10-05 in a minimal Ubuntu 24.04.3 container: `uv tool install` of the
wheel, `qing --version` matching the release version, one local synthetic
first request through `qing serve`, and `uv tool uninstall qingniao`
removing the CLI while the state directory is preserved. The reproduction
lives in `experiments/clean-install-verification/`.

Real import-path validation uses `experiments/real-provider-verification/run_import_validation.py`
(the same sanitization/budget guardrails as `run_validation.py`: exit 77
without credentials, a real-request cap with counting before every request,
no cross-provider retries, sanitized reports off-tree). See the README's
verification section for the 2026-10-05 result: MiniMax M3 passed the full
import path; zai-rate limiting blocked glm-5.3-flash within the budget.

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

Two credential sources exist:

1. **Environment variables** — export them in the gateway shell before
   `qing serve` (see above) and reference them as `credential_env`.
2. **Private credential store** — import an existing Claude settings file;
   the secret is stored as a 0600 file under `credentials/` in the state
   directory and referenced as `credential_id`. Plain local files with
   restrictive permissions, not an encrypted vault.

```
uv run qing config apply examples/qingniao-config.example.json
uv run qing config show
uv run qing config import-claude            # desensitized preview, zero writes
uv run qing config import-claude --apply    # commit one import transaction
```

The import reads one Claude settings file (explicit `--source PATH`, else
`$CLAUDE_CONFIG_DIR/settings.json`, else `~/.claude/settings.json`),
never modifies it, never executes hooks or `apiKeyHelper`, and keeps the
upstream model strings verbatim. `--apply` runs offline when the gateway
is stopped (the result is *saved*) and through the authenticated control
API when it is running (*applied*); both explicitly say the connection is
**not verified** — importing never contacts the provider. Decision flags
(`--auth`, `--primary-model`, `--aux-same-as-primary`, `--conflict`,
`--new-provider-id`) make it non-interactive; missing decisions fail with
zero writes. The first write that upgrades a legacy configuration
snapshots the old bytes as `config.backup.pre-v1.json`; the versioned
format is not guaranteed to be readable by older binaries, and rollback
means restoring that backup with the older program. Re-importing the same
source is a no-op; gateway tokens (`qn_`-prefixed) and the gateway's own
loopback address are refused as self-reference (reverse proxy aliases
cannot be detected offline).

See `examples/README.md` for the schema walkthrough. `qing defaults set`
replaces the complete defaults and only affects instances created
afterwards. `qing run --preview` shows the onboarding impact without
registering an instance or writing anything.

## Daily catalog management (entry-level CRUD)

`qing provider`, `qing credential` and `qing model` manage providers,
credential sources and models one entry at a time instead of rewriting
the whole configuration with `qing config apply` (which stays the
advanced whole-config entry point). Every command is non-interactive,
accepts `--json`, uses stable machine error codes and exit codes 0/1/2,
and every mutating command supports `--dry-run`. See `docs/api.md` for
the full command reference, JSON output contract and error-code table;
the examples below are reproducible against a running `qing serve`:

```
# env-sourced provider: the value lives in the gateway process env
uv run qing provider add my-provider --base-url https://api.example.com \
  --auth bearer --credential-env MY_TOKEN

# a named env credential in the catalog, referenced by id
uv run qing credential add shared-key --env MY_TOKEN
uv run qing provider add p2 --base-url https://other.example \
  --auth x-api-key --credential-id shared-key

# a private credential: the value enters through stdin/file only
uv run qing credential add --from-file /path/to/token

# models map catalog providers to exact upstream strings
uv run qing model add my-model --provider my-provider --upstream-model vendor/model

uv run qing provider list && qing model list && qing credential list
uv run qing provider set my-provider --base-url https://new.example
uv run qing model rm my-model          # fails with entry_in_use if referenced
uv run qing provider rm my-provider    # fails while a model references it
```

Mutating commands are read-modify-write transactions through the running
gateway (GET config → change one entry → conditional PUT with
`expected_generation`): a concurrent edit fails with
`generation_conflict` instead of silently overwriting, and a stopped
gateway fails with the change unconfirmed. Deletes are protected by
reference checks — no cascades, no silent rewrites. Credential values
never appear on the command line, in output, JSON, errors or logs;
`credential show`/`list` return metadata only, and private store
versions are immutable (removing a catalog entry never deletes a store
version; that follows the existing recovery/version rules).

The optional `credentials` top-level configuration section stores the
catalog (`{"source": "env", "env": NAME}` or `{"source": "private"}`
entries); it is additive to format version 1, absent sections behave as
empty and are **not serialized** (a configuration without catalog
credentials never carries the key), and providers may reference a
catalog id (resolved by the gateway at request time) or a direct
`cred_<hex>` private id. Compatibility boundary: configurations that
**do** carry the `credentials` section require this build — older
builds reject them as an unknown top-level field. Before rolling back to
an older program, remove the section from the configuration or restore
the pre-upgrade backup (`config.backup.pre-v1.json`).

## Project layout

- `src/qingniao/config.py` — shared configuration schema (format version 1 with
  legacy read-only compatibility), validation, atomic persistence, generation;
  the optional `credentials` catalog section is parsed and cross-validated here
- `src/qingniao/gateway.py` — instances, immutable typed-credential route
  snapshots, leases, CAS updates, the serialized write path; catalog
  credential entries resolve to env/private sources at request time
- `src/qingniao/catalog.py` — entry-level catalog logic: entry views,
  referential integrity checks, credential resolution, edited-schema
  validation (no I/O; the CLI owns the transaction)
- `src/qingniao/state.py` — state directory, discovery file, single-gateway guard
- `src/qingniao/credentials.py` — private immutable credential store
  (0700/0600, symlink/owner/mode/type checks, fsynced writes)
- `src/qingniao/claude_source.py` — one-file Claude settings parsing into
  import candidates (no execution of hooks or helpers)
- `src/qingniao/importing.py` — desensitized planning: exact model mapping,
  decisions, dedup, conflicts, self-reference, wire encoding
- `src/qingniao/transactions.py` — the single logical import commit:
  staging, backup, receipt, recovery, orphan cleanup, operation queries
- `src/qingniao/control_api.py` — `/control/v1` administration endpoints
  (config, imports, operations, instances, requests)
- `src/qingniao/proxy_api.py` — `/v1/messages` forwarding to upstreams
- `src/qingniao/app.py`, `src/qingniao/serve.py` — ASGI assembly and
  foreground server (startup recovery before accepting anything)
- `src/qingniao/cli.py` — `qing` command line interface (including `--version`)
- `tests/` — unit, ASGI-level, real-TCP and CLI-subprocess end-to-end tests
- `experiments/real-provider-verification/` — real-provider validation
  harnesses (`run_validation.py`, `run_import_validation.py`) with the
  sanitization/budget guardrails
- `experiments/clean-install-verification/` — Docker clean-install
  acceptance (Dockerfile + in-container checker + reproduction notes)

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
- imports: parser and planner matrices (sources, decisions, dedup,
  conflicts, self-reference), transaction boundaries with fault
  injection and recovery (orphan cleanup, damaged records refusing
  writes/startup), the authenticated import and operation-query API
  (races, retries, size limit, sanitization), rotation keeping snapshots
  and in-flight streams on immutable versions, CLI matrices (exit codes,
  missing-decision zero writes, JSON, 40/80/120 columns, NO_COLOR and
  redirect), synthetic-secret scans across every artifact, and the local
  end-to-end matrix: temp HOME, synthetic settings, no provider
  variables, offline/online import, a real gateway process, a fake
  Claude client through `qing run`, restart persistence — local fixtures
  only, never a real-provider claim
- entry-level catalog CRUD: schema round-trips and cross-validation of
  the `credentials` section, entry views and referential integrity,
  credential resolution, a CLI matrix against a real gateway (JSON
  structure, exit codes 0/1/2, dry-run zero writes, no-op sets,
  help snapshots, 40/80/120 widths, NO_COLOR), two concurrent
  read-modify-write writers on one generation with exactly one winner,
  and an end-to-end chain where two providers/two models created through
  the new commands serve requests to synthetic upstreams and a live
  `route set` switch reaches the expected upstream with the expected
  model string; synthetic-secret scans cover the full credential path
  (stdin/file input, argv rejection, metadata-only output, unreachable
  gateway failures, state artifacts)

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

- Real-provider evidence covers only the validated configurations;
  every other provider, endpoint and model is untested, and the loopback
  synthetic fixtures remain the regression baseline.
- As of 2026-10-05 the real import path is validated end to end for
  MiniMax M3 only; **glm-5.3-flash answered every preflight attempt with
  429 rate_limit_error** within the acceptance window, so its full import
  leg and the optional real-`claude` leg did not run (the real-request
  budget of 10 was fully and honestly consumed; everything was counted
  before firing). That is a recorded gap that must be closed before the
  import path can be claimed for both authorized configurations. Reverse
  proxy aliases that loop back to the gateway cannot be detected by the
  offline self-reference check.
- A **public** published installation path does not exist yet: the clean
  Linux install from a locally built artifact is verified, but the public
  package name is still being decided (PyPI `qingniao` is occupied) and
  publishing waits for an explicit maintainer authorization.
- The launcher has been exercised against Claude Code 2.1.251 (2026-09-08
  acceptance) and Claude Code 2.1.274 is present in this environment; a
  fresh `qing run` acceptance run within this release slice was skipped
  when the real-request budget ran out.
