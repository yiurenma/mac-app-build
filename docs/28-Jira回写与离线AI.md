# 构建成功后回写 Jira，并可选使用离线本地模型

## 做什么
任务 `SUCCESS` 后：找出自上次处理以来合入来源分支（如 develop）的提交，找 Jira 号时以 PR 为主要来源（先 PR 标题和分支名，再 PR 描述，找不到再退回提交标题/正文），对每个 ticket：
1. 写 comment（含 App 链接，带 `[app-build:<构建号>]` 标记，重复运行不会重复评论）；
2. assign 给 Reporter；
3. 状态流转到 `In Test`（已在该状态则跳过）。

失败、需人工处理等非 SUCCESS 结果不会触碰 Jira。Jira 步骤出错不会改变构建结果。

## 在页面里设置和测试（推荐）
启动 Web Console 后，左侧导航点 **Jira**：
- 填写 Jira 地址、Token，勾选启用/演练模式、流转状态名称等，点“保存设置”。Token 存入 Mac 钥匙串，页面和文件里都不会再出现。
- “测试连接”：只读，显示当前 Token 对应的 Jira 用户。
- “查询 ticket”：输入 TRANSACT-210390，只读显示标题、状态、Reporter，以及能否流转到你设置的状态。
- “用已成功的历史构建演练”：选一个成功的构建，按当前设置列出它会处理哪些 ticket、Jira 号从哪里找到的。只读，不改 Jira，也不影响正式记录。

页面保存的设置优先于 config.yaml，保存在本机 `.appbuilder/jira_settings.json`。环境变量 `JIRA_TOKEN` 仍可用，且优先于钥匙串。

## 默认安全
`jira.dry_run` 默认为 true（config.yaml 已默认启用 Jira，但只演练）。保持 dry_run，查看任务 `state.json` 的 `runtime.jira_report`（会做什么、哪些提交归属不明），确认后再把 `dry_run` 改为 false。

- 首次运行只记录基线提交，不改任何 ticket（`first_run_commits: 0`）。需要回溯可设为 N。
- 一个 PR/提交里写了多个 Jira 号：每个都会更新（评论、转给 Reporter、改状态），查不到的忽略。只取最先命中的那一层（PR 标题/分支 → PR 描述 → 提交标题 → 提交正文），低层级里顺带提到的编号不算。
- 在 Jira 里查不到的编号（HTTP 404，如 `UTF-8` 之类的误识别，或当前 token 看不到的 ticket）直接忽略，只列入 `jira_report.ignored_keys`，不算失败；其他错误（如 500、无权限写入）仍算失败。
- 基线只在非 dry-run 且全部成功后前移；部分失败时下次重跑，已完成的步骤因幂等而跳过。

## 流转时的必填字段（如 Bug 的 Root Cause）
有些工作流在流转到 `In Test` 时要求填写字段，例如 Bug 必须选 Root Cause。在「设置 → Jira 回填与邮件通知」的“更多设置 → 流转时必填字段”里每行写一条：`问题类型 | 字段名 | 值`，问题类型写 `*` 表示全部，例如 `Bug | Root Cause | Code`。
- 脚本先只读查询该流转需要哪些必填字段及可选值，再按配置填写；值必须是 Jira 可选值之一（不区分大小写）。
- 下拉类必填字段（如 Root Cause）没配置时，自动选 Jira 给出的第一个可选值，并在报告里写明选了什么。
- 文本类必填字段没配置、或配置的值不在可选值里：照常评论和转给 Reporter，但不改状态，在评论里提醒手动处理，并列入 `jira_report.needs_manual`。这不算失败。
- “查询 ticket”会显示问题类型、流转时的必填字段、可选值以及是否已配置。

## New build 表单：00 Jenkins 登录，第 3、4 步
- 00“Jenkins 登录”：开关默认开启，填一次工号和两个 Token（Plugin Jenkins、Platform Jenkins 各一个，Token 只在生成它的那台 Jenkins 有效），点“保存到钥匙串”，之后不用再填。“测试 Token”只读询问两台 Jenkins 的 `whoAmI`。
- 开关开启时，Plugin 构建、GMX 构建、取 App 链接都走 Jenkins 自己的接口（`build` / `buildWithParameters` 提交，201 + `/queue/item/N/` 作为派发回执，`api/json` 读状态和参数，`consoleText` 读日志），不打开浏览器。用 API Token 认证的请求不需要 CSRF crumb。
- GMX 提交前按 Job 的参数定义核对参数名、可选值和开关类型；构建开始后回读实际参数，与提交不一致就停下。
- 没保存 Token、旧任务、Jenkins 拒绝 Token（401/403）或跳转 SSO 登录页时，该步骤自动改用浏览器。已提交的构建由回执保护，改用浏览器后不会重复提交。
- 第 3 步“App 链接”只有开关；第 4 步“Jira 回填与邮件”只显示当前状态。Jira Token、回填和邮件通知都在左侧「设置」里（见下方“邮件通知”）。
- Token 和工号只存 Mac 钥匙串，不进任务、定时计划或日志。

## 认证
Token 优先在页面里填写（存入 Mac 钥匙串，不写入配置、日志或任务数据）；也可用环境变量 `JIRA_TOKEN`（Bearer PAT），或 `JIRA_USER` + `JIRA_API_TOKEN`，环境变量优先。`jira.base_url` 必须 https。企业根证书可用 `jira.ca_bundle` 指定。`flavor: server` 按用户名指派，`cloud` 按 accountId。

