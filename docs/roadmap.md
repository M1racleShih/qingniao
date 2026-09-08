# The first useful Qingniao

The first milestone is one complete terminal workflow: connect Claude Code once, select between two configured models from different providers, and inspect what happened to each request.

Status: the core development slice is implemented and reproducible from this repository with `uv` — loopback gateway, shared catalog with validation, per-instance routing with CAS updates, streaming and non-streaming forwarding with sanitized request records, the `qing run` launcher for Claude Code (fresh identity per launch, temporary 0600 settings injection, lease renewal, signal forwarding), and the terminal/JSON output contract. Not yet done: verification against real provider endpoints, configuration preview/backup/restore, a published installation path, and tested clean Linux installs. Those remain part of the first runnable release below.

Python is the selected primary language for the first gateway and `qing` CLI. Specific libraries and distribution tooling remain implementation choices.

## First runnable release

- A local gateway supporting two independently verified Anthropic-compatible upstream providers.
- Exact model-to-provider routing, with requested and actual model names recorded separately.
- Streaming replies and a complete tool-call round trip through Claude Code.
- Shared provider, credential, and model configuration and connection checks through `qing`, with independent model selection and route changes for each Claude Code running instance.
- Defaults for new instances; changing defaults or one instance does not change other existing instances. Switch results identify the affected instance.
- Live route updates for new requests, with each request in progress retaining its original destination.
- A polished [terminal experience](terminal-experience.md), including readable layouts, complete state feedback, and plain output for redirected streams.
- Claude Code settings preview, backup, narrowly scoped updates, and restoration.
- Request history in the terminal showing destination, outcome, timing, and provider-reported usage. Incomplete usage is marked unknown.
- A usable `qing` command, a published installation path, and tested documentation for Linux.

The release is ready only when this workflow is demonstrated against the documented client and provider versions. A mocked response alone does not establish provider compatibility.

## After that workflow is reliable

Priorities will follow user feedback. Candidates include a web GUI, optional pi and Kimi Code integration, shared configuration exports, provider quota adapters, account fallback, and additional operating systems.

Cross-protocol translation and automatic switching between different models need explicit capability and failure semantics. Neither is a first-release promise.

## How progress will be presented

Completed features will be linked to their implementation and verification evidence. The README will gain a real quick start and an actual demo when there is a runnable release. Until then, it will not show invented installation commands, performance claims, or compatibility badges.
