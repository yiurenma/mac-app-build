# Linux 服务器 / Docker 部署

macOS 之外的支持范围：**Linux（含 Docker）**。Windows 未适配。

## 与 Mac 的差异

| 项目 | macOS | Linux / Docker |
|---|---|---|
| 密码、Token 存放 | Keychain | `keyring`（有桌面 Secret Service 时），否则环境变量 / Docker secret |
| 调度器常驻 | LaunchAgent | `./app-build scheduler install` → `systemd --user` 服务 |
| 防休眠 | `caffeinate` | 不需要 |
| 浏览器 | 本机 Chrome | `config.yaml` 中 `jenkins.browser_channel: chromium`（Docker 镜像已带） |

## 凭据（不会写进任务 JSON、配置或日志）

读取顺序：环境变量 → `<变量>_FILE` 指向的文件 → keyring。变量名规则：
`APPBUILDER_SECRET_` + 服务名与账号，非字母数字换成 `_` 后大写。

| 内容 | 变量 |
|---|---|
| Jenkins 账号名 | `APPBUILDER_SECRET_MAC_APP_BUILDER_JENKINS_ACCOUNT_DEFAULT` |
| Jenkins 密码 | `APPBUILDER_SECRET_MAC_APP_BUILDER_JENKINS_<账号名>` |
| GitHub PAT | `APPBUILDER_SECRET_MAC_APP_BUILDER_GITHUB_TOKEN_ENTERPRISE` |
| Jenkins 用户 ID(API) | `APPBUILDER_SECRET_MAC_APP_BUILDER_JENKINS_API_TOKEN_USER_DEFAULT` |
| Jenkins API Token | `APPBUILDER_SECRET_MAC_APP_BUILDER_JENKINS_API_TOKEN_PLUGIN_TOKEN` / `..._GMX_TOKEN` |
| Jira Token | `APPBUILDER_SECRET_MAC_APP_BUILDER_JIRA_TOKEN_ENTERPRISE` |

不确定名字时，在界面里点保存会报错并直接给出该项应使用的变量名。

## 裸机 Linux

```bash
./install.sh
./app-build web --no-open          # 仅监听 127.0.0.1:8765，远程请用 ssh -L 8765:127.0.0.1:8765
./app-build scheduler install      # systemd --user 常驻
sudo loginctl enable-linger $USER  # 无人登录时也保持运行
```

## Docker

Web Console 出于安全只监听回环地址并校验 Host/Origin，所以**不要**直接发布端口；用宿主机网络，再从宿主机本地或 SSH 隧道访问：

```bash
docker build -t app-builder .
docker run -d --name app-builder --network host \
  -v app-builder-data:/app/.appbuilder \
  -e APPBUILDER_SECRET_MAC_APP_BUILDER_GITHUB_TOKEN_ENTERPRISE=... \
  app-builder
```

镜像里同时运行 Web Console 和定时调度器。数据（任务库、定时任务、日志）在卷 `/app/.appbuilder`。

## 未改动的限制

- 构建所需的公司 Jenkins/Nexus/SSO 仍需容器能访问；SSO 若要求交互式 MFA，无界面环境无法完成。
- Windows 需要另行替换文件锁、进程组与信号处理，本次未做。
