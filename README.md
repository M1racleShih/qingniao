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

> **Early development — runnable core.** The gateway, the `qing` CLI, per-instance model routing and the Claude Code launcher are implemented and reproducible from this repository with `uv` (see [Try the development slice](#can-i-try-it)). There is no published package, registry release, or clean-install path yet. Real-provider evidence covers exactly the two tested configurations noted below (MiniMax M3 and glm-5.3-flash) — no other provider, endpoint or model has been verified; the first-release items below (configuration preview/backup/restore, published installs) are still being built.

## Your next model shouldn't need another config file

You want one model for everyday coding and another for a difficult bug. They happen to come from different providers. Now switching models also means switching endpoints, credentials, and settings. Later, you wonder which account handled the request and how much it used.

Qingniao is being built to take care of those connections. Point your coding agent at one local address, choose a model, and let an explicit route send the request to its configured provider.

Your agent stays where you work. Qingniao keeps the connections in order.

## The experience we're building

The first release will be operated through `qing` in the terminal. Clear layouts, readable status feedback, and useful errors are part of the [planned terminal experience](docs/terminal-experience.md). A web GUI may follow based on actual usage.

**Connect once.** Use `qing` to add your providers and models, check the connection, and connect Claude Code with a settings preview and a way to restore the original configuration.

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

**Yes, as a development slice straight from this repository** — no published release or official installation command exists yet, and `npm install -g qing` installs an unrelated package. The intended CLI command is `qing`; the distribution package name is still being selected.

```
uv sync --locked --extra dev
export MY_PROVIDER_TOKEN=...        # credential env your config references; never stored in it
uv run qing serve &                 # loopback gateway (control token in its state dir)
uv run qing config apply my-config.json
uv run qing run --label work        # registers an instance and launches Claude Code
uv run qing instances               # routes, revisions, per-instance state
uv run qing requests                # sanitized per-request metadata (usage unknown vs real 0)
```

Credentials must be exported in the gateway's shell **before** `qing serve`; exporting later in another shell does not affect a running gateway. `qing run` gives every launch (including `--resume`) a fresh instance identity, injects the transport through a temporary 0600 settings file (never in argv), preserves your Claude settings, history and tools, and reports honestly: registered vs. started vs. first gateway-observed request. See [docs/development.md](docs/development.md) and [docs/api.md](docs/api.md).

Real-provider validation (2026-09-08): two concurrent same-directory Claude Code sessions were run through `qing run` against exactly two tested configurations — MiniMax M3 at `https://api.minimaxi.com/anthropic` (model `MiniMax-M3`) and glm-5.3-flash at `https://open.bigmodel.cn/api/anthropic` (model `glm-5.3-flash`), both via `Authorization: Bearer` — covering distinct identities, an in-flight route switch with applied acknowledgement, cross-model tool-result continuation (evidenced by sanitized native tool facts and the reply), a previously inherited default staying unchanged on a live instance across route and defaults updates, defaults that affect only new instances, and explicit model precedence. These two tested configurations do not imply broad provider compatibility, and no real provider was reached through any published install (none exists yet). See `experiments/real-provider-verification/` for the reproducible, sanitized validation script.

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
