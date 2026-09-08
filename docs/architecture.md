# Architecture

Status: foundation stage of `add-instance-scoped-routing`. This document
describes what is implemented in this repository today.

## Overview

Qingniao is a loopback gateway that lets several Claude Code processes share
one connection catalog while each keeps an independent, hot-switchable
routing selection. One foreground gateway process owns a state directory and
serves two HTTP planes on a single 127.0.0.1 listener:

- `/control/v1` — administration (configuration, instances, routes),
  authenticated with a per-run control token
- `/v1/messages`, `/v1/messages/count_tokens` — Anthropic-compatible
  forwarding, authenticated with per-instance bearer tokens

```
Claude/HTTP client --bearer(instance)--> gateway /v1/* --credential(provider env)--> upstream provider
CLI (qing ...) ------bearer(control)---> gateway /control/v1/*
```

## State

Only the shared configuration is persisted (`config.json` in the state
directory, 0600, written atomically via tmp-file + rename + fsync). A
runtime discovery file (`gateway.json`, 0600) exists only while a gateway
runs and carries the port, PID and control token; it is removed on shutdown
and only by the process that owns it (PID-checked). An advisory `flock` on
`gateway.lock` guarantees a single gateway per state directory; startup
failures clean up everything they created.

Everything else is in memory and intentionally dies with the process:

- instances (IDs, token hashes, route snapshots, revisions, leases)
- the control token
- bounded request metadata (max 1000 distinct records, sanitized facts
  only), listed via `GET /control/v1/requests` and `qing requests`

Consequence: a gateway restart invalidates every instance token. Old
clients fail closed with `invalid_instance_token` and must register again;
the shared catalog and defaults survive in `config.json`.

## Shared configuration

Three sections (see `examples/qingniao-config.example.json`):

- `providers`: id -> `{base_url, credential_env, auth}` — an API root
  (http/https, no userinfo/query/fragment; `/v1/...` paths are appended at
  forwarding time), an environment variable name that holds the credential,
  and the injection style (`bearer` or `x-api-key`). No credential values
  are ever stored.
- `models`: id -> `{provider, upstream_model}` — catalog destinations.
- `defaults`: `{model, aux_model, routes}` — the default main/aux request
  model strings and the default exact-match route table
  (request string -> catalog model ID). Both defaults must be keys in
  `routes` when set.

`PUT /control/v1/config` (and `qing config apply`) validates the complete
object, persists it atomically, and only then swaps the in-memory catalog.
Invalid input changes nothing — not the file, not the running catalog, not
any instance.

## Instances and snapshots

An instance is one attached client identity, created via
`POST /control/v1/instances` with optional `label`, `model`, `aux_model`
and `routes` overrides (all validated wholly before registration; any
failure registers nothing).

- The display `id` (`i-...`) and the ≥256-bit bearer token are separate;
  only the SHA-256 of the token is kept. The token appears exactly once, in
  the registration response.
- At creation the instance copies the current defaults (plus overrides)
  into immutable `RouteSnapshot` values: provider ID, base URL,
  `credential_env`, auth style, upstream model, and the revision it was set
  at. Later catalog edits or deletions never touch existing snapshots
  (status shows `catalog_present: false` once a snapshot diverges).
- Each instance has a monotonically increasing `revision`. `PUT .../routes`
  replaces exactly one request model's snapshot with a fresh resolution
  from the current catalog, guarded by compare-and-swap on
  `expected_revision`; the response is an honest `applied` acknowledgement.
- Leases: 30 s default, renewed via `POST .../renew` (the future launcher
  will renew every 10 s). Expiry and ending are evaluated against an
  injected clock; ended/expired instances reject new requests, renewals and
  route updates with explicit errors and no side effects.

## Request forwarding

For each `/v1/messages` (and `count_tokens`) request:

1. Authenticate the bearer against live instance tokens; ended/expired or
   unknown tokens fail closed before anything else.
