# zotero-translate：帮用户装好、配好、用起来

用户在 Zotero 里提交一篇英文论文，本机的翻译服务调用用户配置的模型端点，取回中文译文 PDF 和双语对照 PDF，挂回原条目。你的任务是在用户的 Mac 上把这套东西装好并验证。这份说明里的步骤按顺序做；README.md 是给用户看的完整说明，下文用“README 第 N 节”指它。

**完成**的标志，缺一不可：

- `http://127.0.0.1:8920/health` 有回应，服务由 LaunchAgent 管理；
- 插件的 xpi 已构建，用户确认已在 Zotero 里装好，设置页“中文翻译”显示“翻译服务在运行”；
- 用户同意并看到一篇论文翻译成功（冒烟）。

## 密钥规则

密钥只在用户自己写的环境文件里。

- 让用户在自己的编辑器里填环境文件。用户把密钥贴进了对话，就告诉用户这个密钥已经暴露，建议换一个，并且不把它写进任何文件。
- 检查环境文件时，只看键名和权限（步骤 3 的命令），不 `cat`、不打印、不复制这些文件，也不读 `~/.config/zotero-translate/profiles/` 下的文件。
- 命令行、仓库文件、日志、给用户的回复里不出现密钥的值。

## 其他边界

- 在用户同意之后，才写 `~/Library/LaunchAgents/`、才提交论文。提交论文会消耗用户的模型额度。
- Zotero 里的点击操作（装插件、设置页、右键菜单）由用户做；你告诉用户点哪里，等用户回报结果。
- 8920 端口上已经运行的服务属于用户。你另起的服务一律用新的 `--data-dir`、`--profiles-dir` 和别的端口。

## 步骤

### 1. 核对前提

逐条运行，把结果列给用户。有一条不通过，就停下来说明是哪一条、命令输出是什么，由用户决定怎么办，不继续安装。

| 项目 | 命令 | 通过条件 |
|---|---|---|
| 芯片 | `uname -m` | `arm64` |
| macOS | `sw_vers -productVersion` | 14 或更高 |
| git | `git --version` | 有版本号 |
| uv | `uv --version` | 有版本号。没有就征得用户同意后安装：`curl -LsSf https://astral.sh/uv/install.sh \| sh`（装到 `~/.local/bin`；新开的 shell 里找不到时，每条命令前加 `export PATH="$HOME/.local/bin:$PATH"`） |
| Zotero | `/usr/libexec/PlistBuddy -c "Print CFBundleShortVersionString" /Applications/Zotero.app/Contents/Info.plist` | 主版本 7、8、9 或 10。Zotero 不在 `/Applications` 时，用 `mdfind "kMDItemCFBundleIdentifier == 'org.zotero.zotero'"` 找 |
| 磁盘 | `df -h ~` | 可用 10 GB 以上 |
| 端口 | `lsof -nP -iTCP:8920 -sTCP:LISTEN` | 没有输出。有输出时，`curl -s http://127.0.0.1:8920/health` 返回 `"service": "zotero-translate"` 的话，用户已经装过：改读 README 第 7 节的“更新”；否则是别的程序占着，告诉用户 |

仓库应在 `~/zotero-translate` 一类的家目录位置，不在“文稿”“桌面”“下载”或 iCloud 云盘（登录时由系统启动的服务可能读不了那里）。以下命令都在仓库根目录运行。

完成：六项全部通过，结果已列给用户。

### 2. 建环境，下载运行资源

```sh
uv sync --frozen
.venv/bin/babeldoc --warmup
```

要用 `--frozen`，免得 uv 改写 `uv.lock`。完成：

- `.venv/bin/zotero-translate --help` 列出 `serve` 和 `run`；
- `--warmup` 以退出码 0 结束，最后一条日志是 `Warmup completed, exiting...`。

`--warmup` 下载不了时（网络），需要能访问 README 第 2 节“网络”一行列出的主机；也可以让用户把另一台 Mac 的 `~/.cache/babeldoc` 整个拷来，再运行一次做校验。

### 3. 环境文件

服务需要 `~/.config/zotero-translate/model.env` 和 `argon.env` 两个文件，少一个就启动即退出。键的含义、单端点和双端点的写法，读 README 第 3.3 节；问用户有几个端点、模型 ID 是什么，再告诉用户每个文件写哪些键。

1. 建好空文件并收紧权限：

   ```sh
   mkdir -p ~/.config/zotero-translate
   touch ~/.config/zotero-translate/model.env ~/.config/zotero-translate/argon.env
   chmod 600 ~/.config/zotero-translate/model.env ~/.config/zotero-translate/argon.env
   ```

