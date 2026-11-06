# zotero-translate

**English summary.** zotero-translate translates English academic papers from Zotero into Simplified Chinese. You right-click an item, a local service on your Mac translates the PDF with an OpenAI-compatible LLM endpoint of your choice (using a modified copy of the [BabelDOC](https://github.com/funstory-ai/BabelDOC) engine), and two PDFs are attached back to the item: a Chinese-only translation and a bilingual side-by-side version. It runs on macOS (tested on Apple silicon only) with Zotero 7–10, and is licensed under AGPL-3.0. The rest of this document is in Chinese. If you use an AI coding agent, open this repository with it: `AGENTS.md` tells the agent how to install and test everything.

---

从 Zotero 提交一篇英文论文，取回两份 PDF，挂回原条目：

- **中文译文**：只有中文的单语 PDF；
- **双语对照**：原文在左、译文在右的并排 PDF。

目录：[这是什么](#1-这是什么) · [系统要求](#2-系统要求) · [安装](#3-安装) · [使用](#4-使用) · [数据和隐私](#5-数据和隐私) · [排错](#6-排错) · [更新和卸载](#7-更新和卸载) · [许可证和致谢](#8-许可证和致谢)

## 1. 这是什么

在 Zotero 里右键一篇论文，选“翻译为中文”。翻译在本机的一个后台服务里进行，论文的文字分段发给你配置的模型端点；完成后右下角提醒，两份 PDF 已经挂在原条目下。

**译文的特点**

- **关键概念保留英文。** 在本文中承担核心方法、机制或命名作用的概念，译文里保留英文；已有成熟中文说法的基础术语用中文。节标题一律译成中文。
- **首次出现附中文释义。** 关键概念在摘要和正文中各自第一次出现时，后面跟一句简短的中文释义，之后只写英文。
- **伪代码保留原模板。** 算法框里的行号、缩进、循环和分支层次不变，只翻译其中的自然语言。某个算法翻译失败时，只把这一个算法退回原文，其余照常翻译。
- **保留行内格式。** 粗体、斜体、公式、链接。
- **先读全文，再分批翻译，再审校。** 翻译前先读整篇论文，生成论文档案和本篇术语清单；每批段落连同所在章节和前后段落一起发给模型；初译完成后再审校一遍，同时核对数字、公式和字面值。

**任务不丢**

- 翻译服务重启（更新、崩溃、重新登录）后，没做完的论文自动接着做，不用重新提交。
- 一次尝试失败（超时、网络中断、端点出错）后，服务自动重跑，最多 2 次；密钥错误（HTTP 401、403）这类重跑也没用的，直接报失败。
- 随时可以取消；取消的任务不会再被自动重启。
- 挂回附件时 Zotero 退出了，重启后只补挂还没挂上的那份，不会出现重复附件；挂回出错时可以“继续挂回”，不用重新翻译。

**局限**

- 译文是模型的输出，质量取决于你用的模型。引用的数字、公式请自己核对。
- 版面还原由 BabelDOC 完成，表格、作者栏、图注等复杂版面可能有瑕疵。
- 只在 macOS 上测试过。

## 2. 系统要求

| 项目 | 要求 |
|---|---|
| 电脑 | macOS 14 或更高，Apple 芯片（M 系列）。只在 Apple 芯片上测试过：锁定的 `onnxruntime` 在 macOS 上只有 Apple 芯片的安装包 |
| Zotero | 7、8、9 或 10（插件声明的范围是 6.999 到 10.\*）。在 10.0.7 上测试过 |
| [uv](https://docs.astral.sh/uv/) | 它会下载并管理 Python 3.12，不用自己装 Python |
| git | 克隆仓库用 |
| 磁盘空间 | 安装本身约 1.5 GB：`.venv` 约 0.6 GB，`~/.cache/babeldoc`（版面模型、字体）约 0.5 GB（下载后约 340 MB，第一篇论文翻译时再多约 180 MB 的编译缓存），Python 和 uv 缓存另计。之后每篇论文的任务目录约为原文 PDF 的 4 倍（22 MB 的论文约 86 MB）。建议留出 10 GB 以上 |
| 网络 | 安装时下载 Python 包、Python 3.12、版面模型和字体（`pypi.org`、`files.pythonhosted.org`、`github.com`、`huggingface.co` 或 `modelscope.cn`、`openaipublic.blob.core.windows.net`）。装好后，运行时只访问你配置的模型端点 |
| 模型端点 | 一个 OpenAI 兼容的 Chat Completions 端点和密钥，要求见下 |

### 对模型端点的要求

- **接口。** 程序向 `<OPENAI_BASE_URL>/chat/completions` 发 POST，带 `Authorization: Bearer <密钥>`，请求体含 `model`、`messages`、`temperature`（1.0）和 `reasoning_effort`（`"high"`），从 `choices[0].message.content` 读回答。思考时间很长的模型可以打开流式（`ZT_STREAM=1`，见下）。不认识 `reasoning_effort` 的端点可能返回 HTTP 400。
- **模型。** 要能严格按指令输出 JSON 和保持格式的强推理模型。论文档案那一步会把整篇论文的文字放进一个请求，约每页 1000–1500 token，所以上下文窗口建议不小于 128K token。开发时按 Gemini 系列的模型调出来；试过一个 DeepSeek 的快速版本，关键概念、首次出现释义和翻译质量都明显不如 Gemini。模型越弱，译文越差。
- **请求量和用时。** 一篇 22–31 页的论文约 270–450 次请求，端点正常时约 5–9 分钟（3 篇测试论文：299–511 秒，273–441 次请求）。端点响应慢的时候，同样的论文曾经用到十几、二十几分钟。费用按你的端点计价，建议先拿一两篇试。
- **并发。** 默认同时发出的请求最多 40 个。端点限流时调小，见[配置模型](#配置模型)。

## 3. 安装

下面的命令在终端里运行。`~/zotero-translate` 只是建议的位置，不要放在“文稿”“桌面”“下载”或 iCloud 云盘里：macOS 可能不让登录时由系统启动的后台服务读取这些位置。

### 3.1 取得代码，建环境

```sh
cd ~
git clone https://github.com/JeRrySun-Yong/zotero-translate.git
cd zotero-translate
uv sync --frozen
```

`--frozen` 让 uv 照 `uv.lock` 原样安装，不重新解析版本。成功时最后几行列出安装的包，`.venv/bin/zotero-translate` 存在。还没有 uv 的话先装：`curl -LsSf https://astral.sh/uv/install.sh | sh`，或 `brew install uv`。

### 3.2 下载运行资源

```sh
.venv/bin/babeldoc --warmup
```

下载并校验版面模型、字体等到 `~/.cache/babeldoc`。结束时打印 `Warmup completed, exiting...`。可能要等一两分钟。此后翻译不再联网下载资源。

### 3.3 配置模型

翻译服务用两个环境文件，在 `~/.config/zotero-translate/` 里。**两个文件都必须有，缺任何一个，服务启动即退出。** 密钥只放在这里，不要放进仓库。

| 文件 | 用途 |
|---|---|
| `model.env` | 主模型：翻译、审校、伪代码 |
| `argon.env` | 副模型：论文档案、本篇术语清单、术语释义；主模型的请求太慢时，也由它重发或并行发一份。文件名沿用代码里的叫法 |

先建好空文件并收紧权限，再用编辑器填内容，这样密钥不会出现在一个别人可读的文件里：

```sh
mkdir -p ~/.config/zotero-translate
cd ~/.config/zotero-translate
touch model.env argon.env
chmod 600 model.env argon.env
nano model.env      # 任何编辑器都行
nano argon.env
cd ~/zotero-translate
```

文件每行写 `键=值`，两个文件格式相同。`#` 开头的整行是注释；值后面不能跟行内注释。

```
OPENAI_BASE_URL=https://api.example.com/v1
OPENAI_API_KEY=填你自己的密钥
AI_DEFAULT_MODEL=填你的模型 ID
ZT_PROFILE_NAME=我的模型
```

| 键 | 必需 | 含义 |
|---|---|---|
| `OPENAI_BASE_URL` | 是 | 端点地址，程序在它后面加 `/chat/completions`。例如 `https://api.openai.com/v1` |
| `OPENAI_API_KEY` | 是 | 密钥 |
| `AI_DEFAULT_MODEL` | 建议总是写 | 模型 ID。不写时用程序里的默认值（`model.env` 是 `gemini-3.8-flash-high`，`argon.env` 是 `gemini-4-argon-high`），这是开发时所用端点上的名字，别的端点上多半没有 |
| `ZT_PROFILE_NAME` | 否 | 只写在 `model.env` 里。这份“内置配置”在右键菜单、设置页、任务窗口里显示的名字，最长 60 个字符；不写就叫 Gemini |
| `ZT_REQUEST_TIMEOUT_SECONDS` | 否 | 单个请求的超时秒数，默认 480。回答特别慢的模型可以写 900 |
| `ZT_STREAM` | 否 | 写 `1` 则用流式请求。思考很久的模型，有的端点会在请求等了约两分钟还没有回答时断开连接（HTTP 524），这时打开 |
| `ZT_CONCURRENCY` | 否 | 只写在 `argon.env` 里：副模型同时发出的请求上限，默认 16 |

`model.env` 的并发上限不在文件里，而是 `serve` 的 `--concurrency` 选项（默认 40）。

#### 配置模型

**只有一个端点（最常见）。** 两个文件填同样的 `OPENAI_BASE_URL` 和 `OPENAI_API_KEY`。程序把它们当作同一个端点：两个角色共用一份并发额度，大小取 `--concurrency`（`argon.env` 里的 `ZT_CONCURRENCY` 被忽略），也不会对冲。两个文件的 `AI_DEFAULT_MODEL` 可以相同，也可以让副模型用更强的那个。端点限流时，给 `serve` 加 `--concurrency 8` 这样的选项（写法见[开机自动启动](#35-开机自动启动)）。

**两个端点。** 主模型用便宜快速的端点，副模型用更强的端点。此时开启“对冲”：主模型的一个翻译批次或审校请求发出 400 秒还没结束，就多发一份给副模型，用先通过校验的回答。`serve` 加 `--no-hedging` 关闭对冲，关闭后改为主模型连续 2 次超时才转发给副模型。

**检查端点能不能用。** 内置配置没有“测试连接”按钮。拿不准端点或模型 ID 对不对，可以先在 Zotero 设置页（装好插件之后，见[使用](#44-设置页中文翻译)）新建一份同样地址的配置，点“测试连接”。

不要把密钥发给别人或贴进聊天、issue。

### 3.4 手动试启动

先确认服务能起来。在仓库根目录运行：

```sh
.venv/bin/zotero-translate serve
```

成功时终端打印 `{"url": "http://127.0.0.1:8920"}` 并一直运行。另开一个终端：

```sh
curl -s http://127.0.0.1:8920/health
```

应返回 `{"service": "zotero-translate", "version": "0.1.0"}`（版本号以后会变）。回到第一个终端按 Ctrl-C 结束服务。

启动即退出时，终端里就是原因：

| 终端里的话 | 退出码 | 怎么办 |
|---|---|---|
| `找不到管线模型的环境文件：<路径>` 或 `找不到 argon（…）的环境文件：<路径>` | 2 | 补上那个文件（3.3） |
| `环境文件 <路径> 缺少：OPENAI_BASE_URL, OPENAI_API_KEY`（列出缺的键） | 2 | 补上缺的键 |
| `端口 8920 已被占用，本服务退出` | 1 | 见[排错](#6-排错) |
| `已有翻译服务在使用数据目录 …` | 1 | 已经有一个服务在跑了，不用再启动 |

### 3.5 开机自动启动

用 macOS 的 LaunchAgent：登录后自动启动，崩溃后自动重启。在仓库根目录运行下面整段，它把 `$HOME` 和当前目录写进 plist（launchd 不展开 `~` 和 `$HOME`，plist 里必须是绝对路径）：

```sh
mkdir -p ~/Library/LaunchAgents ~/Library/Logs/zotero-translate
REPO="$PWD"
cat > ~/Library/LaunchAgents/local.zotero-translate.service.plist <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>local.zotero-translate.service</string>
  <key>ProgramArguments</key>
  <array>
    <string>$REPO/.venv/bin/zotero-translate</string>
    <string>serve</string>
    <string>--env-file</string>
    <string>$HOME/.config/zotero-translate/model.env</string>
    <string>--argon-env-file</string>
    <string>$HOME/.config/zotero-translate/argon.env</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$REPO</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$HOME/Library/Logs/zotero-translate/service.log</string>
  <key>StandardErrorPath</key>
  <string>$HOME/Library/Logs/zotero-translate/service.log</string>
</dict>
</plist>
EOF
plutil -lint ~/Library/LaunchAgents/local.zotero-translate.service.plist
```

最后一行应打印 `…plist: OK`。要加选项（例如 `--concurrency 8`、`--no-hedging`、`--port 8921`），在 `ProgramArguments` 里 `argon.env` 那一行之后加 `<string>--concurrency</string>` 和 `<string>8</string>` 这样的成对条目，然后重新生成并重新载入。

安装、查看、重启、停用：

```sh
# 安装并立即启动（以后每次登录自动启动）
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.zotero-translate.service.plist

# 查看状态：应有 state = running 和一个 pid
launchctl print gui/$(id -u)/local.zotero-translate.service | grep -E 'state|pid|last exit'
curl -s http://127.0.0.1:8920/health

# 重启（更新代码之后用）
launchctl kickstart -k gui/$(id -u)/local.zotero-translate.service

# 停用：立即停止并卸载；下次登录也不要载入的话再执行 disable
launchctl bootout gui/$(id -u)/local.zotero-translate.service
launchctl disable gui/$(id -u)/local.zotero-translate.service
# 恢复：launchctl enable gui/$(id -u)/local.zotero-translate.service，再 bootstrap
```

- 改了 plist 之后，`kickstart` 不会重新读取它：先 `bootout` 再 `bootstrap`。
- 要停服务，用 `bootout`。只结束进程（`kill`、活动监视器）没用，`KeepAlive` 会立刻把它拉起来。
- 服务日志是 `~/Library/Logs/zotero-translate/service.log`。
- 安装后 macOS 可能弹出“已添加后台项目”的通知。在“系统设置 → 通用 → 登录项与扩展”里，保持这一项开启。
- 服务退出时正在翻译的论文会中断，下次启动时自动接着做（连续 3 次被中断才判失败）。

### 3.6 安装插件

先得到 `zotero-translate.xpi`，二选一：

- 从仓库的 Releases 页面下载；
- 自己构建：在仓库根目录运行 `.venv/bin/python plugin/build.py`，输出在 `plugin/dist/zotero-translate.xpi`（用 `--output <路径>` 改位置）。

然后在 Zotero 里：

1. 菜单“工具 → 插件”（Tools → Plugins）；
2. 点右上角的齿轮，选“从文件安装插件…”（Install Plugin From File…），选这个 xpi；
3. 插件列表里出现 “Zotero Translate”。

打开 Zotero 设置（macOS 上是“Zotero → 设置…”），左边有一页“中文翻译”。最上方应显示绿底的“翻译服务在运行，当前配置：…”。红底的“连不上翻译服务”说明服务没在运行，见[排错](#6-排错)。

插件只保存一项设置：翻译服务地址（页面底部“高级”里，默认 `http://127.0.0.1:8920`）。密钥和翻译参数都在服务一侧。

## 4. 使用

### 4.1 提交

在文献列表里选中一篇或几篇论文（可以多选），右键，选**“翻译为中文（配置名）”**。括号里是当前模型配置的名字（内置配置默认叫 Gemini，可以用 `ZT_PROFILE_NAME` 改）。

- 选中的是条目或 PDF 附件都行。条目有多个 PDF 时，翻译第一个不是译文的 PDF。选中的东西里没有 PDF 时，这一项变灰并写着“（没有 PDF）”。
- 提交时不弹窗口，不抢焦点。右下角出现“已提交 N 篇”的小提示，几秒后自己消失；点它会打开任务窗口。
- 这篇论文已经有译文时，右下角先问“已有译文 / 再翻译一次？”，点“再翻译一次”才提交。这篇论文有没结束的任务时，这次提交被忽略。
- 多篇论文逐篇翻译，一次只做一篇。

### 4.2 任务窗口

右键菜单里的**“翻译任务”**，或菜单栏“工具 → 翻译任务”，打开任务窗口。每个任务一行：论文、配置、阶段、进度条、已用时间、状态。

| 状态 | 含义 |
|---|---|
| 提交中、排队中 | 正在上传；在服务的队列里（会写第几位） |
| 翻译中 | 正在翻译，“阶段”列写做到哪一步 |
| 等待自动重跑 | 上次尝试失败了，几分钟后自动再试（共最多 2 次），不用处理 |
| 挂回中、挂回失败 | 译文已经做好，正在加到原条目下；出错时点“继续挂回”，不用重新翻译 |
| 已挂回 | 完成：两份 PDF 已经挂在原条目下 |
| 失败 | 后面写着原因 |
| 取消中、已取消 | 取消时服务要先结束这篇的所有进程，确认退出后才显示“已取消” |
| 等待服务 | 暂时连不上翻译服务，恢复后自动继续，不判失败 |

每行的按钮：

- **取消**：点一次变成“确认取消？”，几秒内再点一次才取消；
- **重试**：失败或已取消的任务，重新提交为一个新任务，没变的模型回答直接复用；
- **继续挂回**：挂回失败的任务；
- **打开译文**、**打开双语**、**在库中显示**：完成的任务；
- 窗口右上角的**清除已结束**：移走已完成、失败、已取消的行。

关闭任务窗口不影响翻译。关闭 Zotero 也不影响，服务继续做；重新打开 Zotero 后，没结束的任务仍在任务窗口里，继续跟踪。已完成、失败、已取消的行重启后不再保留（译文已经在原条目下）。

### 4.3 完成提醒

任务完成时右下角弹出“翻译完成”，带“打开译文”“打开双语”“在库中显示”三个链接；失败时弹出“翻译失败”和原因。提醒会一直留到你点它。

原条目下多出两个附件，标题是“中文译文”和“双语对照”，文件名是 `<原文件名>-中文译文.pdf` 和 `<原文件名>-双语对照.pdf`。如果提交的是没有父条目的独立 PDF，两份译文是独立附件，并和原 PDF 互为“相关条目”。

### 4.4 设置页“中文翻译”

Zotero 设置里的“中文翻译”页，从上到下：

- **翻译服务**：一行状态，“翻译服务在运行，当前配置：名称（模型 ID）”，或“连不上翻译服务（地址）”。
- **模型配置**：配置列表，● 标出当前配置，列有名称、模型、状态（内置，只读 / 测试通过 / 测试未通过 / 未测试）和密钥（只显示“已设置 ••••末 4 位”）。点一行编辑它，点“新建配置”新建。第一项是内置配置，由 `model.env` 和 `argon.env` 决定，这里只读。
- **新建配置 / 编辑配置**：名称、地址、密钥、模型 ID，以及可选的并发上限（默认 16）、请求超时（默认 480 秒）、流式请求。
  - “测试连接”先保存，再用论文任务的参数（同一模型、high 推理强度、同样的温度和流式设置）发一个极小的请求，最长约 1 分钟。
  - 测试通过后点“应用”：之后提交的论文用这份配置。已经提交的论文仍用提交时的配置，重试也一样；任务窗口的“配置”列显示每篇用的是哪份。
  - “删除”要再点一次确认。还有没结束的任务在用的配置不能删。
  - 用户配置的两个角色（主模型和副模型）用同一个端点，共用并发额度，不对冲。
- **高级 → 翻译服务地址**：默认 `http://127.0.0.1:8920`。只接受本机地址（`127.0.0.1` 或 `localhost`），别的地址插件不发送配置和密钥。

密钥只发给本机翻译服务一次，保存在 `~/.config/zotero-translate/profiles/<配置号>.env`（权限 600）；Zotero 不保存，服务给插件回的也只有“已设置”和末 4 位。

### 4.5 切换模型

右键菜单的**“切换模型 ▸”**列出所有“测试通过”的配置（包括内置配置），点一下就切换，不用开设置页。当前配置带单选标记；右键第一项“翻译为中文（配置名）”也随之变化。切换只影响之后提交的论文。

## 5. 数据和隐私

- **论文内容发给你配置的端点。** 论文的文字（不是 PDF 文件本身）按段落分批发给 `model.env` 和 `argon.env`（或你在设置页新建的配置）里的端点，包括整篇论文的文字（生成论文档案时）。端点怎么处理这些内容，由端点的提供方决定，请先确认它符合你的要求。除安装时下载运行资源外，程序不访问别的地址。
- **密钥只在本机的文件里。** 环境文件和 `profiles/` 下的文件权限是 600。Zotero 不保存密钥。服务不把密钥写进日志和任务报告。
- **服务只监听 `127.0.0.1`，没有身份验证。** 同一台电脑上的其他程序和用户可以访问它。不要在多人共用的电脑上运行。
- **数据目录**是 `~/Library/Application Support/zotero-translate`（`serve` 的 `--data-dir`，或环境变量 `ZT_DATA_DIR` 可改）：
  - `tasks/<任务号>/`：每篇论文一个目录。`source.pdf`（提交的原文）、`mono.pdf`（单语译文）、`dual.pdf`（双语对照）、`dossier.json`（论文档案）、`terms.json`（本篇术语清单）、`task.log`（日志）、`status.json`（状态，由服务写）、`report.json`（任务报告）、`attempts/<n>/`（每次尝试的报告和请求记录）；
  - `model-cache/`：成功的模型回答的缓存。同一篇论文再次提交时，命中缓存的请求不再调用模型。里面是论文译文的片段，按论文内容对待；
  - `model-profiles.json`：当前配置和各配置最近一次测试连接的结果；`service.lock`：防止同一个数据目录被两个服务同时使用。
- **清理旧任务。** 插件挂回附件时，会把 PDF 复制进 Zotero 自己的存储，所以挂回之后任务目录可以删。只删已经结束的任务（`succeeded`、`failed`、`cancelled`），不要删排队中、翻译中、等待自动重跑的。删之前打开任务窗口，确认其中没有“挂回中”“挂回失败”“等待服务”“取消中”的行，否则“继续挂回”会失败。

  下面在仓库根目录运行：先列出 30 天以前的已结束任务，看过再删。

  ```sh
  D="$HOME/Library/Application Support/zotero-translate/tasks"
  find "$D" -mindepth 1 -maxdepth 1 -type d -mtime +30 -print0 |
    xargs -0 .venv/bin/python -c '
  import json, pathlib, sys
  for d in sys.argv[1:]:
      try:
          s = json.loads((pathlib.Path(d) / "status.json").read_text("utf-8"))
      except (OSError, ValueError):
          continue  # 没有可读的状态文件：不列出
      if s.get("state") in ("succeeded", "failed", "cancelled") and not s.get("cleanup_error"):
          print(d)
  ' > "$HOME/old-tasks.txt"
  cat "$HOME/old-tasks.txt"
  # 确认无误后删除：
  tr '\n' '\0' < "$HOME/old-tasks.txt" | xargs -0 rm -rf
  ```

  `model-cache/` 很小，一般不用清理。要清理就在没有任务运行时删掉它，代价是以后重译同一篇论文要重新调用模型。

## 6. 排错

**先看哪里。** 服务日志是 `~/Library/Logs/zotero-translate/service.log`（用 LaunchAgent 启动时）。它记录每次启动的 `{"url": …}`、启动错误、未处理的异常，和每个任务的事件：提交、开始、中断、重新排队、等待自动重跑、接受取消、已取消、成功、失败等，每个一行：

```
2026-11-03T02:40:00-08:00 task=<任务号> attempt=1 event=失败 file="paper.pdf" reason="…"
```

按原文件名找任务：`grep 'file="paper.pdf"' ~/Library/Logs/zotero-translate/service.log`。

**任务失败后看哪份报告。** 任务目录 `~/Library/Application Support/zotero-translate/tasks/<任务号>/` 里：

- `report.json` 是最后一次尝试的报告。`state` 应是 `succeeded`；失败时 `error` 是原因，`failed_batches` 列出失败的批次。`stages` 是各阶段用时和请求数，`model` 记着用了哪些模型；
- `task.log` 有异常堆栈；
- `attempts/<n>/report.json` 是第 n 次尝试的报告。

最新的任务目录：`ls -t "$HOME/Library/Application Support/zotero-translate/tasks" | head -1`。

| 现象 | 原因和做法 |
|---|---|
| 设置页是红底“连不上翻译服务”；提交时提示“无法连接翻译服务（服务地址：…）” | 服务没在运行。`launchctl print gui/$(id -u)/local.zotero-translate.service` 看 `state` 和 `last exit code`，`service.log` 看启动错误；`launchctl kickstart -k gui/$(id -u)/local.zotero-translate.service` 重启。服务地址不对时，在设置页底部“高级”里改 |
| `service.log` 每 10 秒出现同一条启动错误 | 环境文件缺失或缺键（3.4 的表）；或端口被占用；或数据目录被另一个服务锁定（例如手动试启动的服务还没结束）。launchd 在启动失败时约每 10 秒重试一次 |
| 启动时报“端口 8920 已被占用” | `lsof -nP -iTCP:8920 -sTCP:LISTEN` 看是谁。是旧的 zotero-translate 进程就结束它；是别的程序，就给 `serve` 加 `--port 8921`（改 plist），并在设置页“高级”里把服务地址改成 `http://127.0.0.1:8921` |
| 任务失败，原因含 `HTTP 429`（“管线模型限流”）或额度用完 | 端点限流或账户额度用完。服务已经按 60 秒、300 秒的间隔自动重跑过两次才报失败。等限流恢复或充值后，在任务窗口点“重试”（做完的模型回答有缓存，不重复收费）。经常限流时调小并发：`serve --concurrency 8`（内置配置），或设置页里该配置的“并发上限” |
| 任务失败，原因含 `HTTP 401` 或 `403` | 密钥错误或无权限。这类失败不自动重跑。改对环境文件里的密钥（改完重启服务：`kickstart`），或在设置页改配置的密钥，再点“重试” |
| 任务失败，原因含“超时” | 端点回答太慢。内置配置在环境文件里加大 `ZT_REQUEST_TIMEOUT_SECONDS`（例如 900），设置页配置填“请求超时”；思考很久的模型再打开流式 |
| 任务窗口写着“等待自动重跑（约 N 分钟后，第 k 次自动重跑，共 2 次），上次失败：…” | 不用处理。两次自动重跑都失败才显示“失败” |
| 任务窗口写着“翻译服务暂时连不上，恢复后继续（已 N 分钟）” | 任务不会丢。检查服务是否在运行，恢复后自动继续 |
| 失败原因是“翻译服务多次重启，任务中断” | 同一个任务连续 3 次遇到服务重启。先查 `service.log` 里服务为什么反复重启，再重新提交 |
| 失败原因是“译文已不在翻译服务中，请重试” | 任务目录已被清理。点“重试” |
| 失败原因是“提交未完成，请重试” | 上传还没完成时 Zotero 退出了。点“重试” |
| 失败原因是“找不到模型配置的文件…” | 这份配置的文件在服务之外被删了。不要点“重试”（它沿用原来的配置号）：新建配置、测试连接、应用（或切换到别的配置），然后在原条目上重新提交 |

**设置页“测试连接”失败的几种提示**（后面还有一行端点自己的说明）：

| 提示 | 检查什么 |
|---|---|
| 地址无效 | 地址要以 `http://` 或 `https://` 开头，有主机名，端口在 1–65535 |
| 地址连不上 | 地址和网络 |
| 密钥无效 | 端点返回了 HTTP 401 或 403：检查密钥 |
| 模型不存在 | 端点返回了 HTTP 404，或 400/422 并提到模型：检查模型 ID |
| 请求超时 | 端点没有按时回答，可以稍后再试，或加大请求超时 |
| 回答不能解析 | 确认地址是 OpenAI 兼容接口、模型 ID 正确；模型要能回答 JSON `{"ok": true}` |
| 端点出错 | 其他 HTTP 错误，例如 429 或 5xx：稍后再试 |

## 7. 更新和卸载

### 更新

在仓库根目录运行：

```sh
git pull --ff-only
uv sync --frozen
.venv/bin/babeldoc --warmup        # 资源已齐时很快结束
launchctl kickstart -k gui/$(id -u)/local.zotero-translate.service
curl -s http://127.0.0.1:8920/health
```

- 更新前确认任务窗口里没有进行中的任务；有也不会丢，重启后会接着做。
- `git pull --ff-only` 失败，说明本地仓库被改过，需要自己处理。
- 如果这次更新改了插件（`git diff --stat <更新前的提交> HEAD -- plugin/addon` 有输出），要重新装插件：到 Releases 下载新的 xpi，或自己构建，再按 3.6 安装。从 Releases 安装的插件，Zotero 也会按插件里登记的更新地址自动检查更新。

### 卸载

1. 在 Zotero 的“工具 → 插件”里移除 “Zotero Translate”。已经挂回的 PDF 是 Zotero 自己的附件，不受影响。
2. 停用并删除服务：

   ```sh
   launchctl bootout gui/$(id -u)/local.zotero-translate.service
   rm ~/Library/LaunchAgents/local.zotero-translate.service.plist
   rm -r ~/Library/Logs/zotero-translate
   ```
3. 删除数据：`rm -r ~/zotero-translate`（仓库和 `.venv`）、`rm -r "$HOME/Library/Application Support/zotero-translate"`（任务和缓存）、`rm -r ~/.config/zotero-translate`（环境文件和用户配置）、`rm -r ~/.cache/babeldoc`（运行资源）。

插件还在 Zotero 的数据目录里留下 `zotero-translate/attachments.json`（记录哪些附件是译文），不删也没关系。

## 8. 许可证和致谢

- 本项目使用 **GNU AGPL-3.0** 许可证，全文见 [LICENSE](LICENSE)。
- 翻译引擎来自 [BabelDOC](https://github.com/funstory-ai/BabelDOC) v0.6.4（AGPL-3.0），已纳入 `engine/` 并修改。哪些部分改过，见 [engine/MODIFICATIONS.md](engine/MODIFICATIONS.md)；`engine/babeldoc/pdfminer/LICENSE` 保留。
- 测试用的论文页面在 `tests/fixtures/`，来自 CC BY 4.0 许可的 arXiv 论文，署名见 [tests/fixtures/README.md](tests/fixtures/README.md)。