2. Read the body and take its `model` string as-is; look it up in that
   instance's route table (exact match; unknown models fail with
   `unknown_model`, never falling back).
3. Capture the immutable route snapshot — from here on the request never
   re-reads defaults, the catalog or the instance's routes.
4. Resolve the credential from the gateway process environment
   (`credential_missing` fails before any upstream I/O).
5. Forward with HTTPX (async, `follow_redirects=False`, no retries):
   the upstream URL is `snapshot.base_url` + the original path and query
   (e.g. `/v1/messages?beta=true`), the body is the client's JSON with only
   the `model` field replaced by `snapshot.upstream_model`, and the headers
   carry the client's `anthropic-version`/`anthropic-beta`/`accept` plus the
   injected provider credential. The client's own
   `Authorization`/`x-api-key` are never forwarded.
6. Relay the upstream status and body verbatim; record sanitized metadata
   (instance, request model, actual provider/upstream model, revision,
   status, usage when present). Streaming responses are relayed chunk by
   chunk through a bounded incremental SSE parser: the wire bytes are
   forwarded unchanged (content encodings such as gzip are decoded by
   HTTPX and not re-encoded downstream), while usage is observed in
   passing — `message_start`/`message_delta` usage values are cumulative
   and replace each other, never summed. A stream that ends without
   `message_stop` is marked incomplete (retaining known usage); an SSE
   `error` event is marked failed; HTTP error statuses are marked failed;
   neither is ever recorded as success.
7. Release the upstream connection in every outcome: downstream
   cancellation (observed explicitly while awaiting upstream response
   headers and buffered bodies, and via the streaming response's
   disconnect listener mid-stream), upstream errors, truncation and
   normal completion. Cancellation before admission never touches the
   upstream; after admission the request keeps its full snapshot until it
   completes or is cancelled.

Request metadata (bounded to 1000 records, sanitized facts only) is exposed
via `GET /control/v1/requests` and `qing requests`; in-progress records
show the usage observed so far.

## Routing semantics

Route updates are per-request-boundary: once the gateway acknowledges a
route change (revision bump), new requests resolve against the new
snapshot, while requests already accepted keep the old snapshot until they
complete. Other instances are unaffected — demonstrated by the ASGI-level
dual-instance test with two upstreams.

## The launcher (`qing run`)

`qing run` registers a fresh instance from the current cwd (explicit
`--model`/`--aux-model`/`--route`/`--label` selections are validated wholly
before registration; `--resume` and other non-conflicting native arguments
pass through), then writes a per-run temporary 0600 JSON `--settings` file
carrying the gateway transport, the instance bearer and the selected
request models. The file outranks project settings env, and it additionally
neutralizes conflicting credentials and provider switches (API keys, OAuth
tokens, Bedrock/Vertex/Foundry/Mantle/AWS/GCP/gateway flags), so neither an
inherited environment nor a project's `settings.json` can bypass the
gateway. The bearer never appears in argv text, logs, status output,
records or the shared configuration; the file is removed on every exit
path, and a registration or spawn failure ends the instance and never
starts the client.

The child keeps the user's Claude config dir, setting sources, session
history, tools, hooks and unrelated environment. A lease keeper renews the
30 s lease every 10 s; one failed renewal is a warning, while explicit
loss (not-found, ended, control-identity change, expiry) or failures
outlasting the lease print reconnect guidance, end the child with bounded
SIGTERM/SIGKILL escalation and exit with a distinct code — the keeper never
re-registers after a gateway restart. SIGINT/SIGTERM are forwarded and
owned from before the child exists, so no setup window can orphan it.
Diagnostics distinguish *registered*, *child started* and *first
gateway-observed request*; stdout belongs to Claude (native `--output-format`
untouched), launcher messages go to stderr, and `--json` switches them to
one NDJSON event per line.

## Not implemented yet

- real-provider verification; configuration preview/backup/restore
