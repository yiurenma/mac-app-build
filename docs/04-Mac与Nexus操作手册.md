# macOS 安装、运行与 Nexus 指引

## A. 前置条件

- Mac 已获得公司批准的 Python **3.11+**、Git、Google Chrome（或 Edge）、VPN/公司网络。
- 已取得 Plugin 与 GMX GitHub 仓库的读写权限、Jenkins 构建权限。
- 准备企业 GitHub PAT，通过环境变量注入；不能发给他人，也不能写进 Git 配置或工具 YAML。
- Python 包应来自公司 Nexus 的 **PyPI group/proxy** 仓库；该 URL 由公司 DevOps/IT 提供，不能把其他制品 Nexus 仓库地址当成 PyPI `/simple/` 地址。

## B. 安装（默认不用公网）

```bash
cd mac-app-build
python3 -m pip --version      # 只检查已安装的 pip
./install.sh                  # 沿用公司 Nexus 和 CA 配置
./app-build doctor
```

`install.sh` 会：检查已有 Python 3.11+ → 只读验证有效的 `index-url` 和 `extra-index-url`（均为 HTTPS 企业 Nexus 时允许）→ 创建 `.venv` → 使用**现有 pip 配置**安装 `requirements.txt` → 检查浏览器配置。**它不安装/升级 pip，不修改 pip.conf、global.cert 或 PIP_CERT。** 默认 `jenkins.browser_channel: chrome`，复用公司管理的 Chrome。

默认允许 `*.hsbc` 等公司域名，拒绝公网 PyPI、HTTP 和陌生主机。其他由 IT 批准的企业主机，可通过 `APP_BUILDER_ALLOWED_PIP_HOSTS` 明确列出准确域名。已安装 pip 仍然需要在 `.venv` 内安装 `playwright` 和 `PyYAML`。`python3 -m pip config list` 可能含凭据，请不要分享完整输出。

**三个容易混淆的下载源：**

| 下载内容 | 使用的仓库/软件 | 何时访问 |
|---|---|---|
| `playwright`, `PyYAML` 与传递依赖 | 公司 Nexus PyPI 索引 | `./install.sh` |
| 浏览器二进制 | 公司已安装 Chrome；或企业浏览器镜像 | 安装/登录阶段 |
| GMX 的 iOS Plugin/依赖及 lock SHA | `./bootstrap.sh` 内部使用的公司资源 | 正式打包 GMX 阶段 |

修改 Python 的 `PIP_INDEX_URL` **不会**自动让 Playwright 二进制浏览器或 bootstrap 下载走同一个 Nexus。若确需 Playwright Chromium，必须选择 `browser_channel: chromium` 并由 IT 配置 `PLAYWRIGHT_DOWNLOAD_HOST`（或 `PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST`）；不要用公网绕过企业代理或 SSL 校验。

## C. Token 与 Jenkins 登录

```bash
export GITHUB_TOKEN='<通过公司批准方式获取的 PAT>'
./app-build login --site plugin
./app-build login --site gmx
```

登录成功后，本地 `.appbuilder/auth/plugin.json` 与 `gmx.json` 可能含会话 cookies，保护为敏感文件。推荐通过 macOS Keychain/企业密码管理器注入 Token，而非长期明文存储在 `.zshrc`、脚本和截图中。

## D. 验证、执行与并发

```bash
# 先验证 URL/分支规则，不执行 Clone、Push 或 Build
./app-build run --plugin-branch develop --plugin-version 1.3 \
  --gmx-branch develop --country UK --project-name PayByBank --dry-run

# 正式后台启动两个独立 APP
./app-build run --plugin-branch develop --plugin-version 1.3 \
  --gmx-branch develop --country UK --project-name PayByBank
./app-build run --plugin-branch develop --plugin-version 1.4 \
  --gmx-branch develop --country HK --project-name Demo

./app-build status
./app-build logs <TASK_ID> --follow
./app-build resume <TASK_ID>
```

默认每次启动使用独立 `runs/<TASK_ID>/plugin` 和 `gmx` 目录，并使用 macOS `caffeinate -i` 尝试阻止空闲睡眠；仍需保持 VPN 和网络可用。任务失败或恢复时先查看日志，确保不是凭据过期或公司 Jenkins UI 变化；切勿直接手动重复点击触发。

## E. 离线质量回归

```bash
./selftest.sh     # 重复三轮离线测试并记录完整日志
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q appbuilder
```

如果暂时没有配置 Nexus，但本机已经具备 Python + 所需依赖，可先运行 `python3 -m unittest discover -s tests -v` 验证业务逻辑，**不必绕过公司安装策略访问公网**。

## F. 常见问题

- **`未设置 GITHUB_TOKEN`：** 检查当前 shell 的 `export`，并让后台 Worker 从同一个 shell 启动；必要时使用企业 GitHub PAT 及 SSO 授权。
- **Nexus `401/403`：** 通过 IT/DevOps 获取批准的认证流程；不要把用户密码直接放在 Shell 命令行。
- **企业 CA 证书错误：** 使用批准的 `PIP_CERT` 配置；不要加 `--trusted-host` 随意关闭证书验证。
- **Playwright Chrome 找不到：** 先通过 IT 安装公司管理的 Chrome，检查 `config.yaml` 的 `jenkins.browser_channel`。
- **Jenkins 登录无效：** 再运行 `login --site ...`，SSO 若限制自动控制则需要公司许可的会话管理方案。
- **Jenkins 找不到 Build：** 检查新分支是否已被 Jenkins 扫描、URL 映射是否符合 Job 命名规则。
- **bootstrap 失败：** 查看 `<TASK_ID>/bootstrap.log`，确认公司内网制品仓库可访问和所需本地依赖可用。
- **其他文件被修改：** 工具会拒绝提交。不要随便增大白名单，先分析引入变更的 Hook 或 bootstrap 行为。
- **APP 构建 SUCCESS 但上传不确定：** 检查 `result.json.runtime.upload_validation`；必要时与负责上传 Pipeline 的同事确认成功日志表达式。
