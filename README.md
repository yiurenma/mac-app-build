# HSBC UK PBB — Mac APP Build Automation (v0.8.0)

## v0.8.0 — 共用构建表单与定时任务

New build 默认只展开 Plugin 来源分支/版本与 GMX 来源分支/实体/项目。更多 Plugin、更多 GMX、共用高级设置分别折叠；所有输入均有中文名称和行内说明。来源分支支持 GitHub 全部分页加载、输入搜索、刷新、仓库变化重载及手输兜底；Token 只由 Mac 服务端读取。

开启「保存为定时任务」后，在同一表单填写名称与 cron；保存、编辑、取消和立即构建共用字段、默认值与校验。所有高级参数及工单编号完整保存，凭据不进入计划。Chrome 显示默认开启，定时和手动一致，可在共用高级设置关闭。Schedules 提供调度器状态、五次预览入口、启停、立即运行、编辑、最近 20 次记录与构建日志链接。默认最多 10 条完整 Plugin→GMX 流水线，手动、定时、Resume 共用名额。保留 25 个实体、AUTO 分支 fallback、历史数据与 Resume。

参见 [定时任务操作说明](docs/24-定时任务.md) 和 [v0.8.0 交付与验证](docs/25-v0.8.0-交付与验证.md)。代码更新不会自动安装 Mac 后台服务。

## v0.7.9 — 完整 Country / Entity 选项

正式构建和 GMX 构建测试的下拉框均按 Jenkins 截图提供：AE, AU, BM, CIOM, CN, EG, EX, HK, ID, IN, LK, MO, MSB, MT, MX, MY, PH, QA, SG, TW, UK, US, VN, NOT_SET, ALL。默认 UK；NOT_SET 和 ALL 为 Jenkins 特殊值。

## v0.7.8 — GMX 分支命名修复

GMX 自动分支现在为 `upload/<工单编号>-auto<时间戳>`。默认从 GMX Source Branch 提取唯一的大写工单编号；源分支不含编号时，默认生成 `upload/AUTO-<12位时间戳>`（自动标识，不代表真实工单）。可选填页面 **GMX Issue Key**、CLI `--gmx-issue-key` 或配置 `build.gmx_issue_key` 覆盖。显式填写的编号格式错误或源分支存在多个编号时会提前停止。

已因 `upload/auto...` 被拒绝的旧任务：保留 `.appbuilder` 数据，更新程序后点击 **Resume**。程序在确认旧、新目标分支均未发布后，自动修改本地分支名和任务 Jenkins URL，保留现有提交、Plugin 构建和已完成阶段。源分支不含编号的旧任务会自动改为 `upload/AUTO-<原时间戳>`，无需补填工单。已推送或已构建的旧目标不会自动改名。不会 force-push。

## v0.7.6 — 完整日志读取与提取诊断

Plugin 单元测试从指定 Build 下载完整 consoleText（默认 300 秒下载超时，可通过 jenkins.console_download_timeout_seconds 配置为 30–1800 秒），输出请求 URL、HTTP 状态、响应类型、完整响应字节数/行数、Nexus 分组数及 JSON 条目数。按行顺序扫描完整日志，避免为无关日志创建整份标准化副本或行列表，只缓存目标 JSON 段；不会截断到最后几千字；图标已确认当前 Build SUCCESS 时，缺少精确 Finished 末行不会阻止完整日志搜索与版本/SHA 比对。终态支持 Jenkins 时间戳和 ANSI 颜色；明确失败、状态冲突、HTML 登录响应及未完成分段日志仍不能通过。

正式流程同样读取完整日志；只有本次 Artifact Build 已独立确认成功，才允许在缺少末行标记时采用其 Nexus 数据。现场截图的确切日志格式仍待复测，未声称已经读取公司日志。

验证：210 项当前环境可运行测试全部通过；100.5 MB 模拟日志的全量扫描与两个 SHA 提取用时约 0.44 秒（不含网络下载）。三组 Chromium DOM 测试因环境缺少浏览器未运行。下载仍需要容纳完整响应，扫描不再反复复制整份日志；公司现场结果待复测。