2. 请用户在编辑器里填写，等用户说写好了。

3. 核对键名和权限，命令只输出键名和权限：

   ```sh
   grep -oE '^[[:space:]]*(export[[:space:]]+)?[A-Za-z_][A-Za-z0-9_]*[[:space:]]*=' ~/.config/zotero-translate/model.env ~/.config/zotero-translate/argon.env
   stat -f '%Lp %N' ~/.config/zotero-translate/model.env ~/.config/zotero-translate/argon.env
   ```

完成：两个文件都列出 `OPENAI_BASE_URL=` 和 `OPENAI_API_KEY=`，建议再有 `AI_DEFAULT_MODEL=`；两个文件的权限都是 `600`。

### 4. 手动试启动

把整段**作为一条命令在同一个 shell 里**运行：它在后台启动服务，最多等 30 秒直到 `/health` 有回应，打印回应和模型配置，最后结束服务。拆开运行，后台进程可能在 `curl` 之前就随 shell 结束，或者留下来占住端口。

```sh
.venv/bin/zotero-translate serve > /tmp/zotero-translate-trial.log 2>&1 &
TRIAL=$!
for i in $(seq 1 30); do
  curl -sf http://127.0.0.1:8920/health && break
  sleep 1
done
echo
curl -s http://127.0.0.1:8920/profiles
echo
cat /tmp/zotero-translate-trial.log
kill $TRIAL
wait $TRIAL 2>/dev/null
```

完成：

- 第一行是 `{"service": "zotero-translate", "version": "…"}`；
- `/profiles` 的回答里 `"current": "gemini"`，`model` 和 `base_url` 是用户写的（回答里没有密钥）；
- 日志只有一行 `{"url": "http://127.0.0.1:8920"}`；
- 之后 `lsof -nP -iTCP:8920 -sTCP:LISTEN` 没有输出。

整条命令的退出码是 143，这是预期值：最后的 `wait` 返回服务被结束的状态。以上面的输出为准，不看退出码。

### 5. 开机自动启动

先告诉用户要写 `~/Library/LaunchAgents/local.zotero-translate.service.plist` 并用 `launchctl` 载入，得到同意。plist 的生成命令在 README 第 3.5 节：读那一节，在仓库根目录运行其中的整段（它用 `$PWD` 和 `$HOME` 写出绝对路径）。然后：

```sh
plutil -lint ~/Library/LaunchAgents/local.zotero-translate.service.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.zotero-translate.service.plist
sleep 3
launchctl print gui/$(id -u)/local.zotero-translate.service | grep -E 'state|pid|last exit'
curl -s http://127.0.0.1:8920/health
```

完成：`plutil` 打印 `OK`，`state = running`，`/health` 有回应。`state` 不是 running 时，看 `~/Library/Logs/zotero-translate/service.log` 的最后几行，原因通常是步骤 3 的环境文件，或端口被占。

重启用 `launchctl kickstart -k gui/$(id -u)/local.zotero-translate.service`。改了 plist 要先 `bootout` 再 `bootstrap`。停服务用 `bootout`，`kill` 没用（`KeepAlive` 会拉起来）。

### 6. 插件

```sh
.venv/bin/python plugin/build.py
```

命令打印 xpi 的路径（`plugin/dist/zotero-translate.xpi`）。把路径告诉用户，请用户在 Zotero 里点：“工具 → 插件”，右上角齿轮，“从文件安装插件…”，选这个 xpi。再请用户打开 Zotero 设置里的“中文翻译”页，告诉你最上方那行写的是什么。

完成：xpi 文件存在，用户回报那一行是“翻译服务在运行，当前配置：…”。回报是“连不上翻译服务”时，回到步骤 5 查服务。

### 7. 冒烟

先问用户同意，并说明：一篇 22–31 页的论文约 270–450 次模型请求，端点正常时 5–9 分钟，会消耗用户的额度。建议挑一篇较短的论文。有两种做法：

- **经 Zotero（建议，走完整链路）：** 请用户选一篇带 PDF 的论文，右键“翻译为中文”。完成时右下角提示“翻译完成”，原条目下多出“中文译文”和“双语对照”两个附件；任务窗口里状态是“已挂回”。
- **命令行：** 不经过 Zotero，只验证服务端的翻译。选项以 `.venv/bin/zotero-translate run --help` 为准：

  ```sh
  .venv/bin/zotero-translate run <PDF> --output-dir ~/zotero-translate-smoke \
    --env-file ~/.config/zotero-translate/model.env \
    --argon-env-file ~/.config/zotero-translate/argon.env
  .venv/bin/python -c 'import json, sys; r = json.load(open(sys.argv[1])); print(r["state"], r["error"])' ~/zotero-translate-smoke/report.json
  ```

  退出码 0 成功，1 论文任务失败，2 环境文件缺失或不全。

