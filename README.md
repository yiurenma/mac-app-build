# HSBC UK PBB — Mac APP Build Automation (v0.6.0)


## 首选：Web Console（v0.6）

**不需要输入很长的命令**：在 macOS 双击 **`Start APP Builder.command`**，会安全检查新版本（仅 Git checkout 且工作区干净时）、自动安装缺失的 Python 依赖、运行 doctor、启动本地服务并打开浏览器。

- 本地页面：**http://127.0.0.1:8765**（只监听你的 Mac `127.0.0.1`，不部署服务器）。
- 页面填写 Plugin/GMX 源分支、Plugin Version、Country、Project Name，点击 **Start build**。
- 首次还可以在页面填写 **GitHub PAT + Jenkins 用户名/密码**，保存到 **macOS Keychain**。以后表单可留空，后台从 Keychain 读取。环境变量 `GITHUB_TOKEN` 仍可用且优先。
- 页面实时显示 **任务列表、进度阶段、Git/Playwright/Jenkins/Shell 日志、错误截图和恢复操作**；多个任务继续以原有隔离 Worker 并发运行。
- 如果希望从终端启动：`./app-build web`。若同时安全更新：`./app-build web --update`。关闭 Web 页或按 Ctrl+C 不会停止后台 Worker。

[查看 Web Console 完整说明](docs/07-Web-Console本地管理界面.md)。**CLI 保留兼容**，下面全部原有 `run`、`resume`、`doctor` 命令仍可用。

---

在一台 Mac 上**并发运行多个独立**的 Plugin → GMX → Jenkins APP 打包任务。

### v0.6.2 — 一键启动稳定性增强

- 双击 `Start APP Builder.command` 时，若本地 Web Console 已运行，会**直接打开现有页面**，不会重复占用端口。
- GitHub 暂时不可访问或 `config.yaml` 存在本地未提交的修改时，**跳过更新但继续启动 Web Console**；只有对 `run/resume --update` 的显式更新仍遵循原有严格停止策略。
- Web 页面自动显示当前访问的主机和端口，不再固定显示 8765。
- 点击已有任务后，可直接打开 **Plugin Trigger #1、关联 Callback #2、GMX Build** 对应 Jenkins 页面（仅基于本地状态；不访问 Jenkins API）。
- 修复长期刷新任务列表时 SQLite 连接未关闭的问题。
- 上述功能通过离线测试验证；公司 Mac/SSO/Jenkins 的端到端联调仍需在企业网络内进行。


每个任务有单独的 Git 克隆、时间戳、Jenkins 编号、日志和恢复状态。**没有用 Jenkins REST API**；Jenkins 操作由 Playwright 打开浏览器页面模拟人操作。

> **状态**：这是可安装、可离线测试的第一版工程。没有企业内网、GitHub Token、SSO、Jenkins Job 的环境，尚未做真实端到端联调。**不要把本工具视为已经在公司的 Jenkins 上成功运行过。** 首次真实执行建议使用非生产测试分支。浏览器如果无法准确识别内部 Jenkins 参数或产物，会报 `NEEDS_ATTENTION` 并截图；不会盲目点击。


## 设计文档与流程图（已审查三轮）

- [完整需求与验收标准](docs/01-需求规格与验收.md)
- [代码架构与逐项映射](docs/02-设计与代码对照.md)
- [三轮审查与持续离线回归](docs/03-三轮代码审查与自测报告.md)
- [macOS 与企业 Nexus 安装手册](docs/04-Mac与Nexus操作手册.md)
- [内网 Jenkins/SSO 联调清单](docs/05-企业内网联调清单.md)
- **精致版流程图（优先查看）**：[完整构建流程](diagrams/premium/01-完整构建流程-精致版.png) / [Jenkins 自动化状态机](diagrams/premium/02-Jenkins状态机-精致版.png) / [macOS 多任务架构](diagrams/premium/03-macOS多任务架构-精致版.png)。
- 可编辑技术版流程图：[完整流程](diagrams/01-完整流程.svg) / [Jenkins 状态机](diagrams/02-Jenkins状态机.svg) / [多进程架构](diagrams/03-多进程架构.svg)（另有 PNG 及可修改的 `.dot` 源码）。