## v0.7.5 — 从 Nexus 上传日志直接更新 lock

正式流程不再运行本地 `ci-script --upgrade` 或 `./bootstrap.sh`。从当前 Plugin 成功 Build（或已关联的 Artifact Callback）的完整 `consoleText` 中解析 `Nexus upload output` JSON，按两个插件名、完整版本与 `.tar.gz` 文件名读取各自 `artifactSHA256`。忽略 `.sha256`、`.metadata.json` 等附件；缺失、版本不符、哈希冲突或日志未成功结束时停止。

直接精准更新 `mobile-ci.lock` 的两个版本和 SHA256，再提交并 Push `mobile-ci.json` 与 `mobile-ci.lock`。Push 前验证 Commit 内的 SHA256 与当前日志完全一致。独立 Bootstrap Test 的页面和后台执行入口已移除。

现有 Plugin Jenkins URL Test 保留图标状态判断，另行读取同一指定 Build 的完整日志，显示两个插件各自的版本和 SHA256，并保存到诊断结果 JSON。Plugin 测试表单新增三个必填输入：完整版本号、Journey SHA256、DomainModel SHA256。逐项显示输入值、提取实际值与匹配结果；只有构建状态符合预期且三个值都一致，整体测试才 PASS。构建状态与 Nexus 提取结果仍分开报告。

验证：新增 9 项 Nexus/lock/诊断比对测试；移除 Bootstrap 测试后，当前环境可运行的 207 项离线测试全部通过，Python 编译和 shell 语法检查通过。三组真实 Chromium DOM 测试因本环境缺少 `/usr/bin/chromium` 未执行；公司 Jenkins 仍需现场复测。未将现场日志或截图写入仓库。

## v0.7.4 — 修正 ci-script 升级参数

正式流程和 Bootstrap Test 使用 `ci-script --upgrade`（两个 ASCII 横杠），成功后执行 `./bootstrap.sh`。同时处理十进程首次初始化 SQLite WAL 模式时的锁竞争：仅对 BUSY/LOCKED 错误有限重试，其他错误立即抛出。原 v0.7.3 的 `ci-script upgrade` 会被公司 CLI 当成名为 upgrade 的脚本，现场报未声明；本版修正参数，保留失败停止和现有隔离校验。离线测试现在明确拒绝不带横杠的参数；公司 CLI 的实际升级仍须现场验证。

## v0.7.3 — Bootstrap 前自动升级 ci-script

正式 GMX 流程与隔离 Bootstrap Test 在原有 Bootstrap 执行段中依次执行 `ci-script upgrade`、`./bootstrap.sh`，使用同一 GMX 工作目录。升级失败立即停止；两个命令的输出进入原有日志，不增加界面操作或独立流程步骤。保留完整 Artifact 版本、lock 校验和测试不提交、不推送、不触发 Jenkins 的边界。

## v0.7.2 — GMX 复选框标签遮挡修复

当 Jenkins 的 `label.attach-previous` 覆盖复选框时，先尝试正常勾选；点击超时后仅使用该输入唯一关联的可见标签，再回读确认状态。覆盖 `SYNC_UPLOAD`、`SKIP_TEST`、`FORCE`、`KEEP_ALIVE`；已符合输入的状态不重复切换。禁用控件、无唯一标签或回读不一致时停止，不点击 Build。

保留 v0.7.1 的完整 Artifact 版本输入、Bootstrap 隔离测试，以及 GMX 正式 Build 创建编号后结束测试的边界。Bootstrap 中公司 `ci-script` 依赖安装返回 202 / 脚本退出 71 的问题尚需完整现场日志，本次未更改依赖安装逻辑。

## v0.7.1 — GMX 现场测试精准停在指定步骤

