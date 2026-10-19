# dsh-ascend

给 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 增加独立的 **vLLM Ascend** agent 预设。选择预设后，模型的 `bash` 工具直接在准备好的 Docker 容器中执行。无需修改 dsh 源码。

- 容器预装 **lsp-cli 0.18.18**、Python 3.13、uv、git、rg、bash。
- 初始化时克隆 `vllm-project/vllm`、`vllm-project/vllm-ascend`，保留完整历史和 tags。
- 两份提示词来自 Graphub 的 `v5/prompts`，原文保存在 [prompts](prompts)。仅替换 WORKDIR、仓库路径/准备时 HEAD、输出上限占位符。
- GitHub 使用独立的私仓 [github-mcp](https://github.com/Delva0/github-mcp)，完整保留 REST / GraphQL / DSL，默认 REST。
- 仅在此预设中提供 `bash` 和 `mcp__ascend_github__github`。后者是 dsh 的命名空间；MCP server 仍仅暴露 `github`。

适配并固定 **dsh 0.2.1-rc.2**。本版是 agent 工具环境：dsh 的宿主机文件树、终端面板不映射容器。模型凭据和模型选择沿用 dsh 的设置。

## 安装

准备 Node.js 24+、[Docker](https://docs.docker.com/get-started/get-docker/)（Linux containers）、[uv](https://docs.astral.sh/uv/getting-started/installation/) 和 [GitHub CLI](https://cli.github.com/)。宿主用户须能直接执行 `docker info`。

```bash
npm install --global pnpm @deepseek-ai/dsh@0.2.1-rc.2
gh auth login
mkdir -p ~/projects
gh repo clone Delva0/dsh-ascend ~/projects/dsh-ascend
cd ~/projects/dsh-ascend
npm run setup
dsh web
```

首次准备需要下载 Python 依赖和两个完整 Git 仓库，耗时取决于网络。`npm run setup` 会：

1. 检查依赖和 dsh 版本；未安装 dsh 时安装固定版本。
2. 从有权限访问的 `Delva0/github-mcp` release 下载 v0.1.1 wheel，核对 SHA-256，安装至本仓 `build/github-mcp` 独立环境。
3. 构建 `dsh-ascend:0.1.0` 镜像，创建持久 Docker volume 和容器 `dsh-ascend-workspace`。
4. 克隆两个上游仓库；重复运行保留已有 checkout 和用户修改，不执行 reset/pull。
5. 将连接信息保存到 `${DSH_HOME:-~/.dsh}/ascend/workspace.json`，将插件包安装进 `web` profile。文件里只有容器名和可执行路径，没有 token。

打开 dsh 后，**新建会话，在第一条消息前选择 `vLLM Ascend`**。该版 dsh 会锁定已开始对话的预设。其他预设的工具环境保持原样。

可以先让 agent 执行 `lsp --version`、`pwd`，再开始代码研究。本预设使用原提示词的静态分析约束；镜像没有安装 CANN/NPU 推理运行环境。

## 插件页面加载

也可以先准备环境，再在 dsh 的 Plugins 页面安装本仓对应的 GitHub 包或本地包：

```bash
npm run setup -- --skip-plugin
npm pack --pack-destination build
```

安装本地 `build/delva-dsh-ascend-0.1.0.tgz` 可避免 GUI 的 Git 私仓凭据差异。等价命令：

```bash
dsh plugin --profile web add "$PWD/build/delva-dsh-ascend-0.1.0.tgz"
```

安装后启用插件 bundle 并选择预设。无需把 YAML 手工合并进已有配置。移动/删除本地 checkout 会影响 `build/github-mcp` 路径；移动后重新运行 setup 更新它。

## 配置

其他已有的 Web profile：

```bash
npm run setup -- --profile my-web
dsh my-web
```

独立的容器与卷：

```bash
npm run setup -- --container my-ascend --volume my-ascend-data
```

插件内配置支持 `container`、`workdir`、`maxOutputBytes`、`githubCommand`、`githubBackend`。`workdir` 默认 `/workspace`，必须包含两个已准备的仓库；默认内联输出上限为 100000 字节。可通过启动环境覆盖连接与 GitHub 后端：

```bash
GITHUB_MCP_BACKEND=graphql dsh web
GITHUB_MCP_BACKEND=dsl dsh web
DSH_ASCEND_CONTAINER=my-ascend dsh web
GITHUB_MCP_COMMAND=/absolute/path/to/github-mcp dsh web
```

插件显式向 GitHub MCP 转发 `GH_TOKEN`、`GITHUB_TOKEN`、`GITHUB_PERSONAL_ACCESS_TOKEN`；未设置时复用 `gh auth login`。这些凭据不传入容器。容器不挂载宿主 home、Docker socket 或项目目录。

## Bash 与任务生命周期

工具名是 `bash`，参数为 `command`、可选 `timeout`（毫秒）、`description`、`run_in_background`。工具描述与 [bash.md](prompts/bash.md) 完全一致。

- 每次调用从配置的 WORKDIR 开始，调用内的 `cd`、变量、管道、重定向按 bash 处理；文件持久保存。
- 前台默认等待 120000 ms，上限 600000 ms；超过等待预算自动转后台。显式后台立即返回任务 ID 和输出路径。
- 后台完成通过 dsh agent inbox 通知所属会话，可唤醒空闲会话。恢复相同会话后继续检查未通知任务。通知按至少一次投递处理：进程恰好在入队后、确认落盘前退出时可能重复。
- 取消前台调用停止其监督进程拥有的任务；已后台化的任务继续运行。关闭 dsh 保留容器和后台任务。
- stdout/stderr 合并写入容器日志；大输出返回预览、保存路径及截断状态。原始日志保留，监督器在约 5 GiB 时终止过量输出任务；结果副本最多 64 MiB。
- 容器重启后未完成任务标记 `interrupted`，退出码未知。运行中的插件固定完整容器 ID 与 Docker endpoint，不会在同名容器替换后静默执行到替代容器。

后台任务、通知状态位于 `/workspace/.dsh-ascend/tasks`。通过 bash 读取返回的日志路径；该预设与对应 v5 配置一样不额外注册 read_file/task 工具。管理员可在容器内调用协议入口停止一个已知任务：

```bash
docker exec -i dsh-ascend-workspace /opt/ascend/.venv/bin/python -m ascend_workspace.rpc <<'JSON'
{"action":"stop","session":"DSH_SESSION_ID","task_id":"32_HEX_TASK_ID"}
JSON
```

工作目录在持久卷中。停止/启动容器不会清空仓库；插件连接时会启动已停止的本插件容器。删除容器前应先停止后台工作，保留 volume 即保留代码。卸载插件使用 `dsh plugin --profile web remove @delva/dsh-ascend`，不会删除卷。

## 开发与验证

```bash
npm ci --ignore-scripts
uv sync --frozen
npm test
uv run --frozen ruff check .
uv run --frozen pytest -q
npm run test:dsh
```

最后一项要求已运行 setup、Docker 容器可用，以及 GitHub MCP 已配置。它使用真实 dsh 预设和工具注册表验证：工具隔离与切换、提示词、容器 bash、lsp 源码查询、GitHub API、后台通知及取消；不调用模型。

[UPSTREAM.json](UPSTREAM.json) 记录两份提示词、任务监督器及参数辅助代码的来源 commit 和 SHA-256。项目自包含，不依赖本机 Graphub/gh-puller checkout。Python 依赖使用 `uv.lock`，Node 开发依赖使用 `package-lock.json`。