> **当前 v0.6 已增加独立 Web Console、安全的本地 HTTP API 与离线回归。真实 Nexus/SSO/Plugin/GMX Jenkins 仍需在公司 Mac 和企业网络中现场联调，不能把离线模拟当作端到端成功。**

## 0. 工作流

1. 使用 Plugin 输入源分支创建 `release/<大版本>.<YYYYMMDDHHmm>`，两个 `.xcconfig` 的 `BUNDLE_VERSION_SHORT` 同步设为 `<大版本>.<时间戳>`，精确提交并 Push。
2. 根据规则 `/` 和 `.` → `_` 定位 Plugin Jenkins 新 Job；如果初次只有 `Build`，先触发初始化构建，再等待 `Build with Parameters`。随后使用默认参数触发正式构建。
3. 仅在本次成功 Build 及关联 Callback 的 `build-universal/` artifacts 里查找 DomainModel 和 Journey 的 `*.tar.gz`；要求两个**完整 Artifact Version 一致**。
4. 使用 GMX 输入源分支创建 `upload/auto<同一时间戳>`。在根目录 `mobile-ci.json` 精准修改两个 `paybybank-ios-plugin` 依赖的 `version` 为**完整 Artifact Version**，不做 `1.3.0` 等转换。
5. 在当前任务 GMX 根目录运行 `./bootstrap.sh`；读取 `mobile-ci.lock` 新 SHA256，检查版本、校验和与其他依赖未被意外修改。
6. 只提交 `mobile-ci.json` 和 `mobile-ci.lock`，推送后根据 `upload/xxx` → `upload%2Fxxx` 定位 GMX Jenkins 新 Job。
7. 兼容首次普通 `Build` 初始化，正式执行 `Build with Parameters`：`ENTITIES=国家`（默认 UK）、`SYNC_UPLOAD=true`、`CUSTOM_FOLDER=0_A_<国家>_<项目>`；其他保持 Jenkins 默认（如 BUILD_TYPE=Develop、SIGNING_REPO_BRANCH=master）。
8. 等待 GMX Jenkins 本次 Build 结果（不能依赖 Last Successful）。保存 Build Number、Commit SHA、日志、结果 JSON。

**Jenkins SUCCESS ≠ 一定有已验证的外部上传结果。** 默认最终结果只能证明所关联 GMX Jenkins Build 成功；若需要严格证明上传完成，在 `config.yaml` 填入你们 Console Output 中可靠的 `upload_success_pattern`，检测不到时停止为 `NEEDS_ATTENTION`。

## 1. 安装（Mac / 企业 Nexus）

需要 macOS、Git、Python 3.11+、公司 VPN，以及访问内部 Nexus 的权限。请按公司 IT 政策安装 Python、Git 和 Chrome；这里不自动使用公网 Homebrew、PyPI 或浏览器下载服务。

**为什么要配置两类下载源？** `pip` 安装 `playwright`、`PyYAML` 以及传递依赖；Playwright 浏览器下载则是独立的二进制包，**不会自动使用 pip 的 Nexus 地址**。另外 GMX 的 `./bootstrap.sh` 是打包时运行的公司项目脚本，所需内部制品仓库/认证需要单独联调，不能假设配置 PIP_INDEX_URL 就能解决。

推荐方式：通过公司 Nexus 安装 Python 依赖，使用 IT 已安装的 Chrome。项目 `config.yaml` 默认 `jenkins.browser_channel: "chrome"`，与原版的 Playwright-managed Chromium 不同。

```bash
# 公司已配置好 pip/Nexus 时，不需要再设置 PIP_INDEX_URL
python3 -m pip --version
# 推荐直接运行 ./app-build run ...，它会在需要时自动 install + doctor
```

`install.sh` **不会安装或升级 pip，也不会修改 pip.conf、`PIP_CERT` / `global.cert`**。它只读检查当前有效的 `index-url` 和 `extra-index-url`：两个地址都是 HTTPS 企业 Nexus 时可以同时使用，且保留现有 CA 证书与认证配置。公网 PyPI、HTTP 或陌生主机会触发安全停止，不自动回退公网。非 `.hsbc` 的批准企业域名可经公司 IT 确认后设置 `APP_BUILDER_ALLOWED_PIP_HOSTS`（填准确主机名）。

