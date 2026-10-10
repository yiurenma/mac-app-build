# v0.6 本地 Web Console（macOS）

## 目标与使用方式

安装后，**双击项目根目录 `Start APP Builder.command`** 即可启动本地管理页面，或通过终端运行 `./app-build web`。页面固定仅监听 `127.0.0.1:8765`；浏览器打开同一地址。关闭终端里的 Web 服务**不会结束**已经启动的独立 Worker；Worker 仍采用原项目独立目录和 SQLite 锁保护。

若以 Git clone 形式安装，双击入口会先尝试 `git pull --ff-only`，且仅在工作区干净时更新；ZIP 方式跳过更新。`app-build` 按需调用 `install.sh`，并在 Web 服务启动时执行非阻塞 doctor；Web 页面将展示环境问题，在正式创建/恢复任务前执行严格 doctor。缺少包时仅使用已配置的企业 Nexus，不修改 pip / CA 配置。

## 页面能力

- **New build**：Plugin 源分支、GMX 源分支、Plugin 版本、国家、项目名；可选择可见 Chrome、完整动作日志。
- **Credentials**：GitHub PAT、Jenkins 用户名/密码首次可在页面输入，存储在当前 Mac 用户 Keychain；后续无需输入。用户密码与 Token 不保存到 SQLite、构建任务参数、配置文件，也不会返回给网页。已设置的环境变量 `GITHUB_TOKEN` 优先于 Keychain PAT。
- **Live activity**：显示原 Worker 产生的真实日志和阶段，3 秒一次增量刷新，含历史记录和等待心跳。它不是对 Jenkins 调用频率的调整；新 Job 仍按 `discover_poll_seconds: 10` 检查，Build 状态仍按 `poll_seconds: 45` 检查。
- **Recent builds**：显示历史、运行、成功、失败/需要关注数；点击查看每个任务的日志，失败时点击截图链接。
- **Resume task**：原 Worker 停止且任务未成功时才允许恢复。沿用原任务 ID、时间戳、已有 Push/Build Number 和 Callback 检查，不新建任务。真正是否能恢复仍遵循现有 Jenkins 审慎策略，不能重复点击运行中的构建。
- **并发隔离**：仍使用原 SQLite 和独立 Worker，保持 config.yaml 中 `max_parallel: 2` 的限制。

## 安全设计

- 服务仅绑定 `127.0.0.1`，**不**开放到公司网段，不需要云服务器，也不连接外部前端 CDN/analytics。
- 写操作需要同源 Origin、随机 CSRF token 和 JSON 内容类型；检查 Host，阻止 DNS rebinding。静态页面采用限制型 CSP，文本通过 `textContent` 渲染。
- 请求长度有限制；日志使用偏移增量读取，不加载整个大文件到浏览器；截图只能从已知任务目录读取符合文件名规范的 PNG。
- GitHub PAT 和 Jenkins 密码经 loopback HTTP 一次性传给本地 Python 服务并保存至 Keychain；浏览器端不使用 localStorage/cookie 保存密钥。日志提供尽力脱敏，分享前仍应检查内容。
- Jenkins 内部页面仍由 Playwright 真实 Chrome 自动化操作，**不是**通过 Jenkins REST API 绕过 SSO/MFA；MFA 仍可能需人工确认。

## 前置条件和限制

- Python >=3.11、Git、IT 管理的 Chrome、pip 的公司 Nexus 配置、企业网络/VPN；GitHub 和 Jenkins 的权限。
- macOS `security` 程序用于 Keychain，首次使用可能触发系统确认。公司 SSO 表单变化仍需联调调整。
- 在已克隆项目上启动 `--update` 可能因为本地改动或 Git 冲突而安全停止；检查这些问题后重新启动。
- 本版本已经本地验证静态页面渲染、真实 HTTP 交互/安全校验及离线任务模拟。不能代表在公司真实 Jenkins 上已完成端到端构建，也未在用户 Mac 版本上现场验收。

## 命令

```bash
# 直接启动本地页面
./app-build web

# 安全更新后启动（Git clone 工作目录）
./app-build web --update

# 指定另一个本地端口
./app-build web --port 9876

# 不自动弹出浏览器
./app-build web --no-open

# 原 CLI 构建、watch、resume、status 仍可用
./app-build status
```

退出 Web 服务 Ctrl+C，**不会主动杀死构建 Worker**。任务详细日志位于 `.appbuilder/runs/<TASK_ID>/run.log`，截图位于该任务的 `screenshots/`。