- **Bootstrap Test**：输入 GMX 分支及完整 **Plugin Artifact Version**（例如 `1.3.1-alpha.1-SNAPSHOT.2`）；只在隔离 checkout 修改 `mobile-ci.json` 的 DomainModel/Journey 两个依赖，执行 `./bootstrap.sh`，核对 `mobile-ci.lock` 两个版本及 SHA256，并阻止非预期文件修改。**不 Commit，不 Push，不启动 Jenkins**。
- **GMX Jenkins Build Test**：如有必要先执行首次普通 Build 以初始化 Job，待 `Build with Parameters` 出现后填写参数并触发第二次 Build；**检测到正式 Build Number 后立即结束测试**，输出编号与 Jenkins URL，不等 Jenkins SUCCESS，不验证上传。真正的 Jenkins 构建仍会在服务端继续执行。
- 之前两个 Plugin/GMX **只读 Jenkins URL Test** 保留，适合在第二次 Build 最终完成后再检查 SUCCESS。
- 整包替换更新，无需 Git Pull。旧任务数据可从旧目录的 `.appbuilder` 用 `Import Previous Data.command` 导入新目录。

## v0.6.9 — GMX URL 复制兼容和初始化说明

**现场诊断 URL**：允许从公司 Jenkins 浏览器复制的 GMX 分支编码 `%2F` 或 `%252F`，保留原样访问，不做有风险的自动解码；严格拒绝其他百分号编码。Web Console 和 `diagnose-jenkins` 均适用，输入已有 GMX Build #2 URL 时不会触发任何新 Build。

**GMX 实际正式打包**：新 Job 初始只有 `Build` → 必须点击第一次 Build（可因参数未初始化而 Disabled/Skipped）→ 等待 Jenkins 出现 `Build with Parameters` → 填写并确认参数 → 启动正式 Build #2 → 检查当前编号的结果及上传状态。只读 URL 测试不能代替前面的初始化。

## v0.6.8 — GMX 参数化运行模式适配

GMX Jenkins 与 Plugin 不同：首次 #1 可显示 `Build disabled: ENTITIES or BUILD_TYPE is empty`，这属于参数尚未生效的初始化，不要求成功；正式构建通过 **Build with Parameters** 触发，例如 #2 成功结束；**没有发现 Plugin 那种 Manual Trigger → Manual Callback 结构，GMX 只监控本次正式 Build**。

GMX 参数页按现场截图新增 `SKIP_TEST`（默认 true）、`FORCE`（默认 true）、`KEEP_ALIVE`（默认 false）。工具仍按原业务要求显式设置 `SYNC_UPLOAD=true`（截图中 Jenkins 原生默认 false）；如果用户主动关闭，将标记下游上传尚未独立验证，不能把当前 Job SUCCESS 当成 Upload SUCCESS。所有参数都在构建前回读校验。详见 [GMX 适配说明](docs/16-v0.6.8-GMX参数化构建适配.md)。

## v0.6.7 — Plugin / GMX URL 状态测试增强

针对现场 `Build History IDs=[1,2]`，但绿色完成图标无法识别的情况：现在先精确查找指定编号的构建链接，再在相邻记录读取图标语义、SVG 与有限的可见图标 CSS 颜色；若仍不确定会打印 `[DOM]` 和 `[ICON]` 证据、检查 `consoleText` 类型并保留失败现场截图。**无需重新运行 Jenkins Build**。详见 [Jenkins 构建状态现场诊断改进](docs/15-v0.6.7-Jenkins构建状态现场诊断改进.md)。

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

### v0.6.4 — 一键启动稳定性增强

- 双击 `Start APP Builder.command` 时，若本地 Web Console 已运行，会**直接打开现有页面**，不会重复占用端口。
- GitHub 暂时不可访问或 `config.yaml` 存在本地未提交的修改时，**跳过更新但继续启动 Web Console**；只有对 `run/resume --update` 的显式更新仍遵循原有严格停止策略。
- Web 页面自动显示当前访问的主机和端口，不再固定显示 8765。
- 点击已有任务后，可直接打开 **Plugin Trigger #1、关联 Callback #2、GMX Build** 对应 Jenkins 页面（仅基于本地状态；不访问 Jenkins API）。
- 修复长期刷新任务列表时 SQLite 连接未关闭的问题。
- 上述功能通过离线测试验证；公司 Mac/SSO/Jenkins 的端到端联调仍需在企业网络内进行。