完成：

- 经 Zotero：用户确认两个附件已挂回，并能打开；
- 命令行：退出码 0，上面最后一条命令打印 `succeeded None`，`~/zotero-translate-smoke/` 里有 `mono.pdf` 和 `dual.pdf`。

失败时停下，把 `report.json` 的 `error` 和 `failed_batches`、任务窗口里的失败原因告诉用户，再对照下表。服务里的任务，报告在 `~/Library/Application Support/zotero-translate/tasks/<任务号>/report.json`，日志在同目录的 `task.log`。

## 排错

用户在 Zotero 里看到的提示（任务窗口、设置页的“测试连接”）：README 第 6 节有现象和做法的对照表。装机时你会遇到的：

| 现象 | 看哪里 | 怎么办 |
|---|---|---|
| `uv sync --frozen` 失败 | 命令的完整输出 | 锁定的 `onnxruntime` 在 macOS 上只有 macOS 14 以上 Apple 芯片的安装包：回到步骤 1 核对芯片和系统；网络问题让用户检查网络。把完整输出给用户 |
| `serve` 退出码 2 | 终端里的一句话 | `找不到…环境文件：<路径>` 或 `环境文件 <路径> 缺少：<键名>`：回到步骤 3 |
| `serve` 退出码 1 | 终端里的一句话 | `端口 8920 已被占用`：`lsof -nP -iTCP:8920 -sTCP:LISTEN` 看是谁。`已有翻译服务在使用数据目录`：已经有一个服务在跑 |
| 30 秒内 `/health` 没有回应 | 试启动日志 | 日志里就是原因 |
| `service.log` 每 10 秒出现同一条启动错误 | `~/Library/Logs/zotero-translate/service.log` | 环境文件缺失或缺键，或端口被占，或另一个服务锁着数据目录。launchd 启动失败时约每 10 秒重试一次 |
| 经 Zotero 提交后任务失败，原因含 `HTTP 401` 或 `403` | 任务的 `report.json` | 密钥错误或无权限，不会自动重跑。请用户在编辑器里改环境文件的密钥，`kickstart` 重启服务，再在任务窗口点“重试” |
| 失败原因含 `HTTP 429` 或“超时” | 任务的 `report.json` | 限流或端点慢，服务已自动重跑过两次。降低并发（`serve --concurrency 8`，写进 plist），或加大 `ZT_REQUEST_TIMEOUT_SECONDS`；细节见 README 第 6 节 |

## 用户要改代码时

- 开发环境用 `uv sync`（不带 `--frozen`）。代码在 `src/zotero_translate/`，引擎是纳入本仓库的 BabelDOC 源码，在 `engine/`，直接改源码，并同步更新 `engine/MODIFICATIONS.md`（AGPL-3.0 要求显著标明修改）。
- 测试：
  - Python 用 `.venv/bin/pytest`。先只跑改动涉及的测试文件，最后再跑完整套件：`.venv/bin/pytest -n auto`，默认 3 个进程，峰值内存约 5–6 GB，开始前确认可用内存；`-n <N>` 改进程数。每个测试文件在一个进程里跑完，用例要能并行；
  - 新用例用 `tests/conftest.py` 的夹具（`start_service`、`tmp_path`）：端口自动选空闲的，数据目录和环境文件在用例自己的临时目录里。用例写进数据目录的任务，任务号用 `uuid.uuid4().hex`；
  - 插件的纯逻辑用 `node --test plugin/test/`；
  - `tests/test_real_model_smoke.py` 默认跳过，设 `ZT_REAL_MODEL_SMOKE=1` 才调用真实模型，消耗额度，先问用户。
- 构建插件：`.venv/bin/python plugin/build.py`。
- 开发时另起服务，不碰用户正在用的：

  ```sh
  .venv/bin/zotero-translate serve --port 8941 --data-dir <临时目录> --profiles-dir <临时目录>/profiles \
    --env-file <假的 model.env> --argon-env-file <假的 argon.env>
  ```

  假环境文件里写假密钥，端点指向本机一个关闭的端口：启动服务不调用模型，也不动用户的密钥。一个数据目录只能有一个服务（`<数据目录>/service.lock`）。
- 环境变量名以 `ZT_TEST_` 开头的，只供测试，生产中不要设。
- 改了接口、命令或界面文字，同步改 README.md 里对应的说明。