已安装 pip 不等于项目依赖齐全：`./install.sh` 仍然会在独立 `.venv` 中通过现有 Nexus 安装 `playwright` 和 `PyYAML`。默认使用公司 Chrome，不下载 Playwright Chromium。请勿分享包含凭据的 pip 配置截图或输出。

如果公司没有允许 Playwright 控制的 Chrome，但提供了 Playwright 浏览器二进制镜像，可把 `config.yaml` 的 `jenkins.browser_channel` 改为 `chromium`，然后：

```bash
export PLAYWRIGHT_DOWNLOAD_HOST='https://你的内部Playwright浏览器镜像'
./install.sh
```

也可以用 `PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST`。如果 Chromium 已由 IT 提供到 Playwright 的标准缓存，可通过 `APP_BUILDER_SKIP_BROWSER_INSTALL=1 ./install.sh` 跳过下载；运行前需实际检查浏览器版本和启动是否可用。

> 公司 Chrome 可能受到企业策略限制，无法由 Playwright 控制，或 SSO 会话不允许持久化。账号密码自动填入属于尽力兼容企业登录页面的能力，不保证适用所有 SSO/MFA。

## 2. 设置 GitHub Token（页面 Keychain 或环境变量）

```bash
export GITHUB_TOKEN='你的企业GitHub PAT'
./app-build doctor
```

Web Console 也支持首次填写 GitHub PAT 并保存到 Mac Keychain，因此无需每次在 Terminal 导出变量。如果没有 Token，工具会给出 GitHub 创建网址以及授权说明。建议通过 macOS Keychain 或公司密码管理器管理 Token；**不要把 Token 写到 config.yaml、脚本、Git URL 或项目仓库里**。企业 GitHub PAT 可能需要管理员批准、SSO 授权、Contents read/write。工具在启动前检查变量存在，真正的仓库权限由后续 Git 操作验证。Mac 上 `export` 仅对当前 Shell 及其子进程生效。

## 3. 一条命令启动 / Jenkins 自动登录（v0.5）

**首次运行**，可以直接在 `run` 后面提供 Jenkins 账号密码；工具自动写入当前 Mac 用户的 Keychain，**不会写入** `config.yaml`、任务 SQLite/JSON、日志或 Git：

```bash
./app-build run --plugin-branch develop --plugin-version 1.3 \
  --gmx-branch develop --country UK --project-name PayByBank \
  --jenkins-user '你的公司账号' --jenkins-password '你的公司密码' --live
```

**以后运行**，完全不需要 `login` 或再次输入 Jenkins 账号密码：

```bash
./app-build run --plugin-branch develop --plugin-version 1.3 \
  --gmx-branch develop --country UK --project-name PayByBank --live
```

`./app-build run` / `resume` 将按顺序自动处理：

1. 检查 `.venv` 是否可用，缺失时自动执行 `./install.sh`（通过现有企业 Nexus；不更新 pip）。
2. 自动 `doctor`：Python、Git、Playwright、Chrome、GitHub Token 和 Jenkins 认证状态。
3. 复用两个 Jenkins 的浏览器登录缓存；登录过期时从 Mac Keychain 读取账号密码，尝试重新登录并刷新缓存。
4. 进入 Git / Plugin / GMX / Jenkins 的正式构建流程；`--live` 可见浏览器、实时状态和操作轨迹。

**可选**：传入 `--update` 先执行安全的 `git pull --ff-only`；如果工作树不干净会停止，不覆盖本地文件。它只更新本工具代码，不更改 Plugin 或 GMX 的源分支。

```bash
./app-build run --update --plugin-branch develop --plugin-version 1.3 \
  --gmx-branch develop --country UK --project-name PayByBank --live
```

如果希望**只保存凭据但不开始打包**，也可以使用：

```bash
./app-build auth --jenkins-user '你的公司账号' --jenkins-password '你的公司密码'
```

