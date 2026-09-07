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

> **Early development.** This repository currently contains the project foundation and brand assets. The gateway and `qing` CLI are not implemented yet. The experience below describes the first release we are building.

## Your next model shouldn't need another config file

You want one model for everyday coding and another for a difficult bug. They happen to come from different providers. Now switching models also means switching endpoints, credentials, and settings. Later, you wonder which account handled the request and how much it used.

Qingniao is being built to take care of those connections. Point your coding agent at one local address, choose a model, and let an explicit route send the request to its configured provider.

Your agent stays where you work. Qingniao keeps the connections in order.

## The experience we're building

The first release will be operated through `qing` in the terminal. Clear layouts, readable status feedback, and useful errors are part of the [planned terminal experience](docs/terminal-experience.md). A web GUI may follow based on actual usage.

**Connect once.** Use `qing` to add your providers and models, check the connection, and connect Claude Code with a settings preview and a way to restore the original configuration.

**Switch without the setup ritual.** Choose a configured model in your agent. Qingniao routes it to the right endpoint and credential. Change a route with `qing` and see which requests it will affect. New requests use the updated route without a restart; requests already in progress keep their original destination.

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

**Not yet — there is no runnable release or official installation command.** In particular, `npm install -g qing` installs an unrelated package, not Qingniao. Our intended CLI command is `qing`; the distribution package name is still being selected.

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
