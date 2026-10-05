<p align="center">
  <img src="docs/assets/qingniao-mark.png" width="144" alt="青鸟：青绿色的传信使者" />
</p>

<h1 align="center">Qingniao · 青鸟</h1>

<p align="center"><a href="README.md">English</a> · <strong>简体中文</strong></p>

<p align="center"><strong>切换模型，让思路继续。</strong></p>
<p align="center">为 AI 编程工具提供本地模型网关，从终端开始。<br />从 Claude Code 开始，逐步连接更多 agent。</p>

<p align="center">
  <a href="#experience">使用体验</a> ·
  <a href="#try-it">开始试用</a> ·
  <a href="docs/roadmap.md">路线图（英文）</a> ·
  <a href="CONTRIBUTING.md">参与贡献（英文）</a>
</p>

> **早期开发——核心可运行，发布路径正在组装。** 网关、`qing` CLI、按实例的模型路由、Claude Code 启动器和 `qing config import-claude` 配置导入（含本地私密凭证存储）已经实现。发布**产物**可构建、可安装（2026-10-05 已在最小 Ubuntu 24.04 容器内验证干净 Linux 安装，Docker 28.4.0），但**尚无公开注册表发布**——分发包名定为 `qingniao-gateway`（PyPI 名 `qingniao` 被无关项目占用），发布等待显式授权。真实供应商证据：**MiniMax M3 与 glm-5.3-flash（个人 GLM 套餐）均已完整通过导入路径验证**（预检、离线导入、私密存储、网关首请求、重启持久化），且真实 Claude Code（2.1.274）经 `qing run` 在 glm-5.3-flash 上游上完成一轮最小会话。未对任何其他供应商、端点或模型做验证。

## 换个模型，不该还要换一份配置

日常写代码，你习惯用一个模型；遇到棘手的 bug，又想换另一个。它们恰好来自不同供应商，于是，换模型变成了换接口地址、换凭证、换配置。用了一阵之后，你还想知道：刚才的请求走了哪个账号，消耗了多少？

青鸟希望把这些连接工作接过来。让编程工具始终连接同一个本地地址，你选择模型，青鸟按照明确的路由配置把请求发给对应供应商。

你继续在熟悉的 agent 里工作，青鸟把连接打理好。

<a id="experience"></a>

## 我们想做好的三件事

首版通过终端中的 `qing` 完成操作，清晰的布局、易读的状态反馈和有用的错误提示都是[终端体验规划（英文）](docs/terminal-experience.md)的一部分。Web GUI 根据实际使用情况再决定。

**接入一次，准备就绪。** 用 `qing config import-claude` 导入现有 Claude 配置——先出脱敏预览，再显式应用：凭证存入本地私密存储（0600 明文文件，不是加密保险箱），原文件保持不变。也可以用 `qing provider`、`qing credential` 和 `qing model` 逐条管理目录——增、查、列、改、删供应商、凭证来源与模型，无需重写整份配置（整份高级编辑仍走 `qing config apply`）。这些命令非交互且对 agent 友好：稳定的 `--json` 输出、机器错误码、`--dry-run` 预览、引用保护式删除，凭证值只能经标准输入或文件传入，绝不经过命令行。

**切换模型，不再重复配置。** 多个 Claude Code 实例共享供应商与凭证配置，各自独立选择模型。通过 `qing` 修改指定实例的路由，不改变其他实例。网关确认更新后，该实例的新请求使用新路由，已经进行中的请求保持原有去向，无需重启。修改默认值仅影响新实例。

**每次请求，去向和用量都清楚。** 在终端中一起查看请求时选择的模型、实际使用的上游模型、供应商、耗时和返回的 token 用量。没有返回的用量保留为未知，估算费用明确标注为估算。

例如，我们希望让这样的路由过程一眼可见：