每个任务有单独的 Git 克隆、时间戳、Jenkins 编号、日志和恢复状态。构建提交与结果监控由 Playwright 操作页面；仅新增精确队列号的只读 JSON 查询，沿用已有登录会话，不新增凭据或调用写入 API。

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

## v0.6.4 — 页面可编辑的默认输入参数

**New build** 的五个常用字段保留不变。展开 **高级参数 & Jenkins Defaults** 即可查看、修改：

- Plugin 和 GMX 的 GitHub 仓库 URL、Jenkins 分支 Job 的父级 URL、Chrome/Edge/Chromium 浏览器模式。
- GMX `BUILD_TYPE=Develop`、`SIGNING_REPO_BRANCH=master`、`ENTITIES=Country`、`SYNC_UPLOAD=true`、`CUSTOM_FOLDER=0_A_<Country>_<Project>`。提交时 **五项均会填入 Playwright 控制的实际 Jenkins 参数表单并验证**；字段不存在会报告明确错误而不会盲目点击。Plugin Jenkins 仍保持自己的默认参数。
- 新 Job 轮询 10 秒、构建状态轮询 45 秒、Console 终态等待 10 秒、发现/构建/Callback 超时、提交变更限制、可选上传成功日志正则及自定义 Jenkins CSS 字段选择器。
- 最大并发任务显示为只读，因为它是当前 Web Console 的全局配额，仍由 `config.yaml` 控制。

页面从 `config.yaml` 读取默认值，并展示自动派生的 Plugin/GMX 分支命名和 GMX 自定义目录。每个任务的实际设置写入独立的任务状态，**不会覆盖公司公用 YAML 配置，也不会影响其他正在并发运行的任务**。已有任务的 Resume 保持原任务参数。GitHub Token、Jenkins 密码只经 Mac Keychain 管理，不写入任务数据库。

更多说明见 [v0.6.4 Web 参数配置手册](docs/11-v0.6.4-Web参数补齐与验证.md)。

## 定时任务

在 **New build** 开启「保存为定时任务」，用同一表单输入 cron；**Schedules** 管理已有计划。启动 Web 会自动检查并启动独立调度器，关闭 Web 后继续运行，无需额外命令。已有实例会复用；按需手动 `./app-build scheduler install` 后在登录 Mac 时自动运行。错过的时间只补跑最近一次，并发已满时排队。详见 [定时任务说明](docs/24-定时任务.md)。

## 0. 工作流

