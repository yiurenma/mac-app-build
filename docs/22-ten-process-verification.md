# v0.7.3 十进程并行验证

本次基于提交 3686cb4c1c1d6af4eeec402856e29a43db11eae7，不修改生产执行逻辑。

## 验证结果

- 同时启动十个独立进程，每个运行原有完整回归 218 项，十个进程均退出 0，合计 2180 次用例执行。日志保存在 parallel-verification-v0.7.3/suite-01.log 至 suite-10.log。
- 新增两个可重复执行的十进程集成用例，额外连续执行三轮，每轮两个用例通过。
- 十个进程同时创建任务，使用同一个临时 SQLite 数据库，验证任务 ID、时间戳、Plugin/GMX 分支及 Job URL 全部唯一；十个槽位满后拒绝第十一个任务，释放一个槽位后允许新任务。
- 十个真实 Worker 子进程分别持有自己的锁，同时执行本地模拟 ci-script upgrade。以屏障确认十个升级脚本确实重叠运行，然后释放并执行各自 bootstrap。验证每个工作目录、升级顺序、日志和输出文件；同任务重复 Worker 被拒绝，完成后锁可重新获取。
- 纳入新增用例后的三轮完整回归，每轮 220 项全部通过；Python 编译、Shell 语法及 CLI dry-run 同时通过。完整日志见 verification-logs/round1.txt 至 round3.txt。

## 适用范围

十任务测试仅在临时配置中将 max_parallel 设为 10；应用默认并发上限仍为 2。测试运行的是本地模拟升级和 bootstrap 脚本，没有访问企业 Jenkins、Nexus 或 SSO，没有触发企业构建，也不能证明企业构建与上传在十任务负载下通过。公司现场的脚本升级、网络、会话隔离及 Jenkins 限流仍待验证。

复现：`python3 -m unittest discover -s tests -p test_parallel_ten.py -v`。