安全提醒：命令行输入的密码会出现在 Shell 历史及短暂的进程参数中；不会进入工具日志和 Git。如果公司 SSO 要求 MFA、验证码、设备认证，账号密码**不能跳过该验证**，有时仍需在可见 Chrome 中完成一次确认。对于未识别的外部 SSO 登录域名，程序不会填入密码；请先经 IT 确认后将域名加入 `jenkins.auth_allowed_hosts`。原有 `./app-build login --site plugin/gmx` 仍作为手动应急功能保留，但不再是每次运行的前置步骤。

## 4. 运行多个独立 APP

```bash
# 安全预览：只打印计划，不克隆、不修改、不 Push、不触发 Jenkins
./app-build run --plugin-branch develop --plugin-version 1.3 \
  --gmx-branch develop --country UK --project-name PayByBank --dry-run

# 后台独立进程，任务 1
./app-build run --plugin-branch develop --plugin-version 1.3 \
  --gmx-branch develop --country UK --project-name PayByBank

# 后台独立进程，任务 2
./app-build run --plugin-branch develop --plugin-version 1.4 \
  --gmx-branch develop --country HK --project-name PaymentTest

# 看所有任务
./app-build status

# 查看某任务日志（命令会持续跟随）
./app-build logs <TASK_ID> --follow

# 断点恢复（先解决登录/网络/参数问题）
./app-build resume <TASK_ID>
```

也可加 `--wait` 前台执行，或加 `--plugin-url`、`--gmx-url` 覆盖默认仓库地址。

- `--plugin-branch` 和 `--gmx-branch` 是**源分支**，永远不会直接修改。
- `--plugin-version` 是**大版本**，例如 `1.3`。
- `--country` 默认 UK；`--project-name` 必填。项目名称允许字母、数字、`_`、`-`，不允许空格或路径分隔符（避免意外路径）。
- 每个任务只生成一次时间戳。若同一分钟内发起第二个任务，管理器分配下一个可用分钟编码，保证分支唯一（编码可能略晚于实际钟表时间）。
- 默认最多 2 个并发任务，可在 `config.yaml` 改 `build.max_parallel`。macOS 后台任务自动通过 `caffeinate -i` 阻止空闲睡眠（合上 Mac 盖、VPN 断开仍会影响任务）。

## 5. 文件与结果位置

```text
mac-app-builder/
  app-build                    # CLI 入口
  install.sh                   # Mac 安装脚本
  selftest.sh                  # 三轮离线回归脚本
  config.yaml                  # 4 个 GitHub/Jenkins 常量和监控参数
  appbuilder/                  # Python 源码
  tests/                       # 离线测试
  .appbuilder/                 # 运行时自动生成，切勿上传
    auth/                      # 可能含会话信息的敏感文件
    tasks.sqlite              # 任务记录
    runs/<TASK_ID>/
      plugin/                  # 本次独立克隆
      gmx/                     # 本次独立克隆
      state.json               # 已完成步骤与精确 Build Number
      run.log                  # 后台运行日志
      bootstrap.log            # GMX 依赖更新日志
      result.json              # 最终 SUCCESS 时生成
      screenshots/             # 浏览器失败诊断截图，可能含内网信息
```

在公司设备上保存、分享日志和浏览器截图时应遵守公司政策。

## 6. 安全与故障策略

- 永不使用 `git add .` / `git push --force`；不修改输入源分支。
- 提交 Plugin 时只允许两个 `.xcconfig`；GMX 只允许 `mobile-ci.json` 和 `mobile-ci.lock`。提交前后检查 Diff 和真实 Commit 文件；Git Hook 引起额外变化会停下。
- 下载依赖执行 `./bootstrap.sh` 是独立任务目录，命令退出码必须成功；`mobile-ci.lock` 中两个 SHA256 必须为 64 位十六进制，其他包不得改变。
- Jenkins 只操作本次分支 Job，锁定触发后的 Build Number，而不是“最近一次成功”。**同一 Job 出现多个新 Build 时停止，防止拿错别人的结果。**
- 首次普通 Build 失败只有在随后出现参数化构建入口时才视为初始化情形；正式构建的失败不能忽略。
- 如果进程在“点击 Jenkins Build 之后、记录编号之前”意外中断，恢复时先根据保存的触发前编号判断，无法唯一判断时不重复点击而提示人工处理。
- `NEEDS_ATTENTION` 表示需要检查 Jenkins 控件/SSO/分支/产物，`FAILED` 是流程失败，`TIMEOUT` 是超时；原始状态和截图保留在任务目录。
- 若 Jenkins 页面布局特殊，可在 `config.yaml` 配置 `jenkins.gmx_fields` 下的三个 CSS 选择器。必须分别唯一匹配实际控件；未能安全识别会停下。
- **关于 iOS 版本限制：** 需求中 `BUNDLE_VERSION_SHORT=1.3.202610100030` 的第三段是长时间戳，可能不符合 Apple 的公开版本号发布约束；上线前应由项目负责人员确认内部构建/签名流程兼容。

