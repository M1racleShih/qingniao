# Contributing to Qingniao

Thanks for helping make model switching less distracting.

Qingniao is at the project-foundation stage. There is no application to build or test yet. Before starting a substantial implementation, open an issue describing the problem, proposed behavior, and how you would verify it. The first release is outlined in the [roadmap](docs/roadmap.md).

The first gateway and `qing` CLI will be implemented in Python. The minimum Python version, libraries, and installation workflow will be established with the first implementation.

## Useful contributions right now

- Describe a real workflow that breaks when you change models or providers.
- Report provider compatibility findings, including streaming and tool-call behavior.
- Improve the documentation or the original visual identity.
- Discuss an implementation slice with a concrete acceptance check.

Never include API keys, authorization headers, private conversation content, or account-identifying screenshots in public reports. A sanitized reproduction is enough.

## A straightforward pull request

1. Explain the user-visible problem and the behavior your change introduces.
2. Keep the change focused and use English for code identifiers, comments, primary documentation, and interface text. Translated documentation is welcome; keep the English and Simplified Chinese READMEs aligned when changing their shared content.
3. Describe what you verified and any remaining limitation. When the runtime is introduced, its build and test instructions must be published with it.
4. Link the relevant issue if one exists.

You can contribute using your usual editor and workflow. OpenSpec, private documents, and access to a separate repository are not prerequisites. Public implementation decisions and the information needed to reproduce a change belong in this repository or its issues and pull requests.

Contributions are made under the repository's [MIT license](LICENSE). Treat names, logos, and any third-party material with care; include provenance for new assets.
