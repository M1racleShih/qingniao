# Claude Code instance-identity experiment

Status: experiment and launch-precedence probe complete (2026-09-08). Runtime implementation has not started; this document records evidence and design guidance only.

## Questions

1. When several Claude Code processes run concurrently against one Qingniao gateway and share the same project cwd, can the gateway attribute every upstream request — streamed main-model requests and real auxiliary-model requests alike — to the instance that sent it? Which child-only, per-process identity carrier makes that work?
2. When a Qingniao launcher must preserve the user's normal Claude settings, hooks, and tools, which injection mechanism wins for the per-instance transport (base URL and token): the child process environment, the project's `.claude/settings.json` `env`, or an explicit `--settings` value?

## Reproduce

Requirements: Linux, Python 3.12 or newer (the requirement for this planned slice; the recorded runs used 3.12.12), a local `claude` CLI on `PATH`. No credentials, no network access, no real provider.

```
python3 experiments/claude-instance-identity/run_experiment.py                     # dual-instance carrier run
python3 experiments/claude-instance-identity/run_experiment.py --launch-precedence # settings-precedence probe
```

Environment used for the recorded results:

| Component | Version |
| --- | --- |
| Claude Code CLI | 2.1.251 (`claude --version`) |
| Python | 3.12.12 |
| Platform | Linux 6.14 |

Both commands exit 0 only if every assertion passes; each was run successfully multiple times on 2026-09-08.

## Design

### Dual-instance carrier run

The script starts a loopback-only scripted Anthropic-compatible fixture (127.0.0.1, ephemeral port), then launches two real `claude -p` children simultaneously, in the same project cwd, with distinct `ANTHROPIC_AUTH_TOKEN` values. Per instance it proves the full chain:

1. Main-model request arrives streamed (`stream: true`, SSE response).
2. The fixture answers with a real `tool_use` for the built-in `Agent` tool (`subagent_type: general-purpose`, `run_in_background: false`).
3. Claude Code launches the subagent, producing a genuine auxiliary-model request that carries `CLAUDE_CODE_SUBAGENT_MODEL` — a distinct model string — over the same base URL with the same identity carrier.
4. The subagent's final text (`AUX-DONE`) returns as the `tool_result`.
5. The main-model continuation carries that `tool_result`; only then does the fixture end the conversation with `CHAIN-OK`, which the child prints as its final result.

A fixture-side barrier holds the first main-model response of each identity until the other identity's first request has arrived (2 s cap), so the two instances provably overlap and interleave.

### Launch-precedence probe

One real child per scenario, normal setting sources enabled (no `--setting-sources` restriction), in a synthetic project whose `.claude/settings.json` `env` carries a deliberately wrong transport: a decoy loopback base URL and decoy token, both served by a second working fixture so misrouting is positively observable.

- `process-env`: intended fixture URL/token only in the child process environment.
- `settings-flag`: intended fixture URL/token only in an explicit `--settings` JSON `env` block; no transport keys in the process environment.

Which listener receives the traffic, and which token arrives, is the precedence evidence.

### Experiment-only isolation (not runtime behavior)

To keep fixture evidence trustworthy and reproducible, experiment children get: an explicit env allowlist (no inherited `ANTHROPIC_*`/`CLAUDE_*`), a fresh per-instance `CLAUDE_CONFIG_DIR` and `TMPDIR` under `/tmp`, `HTTP(S)_PROXY` pointed at a dead loopback port with `NO_PROXY` for the fixture, telemetry/error-reporting/bug-command/auto-updater disables, `--no-session-persistence`, `--setting-sources ""`, and an empty `--mcp-config`. These are fixture-run measures only. A normal Qingniao launcher must preserve the user's existing Claude sessions, settings, tools, hooks, and non-Qingniao environment; only the per-instance transport is injected, via the mechanism the precedence probe established.

Reports and fixture logs contain sanitized facts only: header names, token hashes, model names, role sequences, byte counts — no prompt/response bodies, no raw child stdout/stderr, no session data. The dual-instance run additionally scans its persisted output for both token plaintexts and asserts none appear. The precedence probe does write synthetic setup files under its `/tmp` workdir by design — including the project `.claude/settings.json` that carries the synthetic decoy token; every token in this experiment is a synthetic fixture value, and no real credential is read, stored, or transmitted. The script also snapshots `~/.claude.json` and `~/.claude` with `lstat` before/after and asserts zero changes; nothing under `/tmp` is committed.

## Recorded results (2026-09-08)

Dual-instance run:

