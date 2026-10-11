# TestFairy 链接与本机 Outlook 结果通知

共享 New build / 定时表单新增两个独立开关，默认关闭，旧任务保持原流程：

- TestFairy：GMX 成功后，查找并保存本次 App 的已有分享链接。
- Outlook 自动通知：用户填写收件人并开启后，成功、失败、超时、需人工处理均尝试通知；不依赖 TestFairy 开关或成功。默认关闭时不发送。

## TestFairy

默认登录入口 `https://hsbc-mobilex.testfairy.com/login`。公开登录表单由只读浏览器核验：原生 POST `/login/`，`email`、`password`、`rememberme` 和动态 CSRF 字段。程序在浏览器内填表，保留原 CSRF，不拼接登录 POST。登录后的 App 列表、卡片和 Share 对话框 DOM 尚未使用真实账号验证。

账号密码与 Jenkins 独立。默认经私有 stdin 管道传给本次 Worker，不放入任务 JSON、日志、计划、环境变量或 argv；选择“允许保存本机认证”才写该租户独立 Keychain 条目并保存浏览器会话（0600 文件权限）。定时模板永远拒绝账号密码字段，只复用此前授权的 Keychain。MFA/额外验证在可见 Chrome 完成；不绕过。

目录默认由实际 GMX 参数推导：`IOS_Upload/IOS_<BUILD_TYPE>/<CUSTOM_FOLDER>`，支持表单覆盖逐层目录，不固定 UK。仅匹配同一 App 卡片 Release Notes 中的完整 GMX Build URL、精确 Build number、完整提交 SHA、Folder Param；`%2F` 与解码后的路径不混同。不取最新包，不将 Plugin 产物版本当 App 版本。App 版本及括号中的构建号单独读取。

只提取已有 `/join/` 链接，可打开匹配卡片的 Share 视图，但不点击创建、启用公开访问或保存权限。无现有链接、多候选、错构建、缺少元数据或版本不唯一均停止；上传等待有界。默认最长 600 秒，`config.yaml` 的 `testfairy.wait_seconds` 可设 1–1800。真实主题不同可在本机配置 `testfairy.card_selector`；默认 article/.app-card/.build-card/.version-card/[data-build-id]，未识别时安全停止，不能声称已适配未查看的登录后 DOM。

`TESTFAIRY_LOOKUP` 是独立完成标记。查找失败后 Resume 只重试未完成阶段，不重新执行已完成 GMX。成功链接、App 版本及匹配证据写入本机 runtime/result，并显示于任务结果。

## Outlook

使用本机已登录账号，不读取 Outlook token，不要求 SMTP 或 Graph 凭据，不自动切换其他邮箱。表单填写完整收件人邮箱（英文逗号/分号分隔，最多 50 个），可明确填写发件公司邮箱；留空只允许唯一可核验账号。检查按钮只读探测安装字典、原生脚本接口与账号，不发测试邮件。

适配器要求本机 Outlook 声明并可读取所需脚本能力，使用 macOS 原生 JXA/Apple Events。不能保证新版 Outlook 支持；缺少脚本字典、账号不可核验或自动化权限拒绝会明确 BLOCKED，保留预览，不把 mailto 或草稿当发送完成。本开发环境没有 Mac/Outlook，原生发送尚未实测。不会建议降级 Legacy 作为持久方案：微软宣布其 Exchange Online 支持自 2026 年 10 月结束。

结果邮件包含真实任务状态、已完成阶段、当前失败阶段、原因、需处理提示、脱敏日志摘要、分支、实际 Plugin 产物版本、Jenkins 编号/URL、SHA、时间。仅任务成功且匹配证据可复核时附 App 链接。失败时不借用历史包链接。邮件状态与构建状态分开记录；通知失败不会抹掉原始构建结果，也不会递归触发失败通知。

发送前持久化 SUBMITTING。同一状态/阶段/构建/链接的通知去重；恢复后从失败变成功会产生新结果通知。已证实在 send 前失败会标记 BLOCKED 并保留阶段；提交中断或返回不明标记 UNKNOWN，不自动重发，需人工核对 Outlook 发件箱/已发送。SUBMITTED 只代表原生接口接受提交，不是服务器投递确认。没有真实收件人配置的本轮开发不会发信。