1. Plugin 输入 `1.3` 时生成 `1.3.<YYYYMMDDHHmm>`，输入 `1.3.1` 时保持固定版本；新目标为 `release/v<版本>`。目标已存在时从真实远端目标合并本任务冻结的来源 SHA，再精确修改两个 `.xcconfig` 的 `BUNDLE_VERSION_SHORT`。实际版本不带 `v`；冲突保留现场并停止，不强推。同版本且没有内容变化时不创建空提交。来源 SHA 与最终构建 SHA 分别保存。
2. 根据规则 `/` 和 `.` → `_` 定位 Plugin Jenkins 新 Job；如果初次只有 `Build`，先触发初始化构建，再等待 `Build with Parameters`。随后使用默认参数触发正式构建。
3. 从本次成功 Build（或其关联 Callback）完整 `consoleText` 的 Nexus upload output JSON 中，按 DomainModel 和 Journey 的 `*.tar.gz` 文件名读取**完整 Artifact Version**，要求两个版本一致。不再打开 Jenkins 的 Artifacts 页面（其 `*view*`/`*fingerprint*` 链接会让大 `source.zip` 解压预览超时）。
4. 使用 GMX 输入源分支创建 `upload/<工单编号>-auto<同一时间戳>`。在根目录 `mobile-ci.json` 精准修改两个 `paybybank-ios-plugin` 依赖的 `version` 为**完整 Artifact Version**，不做 `1.3.0` 等转换。
5. 读取本次 Plugin 成功构建的 Nexus upload output JSON，按插件名与完整版本提取两个 `.tar.gz` 的 `artifactSHA256`；直接更新 `mobile-ci.lock` 对应版本和 SHA256，检查其他依赖未变。无需运行本地 Bootstrap。
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
- `--plugin-version` 支持两段版本（如 `1.3`，自动追加时间戳）或三段固定版本（如 `1.3.1`）。
- `--country` 默认 UK；`--project-name` 必填。项目名称允许字母、数字、`_`、`-`，不允许空格或路径分隔符（避免意外路径）。
- 每个任务只生成一次时间戳。若同一分钟内发起第二个任务，管理器分配下一个可用分钟编码，保证任务及 GMX 自动分支唯一（编码可能略晚于实际钟表时间）。固定版本的 Plugin 目标可以重复使用；本机同仓库、同目标任务使用互斥锁，锁不代表对其他机器或人工构建的归属证明。
- 默认最多 10 条完整 Plugin→GMX 流水线，手动、定时和 Resume 共用；可在 `config.yaml` 降低 `build.max_parallel`，最高 10。macOS 后台任务自动通过 `caffeinate -i` 阻止空闲睡眠（合上 Mac 盖、VPN 断开仍会影响任务）。

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


## v0.6.4 Jenkins Callback 已成功但监控不结束

- 访问 `/<build>/consoleText` 后等待控制台真正出现**末行** `Finished: SUCCESS/FAILURE/...`，不会因为页面已触发 `DOMContentLoaded` 就立刻离开。
- 额外等待由 `jenkins.console_settle_seconds` 控制（默认 10 秒）。未加载完时仍会安全检查当前编号 Build 页面头部的结果图标，后续继续重试。
- 兼容 Jenkins 使用 `<svg><use href=".../status/blue.svg#blue"></use></svg>` 而不是 `icon-success` 的构建图标。
- 支持旧任务安全 `Resume`：不重新点击 Build；只继续监控原来已经存在的 Trigger/Callback。
- 如果公司 Jenkins 的自定义状态 HTML 不在受支持的头部结构中，会持续等待并记录诊断信息，不根据其他 Build 的绿标误判。

真实企业 Jenkins/SSO 需要 Mac 内网联调。


## v0.6.4 — Jenkins 构建结果快速确认（Plugin / GMX 共用）

- Jenkins Build 完成后无需等待 Chrome 渲染 Console Output。由 Playwright 已登录浏览器上下文的 `context.request.get()` 直接读取当前 Build 的 `/consoleText` 原始 HTTP 响应（不是 Jenkins REST API）。
- 默认每 **5 秒**检查 `Finished: SUCCESS/FAILURE/ABORTED/UNSTABLE/NOT_BUILT` 最后一行；已完成的 Build 通常在下一次轮询即可识别，不代表真实构建耗时缩短。
- `poll_seconds=45` 只作为 Job 页面状态与 Callback 备用检测；新 `result_poll_seconds=5` 单独配置，也可在 Web Console 高级参数中修改。
- 登录重定向、403、响应未完整等均不当作成功；保留当前 Build 状态图标备用识别和任务断点恢复。
- 旧 Worker 必须退出，新 Worker 才能加载升级后的检查逻辑。

详细说明见 [快速结果检测设计与验收](docs/12-v0.6.4-快速结果检测.md)。


## v0.6.5 — Jenkins Build History 状态勾快速确认