```
instance a: rc=0 is_error=False turns=2 result='CHAIN-OK' requests=3 main_sse=2 aux=1 stderr_sources=['agent:builtin:general-purpose', 'sdk']
instance b: rc=0 is_error=False turns=2 result='CHAIN-OK' requests=3 main_sse=2 aux=1 stderr_sources=['agent:builtin:general-purpose', 'sdk']

  PASS  children_completed
  PASS  children_not_is_error
  PASS  chain_marker_observed
  PASS  streaming_main_requests
  PASS  aux_model_requests
  PASS  aux_tool_result_not_error
  PASS  identity_unique_and_stable
  PASS  instances_distinct
  PASS  first_two_main_requests_distinct_identity
  PASS  concurrent_overlap
  PASS  no_unattributed_v1_messages
  PASS  no_token_plaintext_in_artifacts
  PASS  records_schema_contains_no_body_content
  PASS  home_claude_state_unchanged
```

Launch-precedence probe:

```
scenario process-env (expected winner: decoy): rc=0 is_error=False result='CHAIN-OK'
  winning fixture: v1_messages=3 main_sse=2 aux=1 aux_models=['fixture-haiku-model'] bearers=['Bearer <…>'] identity=['child']
  opposing fixture: v1_messages=0            -> all 7 assertions PASS
scenario settings-flag (expected winner: real): rc=0 is_error=False result='CHAIN-OK'
  winning fixture: v1_messages=3 main_sse=2 aux=1 aux_models=['fixture-haiku-model'] bearers=['Bearer <…>'] identity=['child']
  opposing fixture: v1_messages=0            -> all 7 assertions PASS
real HOME Claude state changes: 0
```

## Observed protocol facts (Claude Code 2.1.251)

- Endpoint: every conversation request is `POST /v1/messages?beta=true`. `count_tokens` and `/v1/models` were not hit in these short sessions.
- Auth shape: `ANTHROPIC_AUTH_TOKEN` produces `Authorization: Bearer <token>` and no `x-api-key` header, on main and auxiliary requests alike. (A separate single-instance scratch probe confirmed `ANTHROPIC_API_KEY` produces `x-api-key` and no `Authorization`.)
- `--bare` (per `claude --help`): "Anthropic auth is strictly ANTHROPIC_API_KEY or apiKeyHelper via --settings" — the Bearer carrier is incompatible with `--bare`. Not exercised at runtime here.
- Fixed headers: `anthropic-version: 2023-06-01`; `user-agent: claude-cli/2.1.251 (external, sdk-cli)`; a stable `anthropic-beta` feature set (two orderings observed, one on auxiliary requests). Identical across instances; useless as identity.
- Model strings pass through verbatim for the strings used here (`--model` on the CLI, and `CLAUDE_CODE_SUBAGENT_MODEL`), with a stderr `[claude-code:unrecognized_model]` warning whose `query_source` distinguishes `sdk` (main) from `agent:builtin:general-purpose` (subagent). This was not tested for every model-selection path (aliases, fallbacks, cloud sessions).
- Auxiliary requests (subagents) reuse the same base URL, auth header, and user agent; they have their own system prompt and a `["user"]`-only message list.
- `metadata.user_id` is sent on every request: stable within an instance and distinct across instances under the experiment's isolated config dirs. How it is derived, and whether it can be controlled per launch, was not established — treat it as a correlation signal, not a carrier.
- Requests contain `cache_control` blocks and mid-conversation `system`-role messages.
- Tool surface: `--tools Agent` advertises exactly 1 tool (observed `n_tools=1`, and the subagent inherits the restriction). `--allowedTools` alone does not restrict the advertised set (25 tools observed in an early probe).
- The model-visible subagent tool is named `Agent`; valid `subagent_type` values are surfaced via system-reminder (observed: `claude`, `Explore`, `general-purpose`, `Plan`, `statusline-setup`). Subagents run in the background by default; `run_in_background: false` is required for a synchronous round trip.
- Isolation flags accepted in `-p` mode: `--no-session-persistence`, `--setting-sources ""`, `--strict-mcp-config` with an empty `--mcp-config`, `--tools`, `--allowedTools`, `--settings` (JSON string). The workspace-trust dialog is skipped in `-p` mode.

### Settings `env` precedence (normal sources enabled)

With user, project, and local setting sources all active, for the `env` keys each source defines:

1. Explicit `--settings` JSON `env` wins (its base URL and token reached the intended fixture; the project decoy received nothing).
2. Project `.claude/settings.json` `env` beats the child process environment: with the intended transport in the process env only, all traffic followed the project's decoy base URL using the decoy token. The override is per key and does not mix (the process-env token never appeared at the decoy).
3. The process-environment `CLAUDE_CODE_SUBAGENT_MODEL` value was used in both scenarios, where the synthetic project settings did not define that key. This probe did not isolate whether user-level settings `env` competes for that or any other key.

