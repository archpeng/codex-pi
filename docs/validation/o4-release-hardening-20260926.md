# O4 发布加固：同候选续发、阶段资源上限与 Python 计数

2026-09-26。O4 在 O3 已接受的候选 `e65ca307e8bb01ed74db8020baf1274ed844fe05` 之上加固三个发布前
缺口：同阶段同候选的 review 事件续发、契约 `resourceLimits` 在 Pi 执行期间的真实约束、以及
`unittest` 日志的可验证计数。本文件记录实现、测试证据与仍然存在的边界；确切最终提交见本轮最终
报告，未安装插件，也不代表 GPT 验收。

## 1. 同候选 review 事件可续发

- 旧行为：ready review 事件的身份只含 phase/contract/candidate/`ready`。事件被机械 supersede 后
  `add_event` 永远拒绝同一身份；同一轮同一候选的证据失效再恢复时无法产生新的 review 事件。
- 修复：看板卡上持久保存 `reviewEpisode`（整数，默认 0）。只有 pending `review_required` 被失效
  取代时才递增，ready 指纹为 `...:ready:<episode>`。因此：
  - 同一 ready 状态的重复刷新保持幂等（同一身份，不新增事件）；
  - 证据失效后恢复（新的合法回执，或日志短暂不可读后字节不变地恢复）最多续发一条新 review；
  - 旧事件保持 handled/superseded；`decide accept` 只能绑定新事件。
- 证据：`tests/test_phase.py::test_review_event_renews_after_invalidation_with_a_fresh_receipt`
  与 `::test_review_event_renews_after_transient_unknown_log_recovers` 使用真实 Git 候选、真实
  回执文件、真实看板与 `decide`，同时断言“重复刷新不增加 revision”。
- 边界：episode 只随看板卡持久化；它本身不唤醒 GPT，也不绕过既有 pause/额度/派发语义。

## 2. 阶段资源上限约束 Pi 执行期

- 契约校验：`resourceLimits` 的每个路径在 `start`/`continue` 冻结前即校验必须位于 task worktree
  内，且 worktree 与目标之间的任何组件都不是 symlink；越界、`..` 或 symlink 中间组件直接拒绝，
  不创建任务证据。扫描时再次校验，若运行中变成 symlink 则按 unknown 处理（绝不放行）。
- 执行期观测：supervisor 循环独立于 board 刷新周期，每 ≤60 秒对每个声明路径做一次有界的
  `pi_size.measure` no-follow 测量（测试/运维可用 `CODEX_PI_RESOURCE_SCAN_SECONDS` 覆盖，范围
  0.05–60 秒）；高水位与状态持久保存在 `rounds/<n>/resource.state.json`，Pi 结束后再做一次最终
  测量。
- 已知超限：完整扫描或部分扫描下界超过 `maxBytes` 时设置粘性 `breached` 高水位，只对所属 Pi
  进程组执行终止（SIGTERM→SIGKILL），记录 `resourceBreached`/`resourceReason`，readiness 为
  `not_ready`，看板产生一条 `phase_blocked`（reason `resource_breached`，evidence 带路径、
  max/observed 与依据）。
- 测量未知：不完整或不可读的测量保持 `unknown`，绝不当作预算内。连续未知达到两分钟（测试可用
  `CODEX_PI_RESOURCE_UNKNOWN_SECONDS` 覆盖）即升级：终止所属 Pi 进程组，记录 `resourceUnknown`，
  readiness `not_ready`，看板 `phase_blocked`（reason `resource_unknown`）。短暂未知若在下一次
  完整测量前恢复，可回到 `ok`，但期间绝不 PASS。
- 证据：`::test_declared_phase_resource_breach_stops_pi_and_blocks`（真实写爆 worktree，exit 75
  终止、blocked 事件与高水位）、`::test_declared_phase_resource_unknown_escalates_and_blocks`
  （chmod 0 目录触发持续未知升级）、`::test_phase_contract_rejects_escaping_resource_limit`
  （traversal 与 symlink 父组件在 start 时被拒且无任务证据）。
- 边界：保护的是**声明的路径**与**持续可观测的常规文件写入**；不保证任意外部进程的写入、不保证
  supervisor 自身死亡后的行为，也不监控未声明的路径或最终测量之后产生的文件。没有新增 daemon、
  heartbeat 或模型调用；用户 pause 语义不变。

## 3. `unittest` 计数

- `pi_check` 在已经读取用于哈希的同一份日志字节上解析最后一个
  `Ran N tests in ...s` + `OK|FAILED (...)` 摘要，输出
  `{run, pass, fail, skip, format: "python_unittest_summary"}`；Go verbose 解析保持不变。
- 计数规则沿用既有 fail-closed 语义：零运行在声明 `minRun` 时不通过；`skip>0` 在 `forbidSkip` 下为
  `skipped`；failure 为 `failed`；缺失/畸形/歧义摘要不产生 counts，声明计数规则时为 `unknown`，
  绝不假通过。
- 证据：`tests/test_receipts.py::test_python_unittest_counts_positive_failure_and_skip`、
  `::test_python_unittest_zero_and_ambiguous_summaries_stay_explicit`，以及
  `tests/test_phase.py::test_python_count_rules_follow_board_readiness_and_accept`：真实
  `unittest` 回执让 readiness `covered`，`skip`/缺失 counts 分别阻塞 `decide accept`，恢复后新的
  review episode 再被接受。
- 边界：本阶段任务的冻结 helper 不热替换，因此本阶段自身的最终回执可能没有 counts；新解析器由
  使用当前源码的运行测试与独立日志复核证明，而不是替换任务工具目录。

## 与既有阶段的关系

- O3 已接受候选 `e65ca30` 保持不变；O4 是安装前的独立加固阶段。
- O4 提交后仍需要 GPT 主会话按完整交付审查；正式安装、托管缓存刷新与业务任务迁移仍不在 Pi 本
  阶段范围。
- 证据索引（本仓库）：`runtime/pi_task.py`、`runtime/pi_board.py`、`runtime/pi_check.py`、
  `tests/test_phase.py`、`tests/test_receipts.py`；最终干净候选的三项验收回执见本轮最终报告与
  `rounds/3/round.checks/`。