```text
Claude Code              青鸟                     你的供应商
  选择模型 A  ──────►    精确匹配模型路由  ───►    供应商 A / 模型 A
  选择模型 B  ──────►    精确匹配模型路由  ───►    供应商 B / 模型 B
                              │
                           请求历史
```

这是体验示意，还不是可直接使用的配置示例。首版会接入经过验证的 Anthropic 兼容上游，逐项确认实际能力，不假设不同模型和协议可以直接互换。

## 已经在用 pi 或 Kimi Code？

继续使用它们自己的模型选择器就好。原生的多供应商配置，可能已经足够满足你的需求。

当你希望多个 agent 共用凭证、路由和请求记录时，再选择接入青鸟。这是后续方向；pi 和 Kimi Code 暂不在首版兼容范围内。

<a id="try-it"></a>

## 现在能试用吗？

有两条路径。**安装路径**从本地构建的发布产物（`.whl`）开始——见下方
[从发布产物安装](#install-from-a-release-artifact)；**开发路径**直接用 `uv`
在本仓库运行（见 [docs/development.md](docs/development.md)）。青鸟计划使用的
CLI 命令是 `qing`；公开分发包名仍未选定（PyPI 上的 `qingniao` 是无关项目）——
在维护者确定名字之前，只安装本地构建的产物，不执行任何公开发布动作。

**开发路径（在本仓库内）：**

```
uv sync --locked --extra dev
uv run qing config import-claude            # 预览导入 ~/.claude/settings.json（零写入）
uv run qing config import-claude --apply    # 保存连接与私密凭证，原文件不变
uv run qing serve &                 # 本地回环网关（控制令牌在状态目录中）
uv run qing run --label work        # 注册实例并启动 Claude Code
uv run qing instances               # 路由、revision 与各实例状态
uv run qing requests                # 脱敏的请求元数据（用量未知 vs 真实 0）
```

也可以跳过导入，直接引用已 export 的环境变量（在 `qing serve` 之前 `export MY_PROVIDER_TOKEN=...`；凭证值不会写进配置文件），或逐条管理目录——非交互、支持 `--json`、秘密安全：

```
uv run qing provider add my-provider --base-url https://api.example.com \
  --auth bearer --credential-env MY_PROVIDER_TOKEN
uv run qing model add my-model --provider my-provider --upstream-model vendor/model
uv run qing provider list && qing model list
```

`qing credential add --env NAME` 注册命名环境变量凭证，供供应商按 id 引用；`qing credential add --from-stdin`（或 `--from-file`）存储私密凭证，其值绝不进入 argv、输出、JSON、错误或日志。每个变更都支持 `--dry-run` 预览；删除操作拒绝破坏引用。`qing run --preview` 可以在不注册任何实例的情况下预览接入影响。

凭证必须在启动 `qing serve` 的 shell 中**事先** export；之后在其他 shell 里 export 不会影响正在运行的网关。`qing run` 每次启动（包括 `--resume`）都生成新的实例身份，通过临时 0600 设置文件注入传输配置（绝不进入 argv），保留你的 Claude 配置、会话历史与工具，并如实区分：已注册 / 子进程已启动 / 网关观察到首个请求。详见 [docs/development.md](docs/development.md) 与 [docs/api.md](docs/api.md)。

## 从发布产物安装

先在本地构建 wheel（见 [docs/development.md](docs/development.md)，`uv build`），再用你习惯的 Python 工具安装。推荐路径使用 [uv](https://docs.astral.sh/uv/)——它管理隔离环境并把 `qing` 放进 `PATH`：

```
uv tool install qingniao_gateway-0.1.0-py3-none-any.whl
qing --version        # 打印发布版本（0.1.0）
```

`python3 -m venv venv && venv/bin/pip install qingniao_gateway-0.1.0-py3-none-any.whl` 也可以（`qing` 会进入 `venv/bin/`）。运行时需要 Python 3.12+ 以及已声明的依赖（typer、rich、httpx、starlette、uvicorn），安装后无需联网。

**卸载与你的数据。** `uv tool uninstall qingniao-gateway`（或 `pip uninstall qingniao-gateway`）删除 CLI 与包文件，但**默认保留用户数据**——状态目录（`$XDG_STATE_HOME/qingniao`，否则 `~/.local/state/qingniao`）中的共享配置、私密凭证与脱敏请求记录不会被主动删除。如需彻底清除，请自行删除状态目录；不会隐式删除任何内容。该策略由干净安装验收强制执行。

## 验证声明（2026-10-05）

- **真实导入路径验证**（`experiments/real-provider-verification/run_import_validation.py` 与 `run_wave4_validation.py`）：每个波次的真实请求预算上限 10（每条请求均先计数后发起；第 4 波前累计账本为 11）。**MiniMax M3**（`MiniMax-M3` @ `https://api.minimaxi.com/anthropic`，`Authorization: Bearer`）与 **glm-5.3-flash**（`https://open.bigmodel.cn/api/anthropic`，个人 GLM 套餐）均通过完整导入路径：预检 200 且模型串精确回显、离线 `config import-claude` 预览零写入、`--apply` 凭证入私密存储（配置中无密钥）、网关首请求上游模型串正确且用量如实记录、重启后私密凭证仍可用。随后真实 Claude Code 2.1.274 经 `qing run` 在 glm-5.3-flash 上游上完成一轮最小会话（成功记录路由到预期供应商）——第 4 波共发起 4 条真实请求（累计 15）；MiMo 兜底未被触发（GLM 环节已通过）。
- **干净 Linux 安装**（`experiments/clean-install-verification/`）：在最小 Ubuntu 24.04.3 容器（Docker 28.4.0，无源码检出）中，wheel 经 `uv tool install` 安装，`qing --version` 输出 `0.1.0`，网关完成一个本地合成首请求（上游观察到正确模型串），`uv tool uninstall qingniao-gateway` 后 CLI 消失而状态目录保留。
- 更早的真实供应商证据（2026-09-08，`qing run` 覆盖两个配置）记录在 [docs/development.md](docs/development.md)。

这些已测配置不代表广泛供应商兼容性；没有任何真实供应商经由公开发布的安装（尚不存在）被访问。

你可以继续关注首个正式版本，也可以一起参与开发。[路线图（英文）](docs/roadmap.md)列出了第一个里程碑。如果切换供应商经常打断你的工作，欢迎提交 Issue，告诉我们你用的 agent、供应商，以及最让你觉得麻烦的一步。请不要附上凭证或私人提示词。

## 让工具配合你的工作方式

实现会围绕这些原则展开：

- **去向明确。** 未配置的模型应该返回有帮助的错误。换成另一种模型应该是一个你能看见的选择。
- **用量清楚。** 实际返回的 token 用量、估算费用和供应商订阅额度分别说明。
- **本地管理。** 网关和请求历史保存在本地，模型请求仍会发往你配置的供应商，并不意味着离线推理。
- **兼容性逐步验证。** 流式输出、工具调用和模型能力，与能否成功回复一段文字同样重要。

## 为什么叫“青鸟”？

在中国神话与文学中，青鸟是传信的使者。我们把这个意象带进开发工具：连接你工作的地方，与自己选择的模型。

Logo 是为本项目创作的 AI 辅助品牌概念稿。青鸟是独立项目，与文中提及的 agent 或模型供应商没有隶属关系。

## 一起把青鸟做出来

有用的问题反馈、供应商兼容性记录、清晰的文档和认真打磨的代码，都能帮助青鸟往前走。可以从[贡献指南（英文）](CONTRIBUTING.md)开始，用你习惯的 Issue 和 PR 流程参与，无需使用特定的规格管理工具。

我们参考了 [flexible-gateway](https://github.com/Agony5757/flexible-gateway) 对模型路由和可恢复的 Claude Code 配置管理的设计。青鸟独立实现，当前项目基础中没有复制该项目的代码。

## 许可证

[MIT](LICENSE)。