优先访问 Jenkins Job 主页，精确匹配当前 Build Number（如 #2）左侧的状态图标。
只读取该编号的 SVG / img / status 元数据，不使用其他历史构建的状态，
随后读取当前编号 Build 页面标题作为第二来源；两者都无法明确判断时，
才回退到原始 `consoleText` 的 `Finished:` 行。所有检查使用已有登录会话和 Playwright，
无需滚动长日志页面。默认每 5 秒重试。

> 这是 DOM 检测，尚未验证公司实际 Jenkins HTML；有些 Jenkins 主题的
> 图标标记可能不同。无明确结果不猜测成功，也不会再触发已经创建的构建。


## v0.6.6 — Plugin / GMX Jenkins **真实浏览器单元测试**

[现场单元测试使用手册](docs/14-v0.6.6-Plugin-GMX-Jenkins现场单元测试.md)


进入 Web Console 左侧 **测试工具 → Jenkins 构建检查**。这里提供两个**独立测试入口**：

- **Plugin Jenkins · 单元测试**：粘贴已有 Plugin 分支 Job URL 或 `/2/` Callback Build URL，指定 Build Number（例如 2）和预期 SUCCESS，点击“打开 Chrome · 测试 Plugin”。
- **GMX Jenkins · 单元测试**：粘贴已有 GMX 分支 Job URL 或 `/2/` Build URL，指定 Build Number（例如 2）和预期 SUCCESS，点击“打开 Chrome · 测试 GMX”。

两种测试都自动启动**可见的 Chrome**；如果出现公司 Jenkins SSO/MFA 登录页面，你可以直接**在弹出的 Chrome 手工输入账号密码**，不需要提前单独运行 `login`，最多等待 180 秒。测试复用正式 Worker 的 `_history_result()`（精确 Build # 对应绿色勾）和 `_heading_result()`（构建页状态图标），必要时才尝试一次短超时 `consoleText`。页面会每秒刷新真正的诊断过程，结束时打印 HTTP、Build Number、历史状态、标题状态、`PASS/FAIL/UNKNOWN`、预期结果和建议。

**只读**：不点击 Build / Build with Parameters、不 clone/commit/push、不创建新任务，不更改原有任务。`PASS` 仅代表构建状态识别符合预期，不能证明 Plugin Artifacts 或 GMX 上传校验已经完成。

CLI 入口同样会启动可见 Chrome 并输出诊断日志：

```bash
./app-build diagnose-jenkins 'https://<公司Plugin-Jenkins>/job/<branch>/2/' --expected SUCCESS
./app-build diagnose-jenkins 'https://<公司GMX-Jenkins>/job/<branch>/2/' --expected SUCCESS
```

URL 必须属于 `config.yaml` 中配置的 Plugin 或 GMX Jenkins 父 Job；不会访问任意外部 URL。历史记录和截图按 site 保存到 `.appbuilder/diagnostics/plugin/` 或 `.appbuilder/diagnostics/gmx/`。

### 不使用 git pull 的安装方法

直接下载新的 **完整 ZIP**，解压到一个**全新目录**，不要直接覆盖或删除旧目录里的 `.appbuilder`。建议先停止旧 Web Console 和 Worker。**先解压完整新 ZIP 到不同文件夹**，在新文件夹双击 `Import Previous Data.command` 并通过 Finder 选择旧版本文件夹，自动、安全地复制旧 `.appbuilder/`（任务、Build 编号、SSO 缓存和日志）。旧文件夹不删除，新文件夹已有数据也不会被覆盖。也可以手动复制 `.appbuilder/`。如修改过 `config.yaml`，手动比对迁移配置；Keychain 凭据在 Mac 中保存，不随 ZIP 迁移。不要把 `.appbuilder` 上传到 GitHub 或发给别人。此版本的 ZIP 保留可执行的 `Start APP Builder.command`，双击即可启动。


## App 链接与邮件通知

