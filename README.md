# dsh-ascend

给 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 增加独立的 **vLLM Ascend** agent 预设。选择预设后，模型的 `bash` 工具直接在准备好的 Docker 容器中执行。无需修改 dsh 源码。

- 容器预装 **lsp-cli 0.18.18**、Python 3.13、uv、git、rg、bash。
- 初始化时克隆 `vllm-project/vllm`、`vllm-project/vllm-ascend`，保留完整历史和 tags。
- 两份提示词来自 Graphub 的 `v5/prompts`，原文保存在 [prompts](prompts)。仅替换 WORKDIR、仓库路径、输出上限占位符。
- GitHub 直接注册为 dsh 原生工具，完整保留 REST / GraphQL / DSL，默认 REST。Python 核心随插件打包。
- 原版 `web_search` / `web_fetch` 一起打包，保留 Brave / DuckDuckGo 搜索，以及网页、PDF、压缩包、图片、引用续读及批量错误隔离。
- 仅在此预设中提供四个原生工具：`bash`、`github`、`web_search` 和 `web_fetch`。

适配并固定 **dsh 0.2.1-rc.2**。本版是 agent 工具环境：dsh 的宿主机文件树、终端面板不映射容器。模型凭据和模型选择沿用 dsh 的设置。

## 安装

