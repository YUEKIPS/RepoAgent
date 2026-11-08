# RepoAgent

RepoAgent 是面向代码仓库理解与证据核查的本地工具。支持公开 GitHub 仓库、连续追问和固定范围的多仓分析，回答保留仓库版本、文件路径、行号及可展开的源码引用。

当前版本：**0.8.0**。本目录是运行源码副本，安装依赖后可独立启动，不需要原开发目录、历史发行版或开发者本机路径。

## 功能

- 全局仓库池；创建对话时选择 1～8 个固定读取版本，创建后范围固定。
- Java、TypeScript／JavaScript 常用声明定位及文本检索；其他语言的语义覆盖有限。
- Session 事件与检查点、按请求容量触发的历史摘要、冷热证据和工具结果裁剪。
- 流式正文、引用校验和修正、任务停止及安全恢复；服务自动继续默认关闭。
- 对话／仓库重命名、归档、回收站与恢复；五种主题及手机侧栏。
- 可选 MCP 工具及文本 Skill 配置，默认不加载外部扩展。

程序默认不运行导入仓库的代码。引用位置有效不等于引用充分支持结论，重要结论仍需核查原文。

## 环境与安装

使用 **Python 3.12**（发行包要求 `>=3.12,<3.13`）。本版本在 Windows 上验证；其他操作系统未在本次副本检查中验证。首次安装需要连接包仓库下载依赖。前端静态资源已随源码提供，不需要 Node.js 或单独构建前端。

在本目录打开 PowerShell：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install ./python
```

如果没有 `py` 命令，可使用已安装的 Python 3.12 的 `python` 命令创建虚拟环境。下文直接使用虚拟环境的解释器，无需修改 PowerShell 执行策略。

## 配置真实模型

复制公开样例：

```powershell
Copy-Item config/deepseek-model.example.json config/model.json
Copy-Item .env.example .env
notepad .env
```

在 `.env` 中填写自己的密钥：

```dotenv
DEEPSEEK_API_KEY=填写你自己的密钥
```

`config/model.json` 指定模型服务、模型名、协议、输出能力、上下文窗口及密钥变量名。附带样例为 0.8.0 使用的 DeepSeek 配置；服务能力发生变化或换用其他路由时，应按实际服务调整，不能只改模型名称却沿用不匹配的能力参数。

启动：

```powershell
.\.venv\Scripts\python.exe -m repoagent.web --model-config config/model.json --env-file .env --initial-model-source live --output-dir work/web --usage-ledger work/web/usage.sqlite3 --port 8765
```

浏览器打开 **http://127.0.0.1:8765/**。按 `Ctrl+C` 停止，之后使用同一命令启动会沿用本目录的 `work/web` 数据。启动本身不会自动发送模型请求。

提问时，问题、所需对话历史和选中的源码会发送至配置的模型服务，费用由你配置的 API 账户承担。不要将 `.env`、实际运行配置、数据或日志上传到公开仓库；本副本的 `.gitignore` 已排除这些常见路径。

## 使用

1. 打开“仓库池”，添加公开 GitHub 仓库地址，可指定分支、标签或完整提交号；等待固定目录准备完成。
2. 点击“新建对话”，选择 1～8 个已准备的读取版本并确认。创建期间可添加仓库后返回，原选择会保留。
3. 输入问题，可附加日志、调整回答方式和原生思考选项。
4. 点击“查看引用”核对版本、文件与行号，展开原文检查结论。
5. 使用“运行设置”调整下次任务的请求次数、时限、输出、摘要和证据容量。已运行任务保持原参数。

GitHub 默认使用匿名连接，受其请求额度限制。需要显式使用自己的 GitHub 令牌时，可另外创建本机 `.env.github`，填写 `GITHUB_TOKEN=...`，并在启动命令追加 `--github-token-env GITHUB_TOKEN --github-env-file .env.github`。GitHub 令牌与模型密钥应分开，不要上传这些文件。仅支持公开仓库，不用令牌提升访问范围。

已有对话不能加仓或改仓；需要另一组仓库时从普通新建入口创建对话。Web 本地路径导入暂不支持。归档／回收站隐藏内容但保留历史；本副本没有旧版本数据迁移功能。

## 不使用模型密钥的页面预览

```powershell
.\.venv\Scripts\python.exe -m repoagent.web --mock-script examples/offline.json --output-dir work/offline --port 8766
```

打开 **http://127.0.0.1:8766/**。这是预设脚本模式，不会理解任意问题，不代表模型效果；初始仓库池为空。本模式无模型调用，但主动添加 GitHub 仓库仍会访问 GitHub。

## 外部扩展（可选）

默认不接入 MCP 或 Skill。需要时复制 `config/extensions.example.json` 为本机 `config/extensions.json`，再使用 `--extensions-config config/extensions.json` 显式加载配置；空样例不会启动外部服务。仅接入自己信任的服务与技能；stdio MCP 服务可能启动配置指定的本机程序。

## 文件与数据

```text
RepoAgent-Public-0.8.0/
├─ README.md
├─ .gitignore
├─ .env.example
├─ config/
│  ├─ deepseek-model.example.json
│  └─ extensions.example.json
├─ examples/offline.json
└─ python/
   ├─ pyproject.toml
   └─ src/repoagent/       # Python 程序、页面资源及 tokenizer
```

运行后产生的仓库缓存、对话、引用、检查点和用量账本位于指定的 `--output-dir`；上述真实模式将用量账本明确放入同一目录。备份运行数据时先停止对应服务，再备份整个目录。不要让两个服务同时打开同一个数据目录；端口冲突时修改 `--port`。

该副本基于本地 Git 检查点 `1334f32`，包含完整 Python 包，未拆删可能被命令行入口导入的模块；开发测试、评测题集、内部文档、旧发行包和 Git 历史不在上传副本中。

## 验证与许可说明

0.8.0 原开发版完成过后端定向 130 项、前端 64 项及相关静态检查，这些结果不等于全量或真实模型语义验收。本次上传副本另外检查源码资源一致性、打包和本机模拟启动，不重复宣称完成真实模型评测；首次从公共包仓库安装全部依赖仍取决于所用网络及平台。

前端内置 Marked，其 MIT 许可与声明保留在 `python/src/repoagent/web/static/marked-LICENSE.txt`、`THIRD_PARTY.txt`。内置 tokenizer 的来源和校验值见 `python/src/repoagent/models/assets/PROVENANCE.md`。发布者尚未为 RepoAgent 选定项目许可证，本副本没有擅自添加许可证；公开发布前请确定项目许可证并核对随包第三方资源的许可条件。