## 7. 离线测试

```bash
./selftest.sh       # 推荐：自动重复三遍并写 docs/verification-logs
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q appbuilder
./app-build run --plugin-branch develop --plugin-version 1.3 \
  --gmx-branch develop --project-name Demo --dry-run
```

> 需要实机联调的位置：Jenkins Bootstrap 入口和构建结果 DOM、GMX 的 `ENTITIES` 实际控件类型、Plugin 下游 Callback 及 Artifact 链路、Mac 上 bootstrap 所需权限与工具。第一版已实现通用识别及明确的失败保护，但这些浏览器场景必须由公司内网 Jenkins 实测确认。


## 实时监控与可视化操作（v0.4.0）

之前默认后台执行任务，`logs --follow` 只会打印阶段日志；Jenkins 页面在后台浏览器里操作，所以新分支创建后界面出现 `No builds` 时无法直接判断是不是卡住。新版增加以下命令：

```bash
# 推荐：后台 Worker 继续执行，当前终端实时观察（Ctrl+C 不会停止任务）
./app-build run --plugin-branch develop --gmx-branch develop \
  --plugin-version 1.3 --country UK --project-name PayByBank --watch

# 演示/调试模式：显示公司 Chrome 的 Jenkins 页面及详细命令轨迹
./app-build run --plugin-branch develop --gmx-branch develop \
  --plugin-version 1.3 --country UK --project-name PayByBank \
  --watch --visible --verbose

# 已存在任务：不改变或重复触发正在运行的 Build，只在终端旁观
./app-build status
./app-build watch TASK_ID
./app-build logs TASK_ID --follow   # 兼容旧命令，现也有阶段等待心跳

# 如果先前 Worker 已退出，再使用新模式恢复（不要杀正在点击 Build 的进程）
./app-build resume TASK_ID --watch --visible --verbose
```

选项说明：

| 选项 | 效果 |
|---|---|
| `--watch` | 实时尾随日志，空闲时每 15 秒打印当前阶段及运行状态；按 Ctrl+C 只退出观察，Worker 继续运行 |
| `--verbose` | 打印 Git 命令和退出码、Jenkins 导航、点击控件、Build Number 等动作；按配置隐藏 Token、密码等凭据 |
| `--visible` | Playwright 使用可见公司 Chrome 窗口操作 Jenkins；多个独立任务各有自己的 Browser Context |
| `--wait` | 完全前台运行并直接显示日志；不能和 `--watch` 同时指定 |
| `watch TASK_ID` | 仅观察已有任务，不会向 Jenkins 发送构建操作 |

* `No builds` 只表示该 Job 暂无 Build 记录；**本次 Build 是否触发，以 Worker 日志中的 `[CLICK]`、`[JENKINS]` 和编号为准**。如长时间停在 `PLUGIN_JENKINS`，先查看 `./app-build status` 和 `./app-build watch TASK_ID`，不要手动重复触发。
* 浏览器登录态保存在本机 `auth/*.json`；详细日志、错误截图均可能包含企业内部页面信息，请勿上传或公开。`--verbose` 不会打印 Git Token，但仍会输出项目地址及 Jenkins Job URL。
* Mac 不在图形桌面登录或 Chrome 未安装时，`--visible` 可能失败；此时保持默认无头模式监控即可。
* 本项目未访问企业 Jenkins 完成端到端验证，首次运行请人工监督。

## 完整现场模式（v0.4.1）

```bash
./app-build run --plugin-branch develop --plugin-version 1.3 \
  --gmx-branch develop --country UK --project-name PayByBank --live
```