依据：[Microsoft Legacy 支持说明](https://support.microsoft.com/en-us/outlook/end-of-support-for-legacy-outlook-for-mac)、[默认账号说明](https://support.microsoft.com/en-us/outlook/set-an-account-as-the-default-in-outlook-for-mac)。当前没有据以保证所有 New Outlook 版本支持此本机自动化的官方接口承诺，实际客户端能力探测是必要条件。

## 验证边界

离线 Chromium 使用公开已核验的登录字段及原生 CSRF 表单模拟，验证匹配、错包、多候选、分享权限保护；Outlook 使用 mock 验证唯一账号、收件人、防重、失败通知及结果隔离。真实 TestFairy 登录后的 DOM、公司 MFA、Mac Keychain、本机 Outlook JXA、最终邮件投递仍须现场验证。本环境未登录公司账号、未触发构建、未发真实邮件。

## 两个独立测试入口

“校验 TestFairy 分享链接”使用单独的登录入口、临时账号密码、预期 `/join/` 分享链接和所选历史任务。后端复用正式 `find()`，按保存的 GMX URL/编号/SHA/目录查找并提取已有链接，显示预期、实际及 MATCH/MISMATCH，不直接打开预期链接回显。不足时明确要求选择具备目标信息的任务；不取最新包。URL 比较只规范 scheme/主机大小写及 HTTPS 默认端口，保留路径和 query 区别；敏感认证参数被拒绝。

“发送一封 Outlook 测试邮件”由用户明确点击并填写收件人后执行，复用所选历史构建的正式报告模板，包含逐步输入/输出/状态/错误和已核验的 App 链接；缺失信息标为未记录，秘密脱敏，主题明确带 TEST，不改正式通知去重记录。先核验本机接口与账号；提交后只报告 SUBMITTED，不声称已验证发出或送达；UNKNOWN 不自动重试。测试记录在独立 SQLite 中按请求编号幂等，同类并发与短时间重复点击被限制。两种测试都不会创建打包任务或启动 Jenkins。

测试接口：POST `/api/delivery-tests/start`（沿用 localhost/Origin/CSRF 防护），GET `/api/delivery-tests/<id>`。TestFairy 只允许 `config.yaml` 的 `testfairy.allowed_hosts` 明确列出的租户，默认仅 `hsbc-mobilex.testfairy.com`，不把公司凭据自动提交到外部重定向。测试密码不入数据库、不默认保存会话。独立包匹配测试等待上传最多 60 秒，MFA 最多 180 秒。

定时 TestFairy 在保存计划时检查已明确选择保存认证，且存在 Keychain 条目或对应会话文件；不能仅持有建单时的临时密码。此检查不承诺未来会话不会过期；到点认证失败仍以真实状态记录。邮件通知去重键不含当前时间或每次更新的日志内容。日志摘要使用统一脱敏函数移除凭据值、Cookie/Token 及 URL 查询参数。

## 逐步骤邮件明细

邮件开头给总结果，随后逐步列 Plugin checkout/edit/push/Jenkins、GMX checkout/edit/lock/push/Jenkins 和 TestFairy lookup 的状态、输入、关键输出及错误摘要。字段来自保存的参数与 runtime 白名单：来源/目标分支、版本修改记录、基线/提交 SHA、两个依赖的完整版本/SHA256、精确 Build URL/编号、App 版本与已验证链接。缺记录写“未记录”，未运行写“未执行”，未启用写“未启用”；不从期望输入反推未保存输出。新失败保存脱敏的每步错误，恢复后保留并标“历史错误（已恢复）”。原始配置和任意 runtime 字段不会整体序列化到邮件。

错误和日志先脱敏：Authorization/Cookie 整行、账号密码/Token 字段及 URL 查询参数不外发。为避免修改分享地址语义，实际含查询参数的分享链接会要求人工核对，不删参数后发送另一个地址。独立预期链接比较仍保留 query 区别，不能把不同链接判为匹配。

## 本次本地验收结果

最终 `./selftest.sh` 三轮各 364 项通过，耗时 70.665 / 67.934 / 65.081 秒。包含 17 项 TestFairy 专项、10 项独立测试 API、10 项 Outlook 通知、7 项逐步报告及 5 项真实 Chromium UI 回归。Python 编译、Shell 语法、CLI dry-run、两个 JavaScript 语法及差异空白检查通过。Python 3.11 grammar 模式通过；此环境实际使用 Python 3.12，未运行 Mac/3.11 实机。

验收中曾出现一项旧 soft-404 测试的 6ms 时间预算不足，导致首次页面检查前即超时；已仅将该测试预算改为 600ms，生产轮询逻辑未改变。最终三轮无失败。全部邮件测试使用 mock，没有向任何真实收件人发送。


### 现场诊断

New build 按 01 Plugin、02 GMX、03 TestFairy、04 邮件分组；03 通常只需账号密码，04 只需收件人，例外路径/发件账号折叠在高级设置。两个独立测试位于页面最底部，与 Jenkins/GMX 测试同级，左侧 TestFairy Test / Outlook Test 可直接定位。先在任务列表选择历史构建，再填写测试收件人并点击发送；不需要开启正式自动邮件开关。

TestFairy 先识别 APPS 入口，等待异步目录出现；排除隐藏节点、普通标题和面包屑，按目录树父子路径定位。面包屑或已选树节点证明已到目标路径时不重复点击祖先。多个有效候选不会取第一个，而显示失败层级、页面路径、可见候选数及标签/角色/树深度；不收集整页内容或链接查询参数。自定义 `testfairy.folder_selector` 必须只覆盖实际目录节点。认证后的真实公司 DOM 仍需用户运行验证。

Outlook 的 JXA 返回阶段、错误号及 `send_attempted`。创建邮件、设置账号/收件人、取编号等明确在发送前的异常显示 BLOCKED；调用发送之后异常、超时、返回解析失败仍为 UNKNOWN。原生退出码和有界脱敏错误保留，不能凭唯一发件账号推断已发送。UNKNOWN 应先在 Outlook 的发件箱/已发送核对，程序不会自动重发；更换测试构建也不会绕过同一目标的不明提交保护。旧版本已记录的 UNKNOWN 不能事后推断发送阶段。

开发覆盖生产 JXA 在 Node 模拟宿主中的执行分支、Python 调用边界和真实 Chromium 的离线目录布局；这些不是实际 macOS Outlook/Apple Events 或公司站点的验收。

本轮补充：APPS 导航本身也有界等待；进入最终目录后在当前页面等待异步卡片，不重新导航到 `/my` 打断加载。Share 仅接受匹配卡片内或由其操作新出现的唯一弹窗内链接，拒绝全页面回退。UNKNOWN 目标比较将收件人视为不分大小写的集合，并规范化发件账号大小写，展示及实际收件人顺序不变。

旧版本 UNKNOWN 丢失的阶段/原始错误无法还原。安全排查入口：New build → 04 邮件 → 更多邮件设置 → 检查本机 Outlook（不发信）；该检查仅验证字典和账号。用户应先在 Outlook 草稿、发件箱、已发送搜索原测试主题，不重复发送原 UNKNOWN。新的失败结果显示阶段、send 是否调用、原生错误号和退出码；本次没有实际 Mac 原生发送根因或投递验证。
