# 浏览器操作审计（2026-10-11）

范围：现有 Jenkins 与 TestFairy 浏览器操作。只使用路由隔离或 localhost 的离线 Chromium 页面、假凭据；没有访问公司认证、创建真实构建、上传或发送邮件。新增改动未推送。此文件记录代码与实际测试证据，不把 click 返回、HTTP 200、DOM 已加载当作业务成功。

## 操作与验证清单

| 操作 | 前置条件 | 实际动作 | 后置证据 | 失败、重试及日志 | 离线证据 |
|---|---|---|---|---|---|
| Jenkins / TestFairy 认证 | 有界等待可见、可编辑/启用控件；每次动作复核当前 origin、form action、formaction；原生校验通过 | 填写、Next、单次提交 | helper 仅返回“尝试提交”；调用方验证认证页面退出、密码框消失；恢复流程另验证精确原目标 | 未批准目标不填凭据；MFA 有界等待；不因 click 无效果重复提交密码；日志不含值 | `test_browser_auth_readiness.py`、`test_startup_and_auth.py` |
| Jenkins 导航/失效会话 | 目标 URL 与允许登录域 | goto、一次 fresh session 恢复 | 精确目标 HTTP / 页面错误类型；明确 ACL 拒绝不会绕过 | 401/403 可恢复一次；多次失败有界；分离 Plugin / GMX 缓存 | `test_jenkins_access_recovery.py` |
| Jenkins 参数页初始化 | load 完成、无可见 aria-busy | 打开参数页、等待控件 | 唯一 Build 所属表单及正确 Job / POST action | 无就绪证据停止；不使用固定 slow_mo 作为正确性保证 | `test_jenkins_mode_timing.py`、`test_jenkins_dispatch.py` |
| GMX 填写 | 唯一可识别字段、正确控件类型 | 下拉、文本、checkbox/关联 label | 填写回读；真正提交前按已解析意图值再核对，字段仍启用 | 重绘改值或禁用字段不提交；末次回读检查只记录字段名/一致性 | `test_jenkins_checkbox_labels.py`、`test_jenkins_dispatch.py` |
| Jenkins Build 提交 | 唯一可操作按钮、正确 Job / form、有效参数 | 先持久化 DISPATCHING，再 click 一次 | 实际目标 POST、响应状态、同源 queue ID 或后续构建证据 | clicked / POST / response / queue 分开记录；UNKNOWN 不盲重发；拒绝响应不称接受 | `test_jenkins_dispatch.py` |
| Jenkins 构建识别 | Job 编码路径及保存 baseline | 只读枚举本 Job Build | 单个新增编号；多个候选阻塞 | PREPARING 后外部新增不能认领；提交前采样变化置 CONFLICT；等待超时不声称仍在队列 | `test_jenkins_dispatch.py`、`test_jenkins_offline.py` |
| TestFairy 延迟登录 | 同源、唯一可见登录表单 | 最多一次自动填写提交 | 返回应用页并出现应用/根目录证据 | 延迟出现可等待；跨域 action 零填写；失败保留首异常 | `test_testfairy_readiness.py` |
| TestFairy 目录 | 精确名称、唯一可见可交互候选及已走路径 | 点击目标目录 | 精确 breadcrumb / selected 路径或目标子层内容变化 | 同源 URL 改变或无效点击不能单独确认；明确矛盾 breadcrumb 不能由新平铺节点覆盖；总预算有界 | `test_testfairy_readiness.py`、`test_testfairy_loading.py` |
| TestFairy 版本入口 / notes | 唯一 App 与语义入口 | See all versions；每卡 notes 仅展开一次 | 非空新版本列表；同卡四字段出现后才继续 | 重绘/延迟受总预算控制；不跨卡拼接字段 | `test_testfairy_versions.py` |
| TestFairy 翻页 | 唯一启用 Next / Load more | 单次前进 | 非空新节点/身份进展；明确结束标记 | 空过渡等待，不重复点击；完整性未知 INCOMPLETE；有明确终点才 NOT_FOUND | `test_testfairy_versions.py` |
| TestFairy 分享读取 | 同卡四字段精确一致；Build 编号和 App build 分离 | 只读取该卡已有链接或唯一新 Share 容器 | 同租户合法 /join/；未知 query 阻塞 | 不创建公开权限；不声明历史版本已固定；诊断有界、脱敏、保首因 | `test_testfairy.py`、`test_testfairy_trace.py` |

## 本轮确定问题与修复

- 两步登录 Next 跳到未批准域名后仍填写密码：真实 Chromium 离线复现；现在逐次验证目标，并在等待控件启用后再次校验。
- 登录 DOM 在 load 后插入：现在有界等待；disabled 控件等待可操作；原生 invalid 表单不提交。
- PREPARING 任务恢复遇到外部新增 Build：原逻辑会认领；现在置 CONFLICT，既不认领也不重发。
- GMX 参数在填写后、最后点击前被重绘改值/禁用：现在依据意图值做最终回读，失败零 POST。
- TestFairy 单次探测错过延迟登录表单、目录 click 无后置证据：现在分别轮询等待和验证目录变化。

## 已验证的时序机制与限制

同一 500ms 异步脚本绑定 fixture：原快速 click 为零 POST；slow_mo=300 可以掩盖该时序；等待可观察 load/busy 后，无 slow_mo 也只产生一次 POST 并取得 queue 回执。800ms 延迟实验表明固定慢动作并不保证成功。load/busy 仅是部分就绪证据，任意 load 后 JS 初始化若不暴露状态，不能凭 load 推断成功；仍需请求及业务后置验证。

当前环境真实 headed Chromium 启动失败（无 X server / DISPLAY），相关用例明确跳过。headless 下调整 slow_mo 的实验不能冒充真实 headed 测试。企业 Chrome/SSO、实际 Jenkins 页面事件绑定、TestFairy 历史版本绑定及真实上传/邮件均未验收。

DISPATCHING / UNKNOWN 的单个新编号仍依赖现有 Job baseline 模型，不提供服务端唯一幂等令牌；存在外部同时构建时不得把这种启发式当成严格归属证明。旧 baseline 没有派发证据时不会自动清空重发。

## 最终验证结果

冻结代码后的完整 `unittest` discovery 回归于 2026-10-11 完成，耗时 158.497 秒：477 项通过、0 失败、0 错误。发现共 491 个用例；runner 计数为 479，其中 2 个 headed 用例跳过，另一个含 12 个用例的 headed 认证测试类在 setUpClass 跳过，未计入 testsRun。输出的 `OK (skipped=3)` 表示两个方法和一个类，不能理解为仅三个用例未运行。跳过原因均为实际 Chromium headed 启动缺少 DISPLAY/X server；没有安装或伪造 Xvfb。

完整日志：`/tmp/browser-actions-final.log`，末尾 `AUDIT_COUNTS` 保存真实成功数和跳过原因。Python compileall、前端 app.js / schedules.js 语法检查及 `git diff --check` 均通过。独立审查完成，无剩余阻塞发现。此轮涉及 3 个实现文件、5 个测试文件和本审查文档，均未提交或推送。

已发布基线 `0b1f7ac82d00d25ef6ed725783201ad18aabdade` 的 [GitHub Actions](https://github.com/yiurenma/mac-app-build/actions/runs/38137665079) 已成功，包含三轮各 452 项测试；这不代表本轮未提交改动的 CI。CI ZIP 下载被环境代理返回 403，未从 ZIP 独立核实该次 CI 的逐项跳过明细。