准备 Node.js 24+、[Docker](https://docs.docker.com/get-started/get-docker/)（Linux containers）、[uv](https://docs.astral.sh/uv/getting-started/installation/) 和 [GitHub CLI](https://cli.github.com/)。宿主用户须能直接执行 `docker info`。之后安装 dsh 和插件即可，**无需手动克隆 vllm/vllm-ascend，也无需先运行 setup**。

```bash
npm install --global pnpm @deepseek-ai/dsh@0.2.1-rc.2
gh auth login
mkdir -p ~/.cache/dsh-ascend
gh release download v0.4.0 --repo Delva0/dsh-ascend \
  --pattern delva-dsh-ascend-0.4.0.tgz --dir ~/.cache/dsh-ascend --clobber
dsh plugin --profile web add "$HOME/.cache/dsh-ascend/delva-dsh-ascend-0.4.0.tgz"
dsh web
```

也可在 dsh 的 Plugins 页面安装下载的 `.tgz`，然后启用这个 bundle。插件包自带容器构建文件、GitHub / Web Python 核心及锁定的依赖，不依赖其他私仓或开发者 checkout。默认搜索使用 Brave，首次搜索前按下文配置 Brave key；也可选择无需 key 的 DuckDuckGo。

加载插件后自动开始首次准备，dsh 界面正常打开：

1. 检查 Docker；用 uv 按 `uv.lock` 安装 GitHub 和 Web 工具共用的 Python 运行环境到 `~/.dsh/ascend/runtimes/github-<源码摘要>`。安装完成才标记就绪，升级源码会自动使用对应的新环境。
2. 构建 `dsh-ascend:0.1.0` 镜像，创建持久 Docker volume 和容器 `dsh-ascend-workspace`，安装 lsp-cli 及配套工具。
3. 自动克隆两个上游仓库的完整历史和 tags。宿主有 Git 时复用宿主网络/代理，再将完整 checkout 导入容器卷并清理临时目录；否则由容器克隆。容器克隆中断时最多尝试三次。已有 checkout 和用户修改会保留，不执行 reset/pull。
4. 注册四个 dsh 原生工具。环境就绪后才允许该预设开始模型请求或执行工具；GitHub、web_search、web_fetch 各自在所属会话首次调用时启动独立 worker。

首次需要下载依赖和两个完整 Git 仓库，耗时取决于网络。初始化日志位于 `${DSH_HOME:-~/.dsh}/ascend/setup-*.log`。关闭 dsh 不会打断已启动的独立准备进程；重新打开时会复用正在准备或已经完成的环境。若安装失败，日志和首条请求的错误会说明原因；修复依赖/网络后重启 dsh 即可重试。

打开 dsh 后，**新建会话，在第一条消息前选择 `vLLM Ascend`**。如果仍在初始化，首次请求会等待就绪。该版 dsh 会锁定已开始对话的预设。其他预设的工具环境保持原样。

可以先让 agent 执行 `lsp --version`、`pwd`，再开始代码研究。本预设使用原提示词的静态分析约束；镜像没有安装 CANN/NPU 推理运行环境。

## 从源码安装或提前准备

开发时可从源码安装；`setup` 只是提前准备环境和安装本地包的可选入口：

```bash
gh repo clone Delva0/dsh-ascend ~/projects/dsh-ascend
cd ~/projects/dsh-ascend
npm run setup
```

只构建插件包、让插件自己准备环境：

```bash
mkdir -p build
npm pack --pack-destination build
dsh plugin --profile web add "$PWD/build/delva-dsh-ascend-0.4.0.tgz"
```

只提前准备、不安装插件可用 `npm run setup -- --skip-plugin`。源码 setup 会将连接信息写入 `${DSH_HOME:-~/.dsh}/ascend/workspace.json`；仅包含容器、镜像和 volume，没有 token 或固定的自动生成运行环境路径。运行时数据放在 dsh 数据目录和 Docker volume，安装后无需保留源码 checkout。

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

插件内配置支持 `container`、`volume`、`image`、`workdir`、`maxOutputBytes`、`githubPython`、`githubBackend`，以及下文的三项搜索配置。自动准备使用 `/workspace`；自定义 `workdir` 需要自行提供含两个仓库的镜像/容器。默认内联输出上限为 100000 字节。可通过启动环境覆盖连接与 GitHub 后端：

```bash
DSH_ASCEND_GITHUB_BACKEND=graphql dsh web
DSH_ASCEND_GITHUB_BACKEND=dsl dsh web
DSH_ASCEND_CONTAINER=my-ascend dsh web
DSH_ASCEND_CONTAINER=my-ascend DSH_ASCEND_VOLUME=my-ascend-data dsh web
DSH_ASCEND_GITHUB_PYTHON=/absolute/path/to/venv/bin/python dsh web
```

插件通过 dsh 的 subprocess provider 显式向 GitHub worker 转发 `GH_TOKEN`、`GITHUB_TOKEN`、`GITHUB_PERSONAL_ACCESS_TOKEN`；未设置时复用 `gh auth login`。这些凭据不传入 Web workers 或容器。容器不挂载宿主 home、Docker socket 或项目目录。

`githubPython` / `DSH_ASCEND_GITHUB_PYTHON` 是可选的开发覆盖项，指定的 Python 必须已安装本项目及 `github` extra；普通安装会自动准备。后端配置使用 `githubBackend` 或 `DSH_ASCEND_GITHUB_BACKEND`，不再读取旧版本的 GitHub 配置环境变量。

## 原生 GitHub 工具

通过 `ctx.tools.register` 注册 `github`，参数、描述和三个后端的处理逻辑沿用原 GitHub 工具。返回值直接是后端的 JSON 数据，进入 dsh 原生工具记录和展示，不额外包装 `content` / `structuredContent`。

- dsh 管理工具调度、调用记录、超时策略和 UI 展示。GitHub 调用显示为 fetch 类工具卡片。
- JS 适配器通过 `ctx.subprocess` 启动宿主机 Python worker，使用插件内部的 JSON-lines 请求/响应协议。
- 每个会话有独立 worker、HTTP client、响应缓存和临时证据文件；同一会话的调用可并发，保存的 `result_id` 可在 worker 存续期间继续查询。会话释放、预设卸载或 dsh 退出会关闭 worker 并清理临时文件。
- 取消调用会通知 Python 停止对应任务，等待停止确认后返回；其他并发调用继续工作。若 worker 无法响应取消，则通过 dsh 终止并回收该进程，使所有受影响调用明确失败，后续调用可重新启动 worker。
- 批量请求中的无效成员只影响自身；全部失败或顶层输入错误才将整次工具调用标为失败。

## 原生 web_search 工具与 Brave 配置

默认沿用 v5：搜索后端 `brave`，搜索并发 `1`，请求间隔 `2` 秒。工具描述和参数原样注册，保留查询批次、地区、时间范围、页码、结果数、限流冷却和原始错误；所有搜索调用共享所属会话的并发与节流状态。

Brave key 从**启动 dsh 的进程环境**读取，优先 `BRAVE_SEARCH_API_KEY`，兼容 `BRAVE_API_KEY`，取第一个非空值。插件不自动读取旧 Graphub/主仓的 `.env`，也不把 key 写入插件配置或 `workspace.json`。在同一个终端配置后启动：

```bash
read -rsp 'Brave Search API key: ' BRAVE_SEARCH_API_KEY; printf '\n'
export BRAVE_SEARCH_API_KEY
dsh web
```

更换 key 后重启 dsh。凭据只转发给 web_search worker，并由原实现仅在 Brave API 请求上设置认证头，不传给 GitHub、web_fetch 或容器。选择 Brave 但未配置 key 时，首次搜索明确报错，其他工具照常可用。

| 插件配置项 | 启动环境变量 | 默认值 |
| --- | --- | --- |
| `webSearchBackend` | `DSH_ASCEND_WEB_SEARCH_BACKEND` | `brave` |
| `webSearchConcurrency` | `DSH_ASCEND_WEB_SEARCH_CONCURRENCY` | `1` |
| `webSearchInterval` | `DSH_ASCEND_WEB_SEARCH_INTERVAL` | `2`（秒） |

显式插件配置优先于环境变量。`webSearchBackend` 支持 `brave`、`duckduckgo`、`auto`；`auto` 在有 key 时选择 Brave，没有 key 时选择 DuckDuckGo。并发必须是正整数，间隔必须是有限的非负数。例如：

```bash
DSH_ASCEND_WEB_SEARCH_BACKEND=duckduckgo dsh web
DSH_ASCEND_WEB_SEARCH_BACKEND=auto dsh web
DSH_ASCEND_WEB_SEARCH_CONCURRENCY=1 DSH_ASCEND_WEB_SEARCH_INTERVAL=2 dsh web
```

## 原生 web_fetch 工具

直接注册原版 `web_fetch` 的名称、描述和参数。完整的 `tool_web.py` 随包提供，原生桥接分别暴露其中的 `web_search` 和 `web_fetch`。

- 保留 HTTP(S) 重定向、HTML 转 Markdown、文本/JSON、PDF 文本提取、ZIP/TAR、gzip/bzip2/xz、图片及其他二进制文件的原始处理行为和限制。
- `url` 每次重新下载；`ref`、`member`、`start_line`、`max_lines`、`find` 按原契约读取当前会话中保存的内容。GitHub 返回外部 `redirect_url` 时，可直接交给 `web_fetch`。
- 按当前会话实际使用的模型能力选择图片输入或仅图片元数据；图片进入 dsh 原生附件服务和工具内容，不把 base64 塞进文本。切换模型后仍可读取同一 worker 中的 `ref`。
- 使用独立的无 GitHub 凭据 HTTP client。会话隔离、取消确认及进程回收与 GitHub 工具使用同一套桥接机制；worker 释放后临时 `ref` 失效，已交给 dsh 的图片附件仍可回放。

## 原版来源与版本约定

GitHub 三后端、web_search、web_fetch 及共享 Python 核心逐字来自 `gh-puller@c891809229c2b02dfc739cf77175746daba9d379`，保留原始 HTTP User-Agent、工具描述和错误/恢复提示。两份系统/Bash 提示词逐字来自 `graphub@4864bff12ef8a4dc62b419f77cf0d2abafdb579f`；仅在装配时替换原有占位符。

[UPSTREAM.json](UPSTREAM.json) 逐文件记录来源 commit 和 SHA-256。测试校验每个导入文件的字节摘要，以及导出的工具描述和参数是否与 Python 原定义一致。dsh 适配仅位于 `plugin/` 和 Python worker 入口；后续升级原工具时应整体更新明确的来源版本及校验记录，不能为适配插件而改写原提示词。

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
uv sync --frozen --extra github
npm test
uv run --frozen --extra github ruff check .
uv run --frozen --extra github pytest -q
npm run test:dsh
npm run test:web
```

最后两项要求 Docker/uv 可用，以及 GitHub 凭据已配置。`test:dsh` 加载插件自动准备环境，使用真实 dsh 预设和工具注册表验证：工具隔离与切换、提示词、容器 bash、lsp 源码查询、原生 GitHub API、Web 工具、后台通知及取消。配置了 Brave key 或选择其他搜索后端时实际调用搜索；默认 Brave 且未配置 key 时验证明确的缺少凭据错误。`test:web` 要求先安装本插件包，启动 Web profile，通过认证后的页面和 API 检查预设，随后关闭测试进程；可设置 `DSH_TEST_PROFILE` 测试其他 Web profile。两者均不调用模型。可分别用 `DSH_ASCEND_GITHUB_BACKEND=graphql npm run test:dsh` 和 `DSH_ASCEND_GITHUB_BACKEND=dsl npm run test:dsh` 验证另外两个后端。

项目自包含，不依赖本机 Graphub/gh-puller checkout。Python 依赖使用 `uv.lock`，Node 开发依赖使用 `package-lock.json`。
