# The first useful Qingniao

The first milestone is one complete workflow: connect Claude Code once, select between two configured models from different providers, and inspect what happened to each request.

Status: project foundation only. All runtime capabilities below are planned.

## First runnable release

- A local gateway supporting two independently verified Anthropic-compatible upstream providers.
- Exact model-to-provider routing, with requested and actual model names recorded separately.
- Streaming replies and a complete tool-call round trip through Claude Code.
- Provider and model configuration, connection checks, and route changes through a local control panel.
- Claude Code settings preview, backup, narrowly scoped updates, and restoration.
- Request history showing destination, outcome, timing, and provider-reported usage. Incomplete usage is marked unknown.
- A usable `qing` command, a published installation path, and tested documentation for Linux.

The release is ready only when this workflow is demonstrated against the documented client and provider versions. A mocked response alone does not establish provider compatibility.

## After that workflow is reliable

Priorities will follow user feedback. Candidates include optional pi and Kimi Code integration, shared configuration exports, provider quota adapters, account fallback, and additional operating systems.

Cross-protocol translation and automatic switching between different models need explicit capability and failure semantics. Neither is a first-release promise.

## How progress will be presented

Completed features will be linked to their implementation and verification evidence. The README will gain a real quick start and an actual demo when there is a runnable release. Until then, it will not show invented installation commands, performance claims, or compatibility badges.
