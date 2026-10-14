# 构建成功后回写 Jira，并可选使用离线本地模型

## 做什么
任务 `SUCCESS` 后：找出自上次处理以来合入来源分支（如 develop）的提交，找 Jira 号时以 PR 为主要来源（先 PR 标题和分支名，再 PR 描述，找不到再退回提交标题/正文），对每个 ticket：
1. 写 comment（含 App 链接，带 `[app-build:<构建号>]` 标记，重复运行不会重复评论）；
2. assign 给 Reporter；
3. 状态流转到 `In Test`（已在该状态则跳过）。

失败、需人工处理等非 SUCCESS 结果不会触碰 Jira。Jira 步骤出错不会改变构建结果。

## 默认安全
`config.yaml` 的 `jira.enabled=false`、`jira.dry_run=true`。先开启 enabled 保持 dry_run，查看任务 `state.json` 的 `runtime.jira_report`（会做什么、哪些提交归属不明），确认后再把 `dry_run` 改为 false。

- 首次运行只记录基线提交，不改任何 ticket（`first_run_commits: 0`）。需要回溯可设为 N。
- 一个提交出现多个不同 Jira 号：有本地模型时让模型从候选中选一个（模型输出的编号必须在候选里，否则丢弃）；没有模型则跳过并列入 `ambiguous_commits`，不猜。
- 基线只在非 dry-run 且全部成功后前移；部分失败时下次重跑，已完成的步骤因幂等而跳过。

## 认证
只读环境变量，不写入配置、日志或任务数据：`JIRA_TOKEN`（Bearer PAT），或 `JIRA_USER` + `JIRA_API_TOKEN`。`jira.base_url` 必须 https。企业根证书可用 `jira.ca_bundle` 指定。`flavor: server` 按用户名指派，`cloud` 按 accountId。

## 离线本地模型（可选）
不联网、不调用任何 API。`ai.enabled: true` 并设置 `ai.model_path` 指向 GGUF 文件，安装运行时 `pip install -r requirements-ai.txt`（llama-cpp-python，需要 Xcode Command Line Tools 编译，或由公司 Nexus 提供 wheel）。模型文件需由公司批准的渠道获得，并核对其许可证。

模型只做两件事：多 Jira 号时选主 ticket；把提交改写成给测试看的短说明（不合格则退回原提交标题）。模型文件缺失或依赖未装时自动降级为纯规则。

## 已知边界
- PR 通过 GitHub 接口（`/api/v3`，复用已有 GitHub Token）按提交查询；接口不通或权限不足时自动退回提交信息，并在 `jira_report.pr_lookup` 里写明原因。企业 GitHub 的接口是否开放、Token 权限是否够，需现场确认。
- 截图和视频在 Jira 附件里，这里不检查"改动前后截图/视频"这条要求。
- 未在公司 Jira、公司 Mac 上实测；`transition_name` 必须与你们 Jira 工作流中的流转名称一致。
- 本机 `.appbuilder/jira_ledger.json` 记录每个来源分支已处理到的提交。