New build 第 03 步默认取本次 App 链接。邮件通知不再使用本机 Outlook（New Outlook 不支持脚本控制，后台也无法获得控制权限），改为通过 Jira 发送：在左侧 **设置 → Jira 回填与邮件通知** 打开“构建成功后给团队发邮件”，填写一张固定的公告单号和收件人的 Jira 用户名，点“发一封测试邮件”确认能收到。之后每次构建成功并拿到 App 链接，会由公司 Jira 服务器发一封邮件：App 链接、分支、构建号和本次更新的 Jira。构建失败不发；发送失败只记录，不重发，也不改变构建结果。详见 [Jira 回写](docs/28-Jira回写与离线AI.md)。

新建 Plugin 任务使用 `release/v<版本>` 和 `release-build/job/release_v<版本中的点替换为下划线>/`；两段输入生成时间戳版本，三段输入保持固定版本。此前保存的本项目默认 `feature-build/job/` 配置仅在创建新任务时映射回 release 父路径；不改写计划模板、历史任务或其他项目的自定义路径。已有 release/feature 任务 Resume 保留原分支、Jenkins URL 和进度；只读诊断继续支持这两个已知目录下的具体分支 Job/Build。

### 定时任务成功后停止与拷贝

- “成功一次后停止”新建与拷贝默认开启，旧计划保留原设置。开启后，同一计划同时最多一个未完成构建（含 Resume）；失败仍按计划继续。整条任务达到 `SUCCESS/FINISHED` 时停止该轮，清空下次运行并取消尚未启动的排队。启用的 App 链接查询包含在成功条件内，邮件失败不重跑打包。已启动任务不强杀，其他计划不受影响。
- 停止原因和成功任务保存在本机数据库，重启不会补跑已停止的轮次。“重新启用”或将成功停止选项从关闭改为开启时，从当前时间开始新一轮，旧任务之后 Resume 成功不会停止新轮。派发已绑定任务但进程启动结果不明时，保留记录并提示核对／Resume，不自动重复启动。
- 列表“拷贝”只打开预填表单。沿用构建参数和 cron，默认勾选成功停止，要求改名并核对未来运行预览后保存；副本默认停用，可明确选择保存后启用。保存生成新 ID，不复制历史运行、原下次时间、成功标记或运行状态。当前仅支持五段 cron，不支持一次性日期。
- 拷贝不保存或回填临时密码、账号或 Token；继续按原配置使用既有 Keychain／TestFairy 租户会话。认证过期时仍能查看／拷贝配置，但保存或启动必须通过现有认证校验。

### 连续构建实现状态（未发布草稿）

新任务通过 `plugin_flow_version=2` 使用合并准备流程；旧任务继续原阶段与快照。Plugin/APP 复用只依据脚本保存的完整历史结果：来源与构建 SHA、有效输入参数、成功结果、原始 Job/Build、完整 Plugin 版本及两个产物 URL/hash、APP 原始卡片结果及分享链接。输入匹配且记录完整时复用，旧 Jenkins 日志被清理不影响复用；记录不完整时正常构建，不回查旧日志，也不保证分享链接永久不变。首次构建仍沿用现有 Jenkins 成功确认、Nexus upload JSON 和 TestFairy 精确卡片匹配流程。

新任务派发仅接受本次 POST 回执直接返回的 Build URL，或该 queue_id 明确对应的 executable Build。回执、尝试身份及关联记录持久化；未知或冲突状态的 Resume 不重新提交，不按新增编号、同 SHA 或时间邻近猜测归属。只有队列回执时，使用已有浏览器登录会话 GET 精确 `/queue/item/<id>/api/json`（保留 Jenkins context path），仅读取 `id/cancelled/task.url/executable.number/executable.url`。排队时有界等待（`queue_timeout_minutes` 默认12分钟）；只有同一队列、同一 Job、有效且一致的 Build URL/编号才能交回原监控。取消、404/410、401/403、跳转、非 JSON、网络错误或超时均保留回执并停止，Resume 不重新提交；认证恢复后可继续查询原队列。不会新增凭据、扩大权限或调用写入 API。队列已被清理且尚未保存映射时不会猜测历史编号。已通过本地真实 HTTPS 服务的显示/隐藏浏览器集成测试，但尚未在企业 Jenkins/SSO 环境实测。旧任务保留原恢复策略，不迁移已有记录。