Security-relevant consequence: a project's `.claude/settings.json` can silently redirect Claude Code traffic and select the credential used for it. A Qingniao launcher that leaves normal setting sources enabled must therefore inject the per-instance transport at a precedence level above project settings — the explicit `--settings` `env` block does that.

## Carrier evaluation

| Candidate carrier | Evidence |
| --- | --- |
| `ANTHROPIC_AUTH_TOKEN` (Bearer) | Proven in the dual-instance run: same cwd, same fixture, streaming + auxiliary, identity stable and distinct. |
| `ANTHROPIC_API_KEY` (x-api-key) | Single-instance scratch probe: header shape observed (`x-api-key`, no `Authorization`), full chain passed. Not dual-proven in the final harness. |
| Base-URL host/port (one port per instance) | Implicitly supported: the base URL governed every request including auxiliary ones. Trivially separable at the gateway, but not exercised as a dual-instance carrier here. |
| Base-URL path prefix | Not established. A scratch attempt failed for an unrelated invocation bug (relative path), so there is no valid observation; treat as unverified. |
| `ANTHROPIC_CUSTOM_HEADERS` | Not observed to work: the custom header did not appear in fixture records when the env var was set (scratch probe). Treat as unavailable until proven. |
| `metadata.user_id` | Observed stable-per-instance/distinct-across-instances under isolated config dirs; derivation and controllability not established; not a carrier. |

## Design guidance for the runtime

1. Carrier: the gateway issues an opaque per-instance token; the launcher injects it with the loopback base URL as the instance's transport. The gateway attributes each request by token hash and never parses bodies for identity. Every observed request kind — main streamed and auxiliary — carried the token, which is the property gateway-side route switching relies on.
2. Transport injection: write the per-instance transport to a temporary private settings file (created with 0600 permissions, deleted when the instance exits) and pass it with `--settings <private-settings-file>` while leaving normal setting sources enabled, so the user's settings, hooks, and tools keep working and a project's `.claude/settings.json` cannot override the transport. Include `CLAUDE_CODE_SUBAGENT_MODEL` in the same file if the gateway must control the subagent model deterministically. The probe validated only the JSON-string form of `--settings`; the file form must be exercised in runtime tests before reliance.
3. Do not use `--bare` while the Bearer carrier is in play; its auth restriction (API key / apiKeyHelper only) conflicts with `ANTHROPIC_AUTH_TOKEN`.
4. Routing semantics: the token (instance identity) is fixed for the life of the process, but routing is a gateway-side decision made per request. The gateway keeps a route table keyed by instance token and updates an existing instance's route atomically: once the gateway acknowledges an update, that instance's new requests use the new route with no Claude restart; requests already accepted complete against their original route snapshot (upstream address, model, credential reference); other instances are unaffected. The experiment proves the enabling property — per-request attribution on every request kind — but did not itself exercise a live route switch; that remains for runtime acceptance tests.
5. Launch recipe (headless): `claude -p --model <gateway main model> --settings <private-settings-file> …` with normal setting sources left enabled, where the temporary 0600 file contains `{"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:<port>", "ANTHROPIC_AUTH_TOKEN": "<per-instance token>", "CLAUDE_CODE_SUBAGENT_MODEL": "<gateway aux model>"}}` and is deleted at instance exit. Embedding the JSON string directly in argv was probe-only and would expose a live bearer in the process list. Readiness can be defined as "first attributed request observed".
6. Request metadata: record the instance ID — a non-secret gateway label resolved from the token — plus model, endpoint, and sizes per request; the secret token itself stays internal to auth matching. Treat any `/v1/messages` request whose token does not resolve as a gateway error.

## Risks and unresolved questions

- Version drift: tool naming (`Agent`), background-by-default subagents, the beta header set, `?beta=true`, and the settings-`env` precedence order are 2.1.251 observations. The runtime should pin or assert the Claude Code version before applying these facts.
- Live route switching, gateway acknowledgement flow, and in-flight snapshot retention are designed but untested; they need runtime acceptance tests with a controllable upstream.
- Path-prefix routing and `ANTHROPIC_CUSTOM_HEADERS` remain unverified; if either is wanted, it needs a dedicated follow-up experiment.
- Egress isolation in the experiment relies on documented disable flags plus a dead proxy; no packet-level verification was done. A normal launcher preserving the user environment must not reuse these fixture-only measures and cannot rely on them as leak protection.
- Auxiliary coverage is limited to `Agent`-tool subagents. Other documented auxiliary triggers (topic detection, `count_tokens` on large contexts) did not fire in these short sessions and remain unobserved.
- Whether user-level settings `env` participates in contention (and how the user settings source resolves under a non-default `CLAUDE_CONFIG_DIR`) was not isolated; only project-settings and explicit `--settings` contention was tested.
- Single machine, loopback fixture only: no statement about real provider compatibility is implied.
