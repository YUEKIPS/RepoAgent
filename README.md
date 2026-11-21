# RepoAgent

**阅读代码，追踪流程，用源码核对答案。**

RepoAgent 是一个在本地运行的代码仓库问答工具。添加公开 GitHub 仓库后，可以询问功能实现、调用流程或异常条件，并通过回答中的文件路径、行号和源码片段核对结论。也可以选定多个仓库，分析前后端接口或不同项目之间的协作关系。

版本：**0.14.5 架构重构预发行版**。

### 本快照

此预发行版整理了任务生命周期、Agent 执行层及模型与工具协议。沿用执行 v41、提示 v26 和工具 v17。

- 应用层统一任务启动、恢复、取消和结果提交。
- Agent 执行层分开准备、上下文与答案交付。
- 模型侧分开载荷、响应与传输，工具侧分开声明、分派与投影。
- 多语言声明索引、文件范围与结构概览。
- 具体风险触发的有界复核、局部修订与安全上下文。
- 统一任务思考配置、请求级计量及未知用量保护。
- 搜索支持文件／目录范围，以及内容、文件列表和计数三种返回模式。
- 本地与GitHub按需搜索共用匹配逻辑，导航反馈合并重复来源身份，原始结果仍保留审计。
- 主提示、Agent循环、复核触发与预算保持；成本和准确性不承诺固定提升。

参数、分页和证据边界见[工具协议v17](TOOL-CONTRACT.md)。

使用独立数据目录work/web-snapshot-0.14.5，不加载旧公开版数据库。凭据和数据不随源码交付。

## 可以用它做什么

- **定位实现**：查找某个接口、类、函数或配置在哪里定义。
- **理解流程**：沿着源码梳理请求处理、数据流转和错误处理。
- **连续追问**：在同一个对话中细化问题，历史接近容量限制时按需摘要。
- **多仓分析**：每个对话选择 1～8 个固定版本，联合阅读相关项目。
- **核对证据**：展开引用，查看对应仓库版本、文件和行号。
- **管理对话**：仓库池、对话归档与恢复、五种主题及窄屏布局。

例如：

> 用户登录时，后端在哪里校验令牌？校验失败会返回什么？
>
> 这个 Python 函数的异常在哪一层被处理？
>
> 前端提交的字段与后端接收的字段是否一致？请给出两侧的源码。

RepoAgent 按需读取源码，默认不运行或修改所分析仓库的代码。回答仍可能有误，重要结论应结合引用原文复核。

## 语言与文件支持

| 支持层次 | 语言或文件 |
|---|---|
| 文本检索、常用声明定位、源码引用 | Java、Python（含 `.pyi`）、TypeScript、JavaScript、TSX、JSX、MJS、CJS、Go、C#、C、C++、Rust、PHP |
| 文本读取、检索与引用 | SQL、Shell、PowerShell、Vue、HTML、CSS／SCSS、JSON、YAML、XML、TOML、HCL、Dockerfile、Containerfile及常见构建配置 |

声明索引用于帮助找到代码位置，不能替代完整类型分析、宏展开、运行时调用图或框架分析。`.h` 按 C/C++ 头文件处理，不判断实际编译语言；语法不支持或解析失败时会明确降级为文本读取。

## 快速开始

需要 **Python 3.12** 和可以访问依赖包仓库的网络。前端资源已经包含在项目中，不需要安装 Node.js 或构建前端。

以下命令在仓库根目录执行。本版本在 Windows／Python 3.12 环境验证；其他平台的完整安装流程尚未验证。

### 1. 安装

