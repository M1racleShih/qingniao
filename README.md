<p align="center">
  <img src="docs/assets/qingniao-mark.png" width="144" alt="Qingniao's blue-green messenger bird" />
</p>

<h1 align="center">Qingniao</h1>

<p align="center"><strong>English</strong> · <a href="README.zh-CN.md">简体中文</a></p>

<p align="center"><strong>Switch models. Keep your flow.</strong></p>
<p align="center">A local model gateway for AI coding agents, starting in the terminal.<br />Starting with Claude Code. Built with more agents in mind.</p>

<p align="center">
  <a href="#the-experience-were-building">The experience</a> ·
  <a href="#can-i-try-it">Try it</a> ·
  <a href="docs/roadmap.md">Roadmap</a> ·
  <a href="CONTRIBUTING.md">Contribute</a>
</p>

> **Early development — runnable core, release path being assembled.** The gateway, the `qing` CLI, per-instance model routing, the Claude Code launcher and the `qing config import-claude` flow (settings import with a local private credential store) are implemented. The release **artifact** is buildable and installable (clean Linux install verified inside a minimal Ubuntu 24.04 container on 2026-10-05, Docker 28.4.0), but there is **no public registry release yet** — the PyPI name `qingniao` is occupied by an unrelated project, and the public package name is still being decided. Real-provider evidence: MiniMax M3 is fully validated through the import path (preflight, offline import, private store, gateway first requests, restart persistence); glm-5.3-flash was **rate-limited (429) at validation time** and its full import leg could not be run within the real-request budget — that gap is recorded, not papered over. Earlier `qing run` real-provider evidence (2026-09-08) covers both configurations; the optional fresh claude run in this acceptance was skipped when the request budget ran out. No other provider, endpoint or model has been verified.

## Your next model shouldn't need another config file

You want one model for everyday coding and another for a difficult bug. They happen to come from different providers. Now switching models also means switching endpoints, credentials, and settings. Later, you wonder which account handled the request and how much it used.

Qingniao is being built to take care of those connections. Point your coding agent at one local address, choose a model, and let an explicit route send the request to its configured provider.

Your agent stays where you work. Qingniao keeps the connections in order.

## The experience we're building

The first release will be operated through `qing` in the terminal. Clear layouts, readable status feedback, and useful errors are part of the [planned terminal experience](docs/terminal-experience.md). A web GUI may follow based on actual usage.

**Connect once.** Import your existing Claude settings with `qing config import-claude` — a desensitized preview first, then one explicit apply that stores the credential in a local private store (plain 0600 files, not an encrypted vault) and never touches your original file. Or manage the catalog one entry at a time with `qing provider`, `qing credential` and `qing model` — add, list, show, set and remove providers, credential sources and models without rewriting the whole configuration (advanced whole-config edits still go through `qing config apply`). The commands are non-interactive and agent-friendly: stable `--json` output, machine error codes, `--dry-run` previews, reference-protected deletes, and credential values that only enter through stdin or a file — never the command line.

**Switch without the setup ritual.** Share provider and credential configuration across your Claude Code instances while choosing models independently in each one. Use `qing` to change a route for a specific instance without changing other instances. After the gateway confirms the update, new requests from that instance use the new route without a restart; requests already in progress keep their original destination. Default changes apply only to new instances.

**See where every request went.** Inspect the requested model, actual upstream model, provider, timing, and reported token usage together in the terminal. Missing usage stays unknown; estimated costs are labeled as estimates.

For example, this is the routing experience we want to make visible:

```text
Claude Code                   Qingniao                 Your providers
  selected model A  ──────►    exact model route  ───►   provider A / model A
  selected model B  ──────►    exact model route  ───►   provider B / model B
                                   │
                              request history
```

These are illustrative routes, not a working configuration example. The first release targets providers with verified Anthropic-compatible endpoints; it will not assume every model or protocol is interchangeable.

## Already using pi or Kimi Code?

Keep using their model pickers. Their native multi-provider support may already be enough for you.

Qingniao's longer-term role is to offer shared credentials, routing, and request history when you want them. Connecting those agents will be optional. They are outside the first release's compatibility scope.

## Can I try it?