## 离线本地模型（可选）
不联网、不调用任何 API。`ai.enabled: true` 并设置 `ai.model_path` 指向 GGUF 文件，安装运行时 `pip install -r requirements-ai.txt`（llama-cpp-python，需要 Xcode Command Line Tools 编译，或由公司 Nexus 提供 wheel）。模型文件需由公司批准的渠道获得，并核对其许可证。

模型只做两件事：多 Jira 号时选主 ticket；把提交改写成给测试看的短说明（不合格则退回原提交标题）。模型文件缺失或依赖未装时自动降级为纯规则。

## 已知边界
- PR 通过 GitHub 接口（`/api/v3`，复用已有 GitHub Token）按提交查询；接口不通或权限不足时自动退回提交信息，并在 `jira_report.pr_lookup` 里写明原因。企业 GitHub 的接口是否开放、Token 权限是否够，需现场确认。
- 截图和视频在 Jira 附件里，这里不检查"改动前后截图/视频"这条要求。
- 未在公司 Jira、公司 Mac 上实测；`transition_name` 必须与你们 Jira 工作流中的流转名称一致。
- 本机 `.appbuilder/jira_ledger.json` 记录每个来源分支已处理到的提交。


## 邮件通知（通过 Jira 发送）

旧的本机 Outlook 发信已删除：New Outlook 不支持脚本控制，定时任务在后台运行时 macOS 也不会弹出“允许控制 Outlook”的授权。

现在用 Jira 自带的通知接口 `POST /rest/api/2/issue/{公告单}/notify`，由公司 Jira 服务器发邮件，使用已保存的 Jira Token，不需要 Outlook 或邮箱密码。

设置（左侧「设置」→ Jira 回填与邮件通知）：

- **构建结束后给团队发邮件**：默认关闭。与“启用 Jira 回写”、“演练模式”互相独立。
- **同一原因连续失败只发一次**：默认开启。同一组分支、同一步骤、同一原因（忽略构建号等数字）只通知一次；之后第一次成功的邮件会写明“之前的构建失败已恢复”。
- **公告单号**：Jira 的通知接口必须挂在一张单上，建一张长期开着的单专门用来发通知。
- **收件人**：Jira 用户名（通常是工号），逗号、空格或换行分隔，最多 50 个；随时增减后保存。

规则：

- 构建 SUCCESS 且拿到 App 链接：发“新测试包”邮件。
- 构建 FAILED / NEEDS_ATTENTION / TIMEOUT：发“构建失败”邮件，写明失败步骤、原因、Jenkins 构建链接和日志最后几行。手动中断（INTERRUPTED）不发。
- 每个构建最多一封。
- 在 Jira 回填之后发，邮件列出本次更新的 Jira（带链接）；演练模式下会注明“实际未修改”，首次运行注明“只记录起点”。
- 只发给填写的用户，不发给公告单的 Reporter、经办人或关注人。
- 发送失败只记录在任务结果里，不自动重发，不改变构建结果。

测试：填好后点“发一封测试邮件”，内容取最近一次带 App 链接的成功构建，主题带 `[TEST]`。

- HTTP 400：用户名不存在或公告单号不对。
- HTTP 401/403：Token 无效，或公司 Jira 关闭了通知接口（需找 Jira 管理员）。
- HTTP 404：公告单不存在或 Token 看不到。
- 只发给自己却没收到：Jira 可能默认不通知操作者本人，在 Jira 个人资料里打开“自己的更改也通知我”，或加一位同事一起测。

## 交付记录

左侧「交付记录」有两个标签：

- **Jira 更新**：每张单一行：时间、单号与标题、做了什么、给它的 App 链接、来自哪次构建、结果（已完成 / 状态需手动改 / 失败 / 演练）。数据来自每次构建保存的 Jira 回填结果。
- **邮件**：每封一行：时间、类型（新包 / 构建失败 / 测试）、主题（可展开正文）、收件人、来自哪次构建、结果。记录保存在 `.appbuilder/mail_log.jsonl`，从这个版本开始记录。

右上角可按 Jira 编号、标题、任务编号或邮件主题搜索。

## Jira 评论和邮件内容（英文）

发到 Jira 和邮件里的内容全部是英文。评论示例：

```
[app-build:42] New test build is ready: https://…/join/…

This ticket is included because of:
- PR #123 "TRANSACT-1 fix login" by alice https://github…/pull/123
- Commit 1a2b3c4d5e "TRANSACT-1 follow-up" by Bob https://github…/commit/…

Change: …

Note: the status was not changed to 'In Test' automatically: there is no 'In Test' transition from the current status 'Development' (available: Ready for Test, No Longer Required). Please update it manually.
```

- PR 标题、分支名、PR 描述、提交标题、提交正文里**提到的每一张单**都会处理，只有 Jira 里不存在的单会跳过；不再按数量截断（超过“每轮最多处理 ticket 数”只在日志里提醒）。
- 不管单子当前是什么状态、经办人是谁，都会写评论并转给 Reporter；只有状态改不了时留给人处理，并在评论里写明原因。
- 失败邮件只引用英文内容：工具自己的中文原因和日志行不放进邮件，完整原因在 APP Builder 的任务里看。