在 PowerShell 中：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install ./python
```

直接使用虚拟环境中的 Python，无需激活环境或修改 PowerShell 执行策略。如果没有 `py`，可以用已安装的 Python 3.12 可执行程序创建虚拟环境。

### 2. 配置模型

项目提供 DeepSeek 配置样例；使用真实模型需要自行准备相应 API 密钥。

```powershell
Copy-Item config/deepseek-model.example.json config/model.json
Copy-Item .env.example .env
notepad .env
```

在 `.env` 中填写：

```dotenv
DEEPSEEK_API_KEY=你的API密钥
```

`config/model.json` 包含服务地址、模型名、网络空闲等待、上下文窗口、工具调用及输出能力等配置。更换模型服务时，应按实际接口能力调整；仅修改模型名不保证兼容。

### 3. 启动

```powershell
.\.venv\Scripts\python.exe -m repoagent.web --model-config config/model.json --env-file .env --initial-model-source live --output-dir work/web-snapshot-0.14.5 --usage-ledger work/web-snapshot-0.14.5/usage.sqlite3 --port 8765
```

在浏览器打开 **http://127.0.0.1:8765/**。按 `Ctrl+C` 停止；再次使用同一命令启动即可沿用数据。端口已被占用时，可把 `--port` 改为其他空闲端口。

服务仅监听本机地址。启动本身不会调用模型；提问时，问题、所需历史与选中的源码会发送给配置的模型服务，费用由该 API 账户承担。

### 4. 添加仓库并提问

1. 打开“仓库池”，添加公开 GitHub 仓库 URL，可指定分支、标签或完整提交号。
2. 等待仓库目录准备完成，再新建对话，选择 1～8 个仓库版本。
3. 输入问题；需要时附加日志，调整回答详略或原生思考选项。
4. 展开引用核对原文，继续追问。

一个对话的仓库范围和版本在创建后固定。需要分析另一组仓库或更新版本时，新建对话即可。运行设置可调整后续任务的请求次数、整题时限、摘要和上下文保护参数；已运行的任务保持启动时的参数。

## 不配置密钥，先看界面

```powershell
.\.venv\Scripts\python.exe -m repoagent.web --mock-script examples/offline.json --output-dir work/offline-snapshot-0.14.5 --port 8766
```

打开 **http://127.0.0.1:8766/**。此模式使用预设响应，不具备任意问题的理解能力，也不调用模型；初始仓库池为空。主动添加仓库仍会访问 GitHub。

## GitHub 连接

默认匿名访问公开 GitHub 仓库，受 GitHub 请求额度限制。若需要使用自己的 GitHub 令牌，可新建本机文件 `.env.github`：

```dotenv
GITHUB_TOKEN=你的GitHub令牌
```

在启动命令中追加：

```text
--github-token-env GITHUB_TOKEN --github-env-file .env.github
```

GitHub 令牌和模型密钥应分别配置。令牌不会将产品的访问范围扩展到私有仓库。

## 数据与隐私

- 对话、源码缓存、引用、检查点和用量记录保存在启动命令指定的 `work/web-snapshot-0.14.5` 中，不包含在本仓库中。
- 备份前先停止服务，再复制完整数据目录；不同服务不要共用一个数据目录。
- 不要提交 `.env`、实际配置、运行数据或日志；项目的 `.gitignore` 已排除常见路径。
- 升级时使用与版本匹配的数据目录；本版不自动迁移旧版本数据库。
- 流式内容在引用检查和保存完成前属于草稿。请求用量未知时不会自动重复发送；自动继续默认关闭。

## 可选扩展

支持通过 MCP 接入外部工具，以及加载文本 Skill。默认不启用扩展。

复制 `config/extensions.example.json` 为 `config/extensions.json` 后，可在启动命令中追加 `--extensions-config config/extensions.json`。空配置不会启动外部服务。只配置可信扩展；stdio MCP 会启动配置指定的本机程序，文本 Skill 本身不授予额外工具权限。

## 当前限制

- Web 入口支持公开 GitHub 仓库，暂不提供本地路径导入或私有仓库导入。
- 每个仓库最多 2,000 个源码文件、5,000 个可读文件，单文件最多 1 MiB；还受目录、总大小和任务预算约束。超大仓库可能无法加入。
- 没有符号索引不代表没有实现；文本覆盖与结构覆盖会分别记录。
- 有效引用只能确认来源和位置，不能保证结论完全正确；运行行为还需要测试、日志或其他证据。

## 项目结构

```text
.
├─ README.md
├─ .env.example
├─ config/
│  ├─ deepseek-model.example.json
│  └─ extensions.example.json
├─ examples/offline.json
└─ python/
   ├─ pyproject.toml
   └─ src/repoagent/
      ├─ web/          # 本地页面与接口
      ├─ execution/    # Agent 执行、会话、上下文、证据与发布
      ├─ repositories/ # GitHub 来源与缓存
      ├─ snapshots/    # 文件处理与语言索引
      ├─ tools/        # 仓库读取与外部工具
      └─ models/       # 模型适配与 token 计量
```

## 许可与第三方资源

RepoAgent 尚未声明项目许可证。

前端使用的 Marked 保留其 [MIT 许可](python/src/repoagent/web/static/marked-LICENSE.txt)和[第三方声明](python/src/repoagent/web/static/THIRD_PARTY.txt)。内置 tokenizer 的来源与校验值见[来源说明](python/src/repoagent/models/assets/PROVENANCE.md)。