`--live` 等于 `--watch --visible --verbose`：Mac 上打开真实 Chrome，终端同步报告每次 Git 命令和结果、Playwright 页面打开/定位/点击/填写、Jenkins 状态轮询，以及 `./bootstrap.sh` 输出。适合首次调试；若只看终端，加 `--watch --verbose` 而不加 `--visible`。操作流水按任务保存在 `.appbuilder/runs/TASK_ID/run.log`，依赖下载输出另外在 `bootstrap.log`。日志尽力隐藏常见凭据，但可能包含企业内部文件名、CI URL 等敏感信息，请勿外传。更多说明见 [实时监控与可见浏览器](docs/06-实时监控与可见浏览器.md)。


## v0.4.2 — Plugin Trigger / Callback 监控修复

**企业 Jenkins 现场发现的实际问题：** 新分支 Job 的第一次正式点击会生成 `#1 Manual Trigger → build will be created soon`；随后 Jenkins 自行生成 `#2 Manual Callback ← Source #1`。旧监控器只轮询 #1 的控制台 `Finished:` 标记及旧版状态图标，可能持续把已经成功的 #1 误判成“运行中”，完全忽略已经开始的 #2。

已修复：

- 同时支持旧版 Jenkins 状态图标与当前 Jenkins 新版构建标题中的状态 SVG。
- 监控 Plugin Trigger 时，轮询 **同一 Job** 的关联 Callback；只有存在明确的 `Manual Callback ← Source #n` 才把监控转到 #n，**不会把任何新编号都当成关联构建**。
- 日志会打印 `[CALLBACK] Plugin Trigger #1 → Callback #2`，之后追踪 #2 的执行结果和 Artifacts。#2 失败就终止，不会继续 GMX。
- **恢复现有任务**：如果已经记录 `plugin_formal_number=1`，会重新检查与 #1 关联的 Callback，继续追踪 #2，**不会再次触发 Plugin Build**。
- 在企业内网无法访问 Jenkins 的情况下只做了离线模拟；真实 Jenkins 页面 HTML/SSO 仍需要现场验证。

如果任务还在旧 Worker 内运行，修改源码不会自动更换运行中的 Python 模块；先结束旧 Worker，确保没有其他同 Task Worker 活动，再运行：

```bash
./app-build resume TASK_ID --live
```

**不要重新执行 `run` 创建新分支，也不要对同一任务手工多次点击 Jenkins Build。** 如 #2 正在运行，恢复后应直接跟踪现有 #2，而不是重新触发 #1。


### Jenkins 新分支 Job 轮询间隔

Plugin / GMX 分支 Push 后，Jenkins 可能需要时间扫描分支并创建 Job。
访问新 Job 暂时返回 `HTTP 404` 属于可预期的等待状态，而不代表构建失败。
默认在 `config.yaml` 设置 `jenkins.discover_poll_seconds: 10`，仅用于等待**新 Job 出现**；
`jenkins.poll_seconds: 45` 仍控制 Build 状态等其他轮询。
日志中的“累计等待 2s”表示已经过了 2 秒，并非每 2 秒轮询。
超出 `discover_timeout_minutes` 后会报告最后 HTTP 状态、检查次数和目标 URL；
不会在 404 页面上尝试点击 Build。


## v0.6.2 Jenkins Callback 已成功但监控不结束

- 访问 `/<build>/consoleText` 后等待控制台真正出现**末行** `Finished: SUCCESS/FAILURE/...`，不会因为页面已触发 `DOMContentLoaded` 就立刻离开。
- 额外等待由 `jenkins.console_settle_seconds` 控制（默认 10 秒）。未加载完时仍会安全检查当前编号 Build 页面头部的结果图标，后续继续重试。
- 兼容 Jenkins 使用 `<svg><use href=".../status/blue.svg#blue"></use></svg>` 而不是 `icon-success` 的构建图标。
- 支持旧任务安全 `Resume`：不重新点击 Build；只继续监控原来已经存在的 Trigger/Callback。
- 如果公司 Jenkins 的自定义状态 HTML 不在受支持的头部结构中，会持续等待并记录诊断信息，不根据其他 Build 的绿标误判。

真实企业 Jenkins/SSO 需要 Mac 内网联调。