Two paths exist. The **install path** starts from a locally built release
artifact (`.whl`) — see [Install from a release artifact](#install-from-a-release-artifact)
below. The **development path** runs straight from this repository with
`uv` (see [docs/development.md](docs/development.md)). The intended CLI
command is `qing`; the distribution package name is `qingniao-gateway`
(`qingniao` on PyPI is an unrelated project). So far only locally built
artifacts are installed; no public publish step has been performed.

**Development path (from this repository):**

```
uv sync --locked --extra dev
uv run qing config import-claude            # preview importing ~/.claude/settings.json (zero writes)
uv run qing config import-claude --apply    # save the connection + private credential, source untouched
uv run qing serve &                 # loopback gateway (control token in its state dir)
uv run qing run --label work        # registers an instance and launches Claude Code
uv run qing instances               # routes, revisions, per-instance state
uv run qing requests                # sanitized per-request metadata (usage unknown vs real 0)
```

Alternatively skip the import and reference exported variables (`export
MY_PROVIDER_TOKEN=...` before `qing serve`; the value is never stored in
the configuration), or manage the catalog one entry at a time —
non-interactive, `--json`-friendly and secret-safe:

```
uv run qing provider add my-provider --base-url https://api.example.com \
  --auth bearer --credential-env MY_PROVIDER_TOKEN
uv run qing model add my-model --provider my-provider --upstream-model vendor/model
uv run qing provider list && qing model list
```

`qing credential add --env NAME` registers a named env credential that
providers reference by id; `qing credential add --from-stdin` (or
`--from-file`) stores a private credential whose value never appears in
argv, output, JSON, errors or logs. `--dry-run` previews every change;
deletes refuse to break references. `qing run --preview` shows what
attaching would do without registering anything.

Credentials must be exported in the gateway's shell **before** `qing serve`; exporting later in another shell does not affect a running gateway. `qing run` gives every launch (including `--resume`) a fresh instance identity, injects the transport through a temporary 0600 settings file (never in argv), preserves your Claude settings, history and tools, and reports honestly: registered vs. started vs. first gateway-observed request. See [docs/development.md](docs/development.md) and [docs/api.md](docs/api.md).

## Install from a release artifact

Build the wheel locally (see [docs/development.md](docs/development.md) —
`uv build`), then install it with your Python tooling of choice. The
primary documented path uses [uv](https://docs.astral.sh/uv/), which
manages an isolated environment and puts `qing` on your `PATH`:

```
uv tool install qingniao_gateway-0.1.0-py3-none-any.whl
qing --version        # prints the release version (0.1.0)
```

An equivalent `python3 -m venv venv && venv/bin/pip install
qingniao_gateway-0.1.0-py3-none-any.whl` also works (the `qing` script
lands in `venv/bin/`). The runtime needs Python 3.12+ and only the
declared dependencies (typer, rich, httpx, starlette, uvicorn) — no
network is needed after installation.

**Uninstall and your data.** `uv tool uninstall qingniao-gateway` (or `pip
uninstall qingniao-gateway`) removes the CLI and the package files but
**keeps your user data** — the state directory
(`$XDG_STATE_HOME/qingniao`, else `~/.local/state/qingniao`) with the
shared configuration, private credentials and sanitized request records
is preserved by default. To remove it too, delete the state directory
yourself; nothing is removed implicitly. This policy is enforced by the
clean-install acceptance.

## Verification (2026-10-05)

- **Real import-path validation** (`experiments/real-provider-verification/run_import_validation.py`):
  with the real request budget capped at 10 (used: exactly 10, all counted
  before firing), MiniMax M3 (`MiniMax-M3` at
  `https://api.minimaxi.com/anthropic`, `Authorization: Bearer`) passed a
  full import-path run: preflight 200 with the exact model echo, offline
  `config import-claude` preview with zero writes, `--apply` storing the
  credential in the private store (no key in the configuration), gateway
  first requests with the correct upstream model string and faithfully
  recorded usage, and a private credential usable after a gateway restart.
  glm-5.3-flash (`https://open.bigmodel.cn/api/anthropic`) answered the
  preflight with **429 rate_limit_error** on both attempts and its import
  leg was not run; that provider-side block is recorded as a gap, not
  bypassed. The optional real-`claude` `qing run` leg was skipped when the
  budget ran out.
- **Clean Linux install** (`experiments/clean-install-verification/`): in a
  minimal Ubuntu 24.04.3 container (Docker 28.4.0, no source checkout), the
  wheel installed via `uv tool install`, `qing --version` printed `0.1.0`,
  the gateway served a local synthetic first request (upstream observed the
  correct model string), and after `uv tool uninstall qingniao-gateway`
  the CLI was gone while the state directory was preserved.
- Earlier real-provider evidence (2026-09-08, `qing run` on both
  configurations) is recorded in [docs/development.md](docs/development.md).

These tested configurations do not imply broad provider compatibility.
No real provider was reached through a publicly published install (none
exists yet).

Watch this repository for the first runnable release, or help us build it. The [roadmap](docs/roadmap.md) shows the first milestone. If switching providers keeps interrupting your work, open an issue with your agent, provider, and the step that gets in your way. Please leave out credentials and private prompts.

## Built for the way you work

These principles guide the implementation:

- **Explicit destinations.** An unknown model should produce a useful error. Switching to a different model should be a visible choice.
- **Honest accounting.** Observed token usage, estimated cost, and provider subscription quotas are different things.
- **Local control.** The gateway and its request history will run locally. Model requests still go to the providers you configure; this is not offline inference.
- **Small, verifiable compatibility steps.** Streaming, tool calls, and model capabilities matter as much as a successful text reply.

## Why “Qingniao”?

In Chinese mythology and literature, the blue-green bird is a messenger. Qingniao carries that idea into a developer tool: a dependable connection between where you work and the models you choose.

The logo is an original AI-assisted brand concept. Qingniao is an independent project, with no affiliation with the agent or model vendors mentioned here.

## Build it with us

Good bug reports, provider compatibility findings, clear documentation, and thoughtful code all help. Start with the [contribution guide](CONTRIBUTING.md). An issue and a pull request are enough; no particular specification tool is required.

We learned from [flexible-gateway](https://github.com/Agony5757/flexible-gateway), especially its focused approach to model routing and reversible Claude Code configuration. Qingniao is an independent implementation; this foundation does not contain code copied from that project.

## License

[MIT](LICENSE).
