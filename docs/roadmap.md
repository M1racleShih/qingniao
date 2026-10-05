# The first useful Qingniao

The first milestone is one complete terminal workflow: connect Claude Code once, select between two configured models from different providers, and inspect what happened to each request.

Status: the core development slice is implemented and reproducible from this repository with `uv` — loopback gateway, shared catalog with validation (explicit format version 1 with a pre-upgrade backup of legacy files), per-instance routing with CAS updates, streaming and non-streaming forwarding with sanitized request records, the `qing run` launcher for Claude Code (fresh identity per launch, temporary 0600 settings injection, lease renewal, signal forwarding), the terminal/JSON output contract, `qing config import-claude` with a local private credential store (one-transaction import, dedup, rotation keeping existing instances on their snapshots, recovery), and entry-level catalog CRUD (`qing provider` / `qing credential` / `qing model`).

As of 2026-10-05 the release path is being assembled: the release **artifact** builds (sdist + wheel, audited clean) and installs in a clean minimal Ubuntu 24.04 container (verified with Docker 28.4.0 — `qing --version`, a local synthetic first request, and documented uninstall that preserves user data), and the real import path is validated end to end for **MiniMax M3** (preflight, offline import with private store, gateway first requests, restart persistence; 10/10 real-request budget used, all counted). **glm-5.3-flash was rate-limited (429 rate_limit_error) on every preflight probe across the acceptance session** (hours apart), so its full import leg and the optional real-`claude` `qing run` leg did not run within the budget — those are recorded gaps, not claims of success. Earlier real-provider evidence (2026-09-08) covers both configurations through `qing run`. No public publish has happened: the PyPI name `qingniao` is occupied, so the distribution package name is `qingniao-gateway`, and publishing (PyPI upload, GitHub Release, tag push) waits for explicit maintainer approval.

Python is the selected primary language for the first gateway and `qing` CLI. Specific libraries and distribution tooling remain implementation choices.

## First runnable release

- A local gateway supporting two independently verified Anthropic-compatible upstream providers.
- Exact model-to-provider routing, with requested and actual model names recorded separately.
- Streaming replies and a complete tool-call round trip through Claude Code.
- Shared provider, credential, and model configuration and connection checks through `qing`, with independent model selection and route changes for each Claude Code running instance.
- Defaults for new instances; changing defaults or one instance does not change other existing instances. Switch results identify the affected instance.
- Live route updates for new requests, with each request in progress retaining its original destination.
- A polished [terminal experience](terminal-experience.md), including readable layouts, complete state feedback, and plain output for redirected streams.
- Claude Code settings import (`qing config import-claude`) is implemented for one explicit settings file with a desensitized preview and an untouched source; broader settings management (narrowly scoped updates of global settings, restoration flows) is not.
- Entry-level catalog management (`qing provider`, `qing credential`, `qing model`) is implemented and verified locally: add/list/show/set/rm without rewriting the whole configuration, dry-run previews, generation-protected concurrent edits, reference-protected deletes and secret-safe credential input (stdin/file only); `qing config apply` remains the advanced whole-config entry point.
- Request history in the terminal showing destination, outcome, timing, and provider-reported usage. Incomplete usage is marked unknown.
- A usable `qing` command and a locally verified install path (build, clean Linux install, uninstall-with-data-preservation) are done; a **public** published installation path is still pending the public package-name decision and an explicit release authorization.

Remaining before we can call the first release ready: resolve the glm-5.3-flash rate-limit gap (repeatedly 429; its full real import leg still needs to run, likely requiring the maintainer to check the key/account quota). The distribution package name is now `qingniao-gateway`; the actual publish step (PyPI upload, GitHub Release, tag push) still waits for an explicit maintainer authorization.

The release is ready only when this workflow is demonstrated against the documented client and provider versions. A mocked response alone does not establish provider compatibility.

## After that workflow is reliable

Priorities will follow user feedback. Candidates include a web GUI, optional pi and Kimi Code integration, shared configuration exports, provider quota adapters, account fallback, and additional operating systems.

Cross-protocol translation and automatic switching between different models need explicit capability and failure semantics. Neither is a first-release promise.

## How progress will be presented

Completed features will be linked to their implementation and verification evidence. The README will gain a real quick start and an actual demo when there is a runnable release. Until then, it will not show invented installation commands, performance claims, or compatibility badges.