### 查看运行数据

Recent builds 每条记录旁的“查看运行数据”打开只读抽屉，不改变当前日志选中的任务。数据概览按执行进度、Plugin、GMX、产物交付、历史复用和错误分组；原始 JSON 与复制使用同一份服务器脱敏快照。任务状态、阶段、创建时间来自任务数据库，其他运行字段来自该任务的 state.json。Trigger 成功不等于 Callback 或最终构建成功。

此入口不修改状态、不触发构建，不回查 Jenkins。任务必须已存在；缺失、损坏、超大或不安全文件显示错误。服务端移除凭据、个人邮箱、邮件正文及 URL 敏感信息；隐藏字段会显示“已隐藏”。

## 更新到最新版本

下载 ZIP 更新：把旧文件夹改名为 `<原名>-old`，把新版本解压成原来的名字，然后在新文件夹里运行 `./app-build web`。它会自动：从 `-old` 复制任务、定时任务和设置（旧文件夹不变）；停止从其他文件夹运行的旧网页和旧调度器（正在构建的任务不受影响）；把开机自启的调度器改指向新文件夹。

用 Git 的话：在项目目录执行 `./app-build update`：先停止本项目的旧服务，再 `git pull --ff-only`，然后启动 Web。只需这一条命令。`./app-build web` 发现之前执行过 `stop` 时会自动解除暂停；端口被占用时只提示一句该怎么处理。

## 停止本项目后台服务

在项目目录执行 `./app-build stop`。它先关闭新任务、恢复任务和定时派发的入口，再核实本项目进程，发送 TERM；默认给退出清理 5 秒，仍是同一进程实例时才发送 KILL。它不等待构建、邮件或其他业务完成。`--timeout 5` 控制退出清理时间。

停止包括本项目 Web、Scheduler、Worker 及能够确认属于它们的专属自动化子进程。普通用户浏览器、其他项目和 PID 已被复用的进程不会被按旧 PID 停止。无法确认归属时会报错，而不是扩大停止范围。若安装了本工具 LaunchAgent，会先核对加载配置和项目目录，再 disable 并 bootout 已核实的服务，避免已加载的 KeepAlive 再次拉起；保留 plist。

配置、任务历史和已保存 state 不会删除，也不会取消远端 Jenkins 构建。被打断的本地任务可在恢复服务后选择 Resume；未知提交回执仍按原有规则停下核对，不自动重复提交。

恢复接收和自带 LaunchAgent 原启用状态：`./app-build start`，然后 `./app-build web --no-open`。start 只恢复本命令更改过的 LaunchAgent 状态，并再次核对服务归属和保存的 plist SHA256；需要时 bootstrap 原服务。`stop --cancel` 是 start 的兼容别名，不会复活已经停止的业务进程。

旧版长期运行进程中的代码不会自动更新。首次使用本命令必须从含该功能的项目目录运行；命令会通过真实参数和工作目录识别旧服务。已脱离项目父进程、且无法证明归属的外部守护进程不按猜测终止。

验证边界：Linux 本地可控后台父子进程实测通过；Mac proc_pidinfo、proc_pidpath、KERN_PROCARGS2 和 launchctl 读取路径按代码及模拟场景验证，尚未在用户 Mac 上实际执行停机。


## 页面布局（v0.9）

- 左侧菜单分两组。**日常**：概览、新建构建、运行记录、定时任务。**设置与排查**：设置、测试工具。
- **设置**：只需配置一次的东西都在这里，全部存在 Mac 钥匙串：Jenkins 工号与两个 API Token、GitHub Token、Jenkins 浏览器登录（备用）、Jira 回填与邮件通知。新建构建表单里不再出现任何 Token 或密码。
- **测试工具**：Jenkins 构建检查、GMX 实测（会真实触发构建）、App 链接、Jira 查单与演练，用标签切换。
