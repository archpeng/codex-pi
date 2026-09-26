# O1 阶段自主执行闭环：实现与验证边界

2026-09-26。本文件描述 0.5.0 源码候选新增的完整阶段闭环，并明确区分**已实现并已有确定性测试**
与**尚未验证、留给 O2/O3 的事项**。脚本不判断设计质量，不代写验收；`exit 0` 只表示执行结束，
`readiness=ready` 只表示“可以交给 GPT 验收”，不是 PASS。

## 已实现（本阶段源码）

| 验收项 | 实现 | 入口与主要落盘 |
| --- | --- | --- |
| O1-1 阶段约定 | `runtime/pi_phase.py` 校验 schemaVersion/必填字段/ID/引用与设计 hash；`pi_task.py start --contract-file` 冻结到 `tasks/<task>/phase.json`，并写入 `phase.state.json` 预算锚点；brief 只引用契约与验收项，不复制设计 | `phase.json`、`phase.state.json`、`rounds/<n>/brief.md` |
| O1-2 结构化进度 | `pi_task.py progress` 短命令：`--activity/--step/--completed-criteria/--next/--blocker/--evidence-ref`，输入校验、短锁、原子写；`--show` 只读；自报 `verified=false`，与 supervisor 观察/回执分开。每个完整阶段最多两次非阻塞进度回音（默认间隔 ≥10 分钟），普通/重复写入不排队；里程碑、合并、暂停与额度语义见下文 | `rounds/<n>/progress.json`、board card `progress/notify` |
| O1-3 就绪检查 | `pi_task.build_phase_snapshot` 是唯一的规范化阶段证据快照：候选身份（`known(head, source)` / `unknown(reason)`，active 必须由有界真实 HEAD 探测，terminal 必须 exact `endHead`，不回退 startHead）、逐项证据等级（Pi 自报 / 进程活动 / 回执元数据 / 候选匹配且日志哈希验证 / 机械就绪 / GPT 接受分开表达）、scope、执行事实与合取式 readiness。执行门与证据门同源：只有 `state=completed`、`exitCode=0`、`cancelled=false`、`timedOut=false`、必需项全部 covered、scope 完整通过且扫描完整才为 `ready`；已知异常终态/非零退出/超时/取消为 `not_ready`，退出码或标志缺失/矛盾为 `unknown`。回执缺项/失败/跳过/未知、日志缺失或篡改、计数缺失均 fail closed；`covered` 必须候选匹配 + log hash 验证 + 计数规则全部满足。短看板、进度/终态事件、`readiness` 命令与 `decide accept` 都消费同一快照；不另建推断 | `rounds/<n>/readiness.json`（终态）/ 内存投影；board `phase.evidence` |
| O1-4 事件分类 | 有阶段契约时，运行中的检查失败/超时/资源问题只留本地回执，不生成 queue 事件；终态只有 delivery-ready、真正需决策的阻塞、超时或所有权异常才产生事件；事件指纹含 phase/contract/candidate/reason；陈旧事件被机械标记 superseded 且不投递 | board card `phase/progress`、`handled` 中的 `superseded` |
| O1-5 一次自动补齐 | 仅当 Pi 正常退出、缺项可机械列出、无未解决必需检查失败、阶段未暂停、总预算仍允许时，同一 supervisor 在同一 session/worktree 内续接一轮；配额按 phaseId 持久化在 `phase-auto.json`，重复刷新/并发/崩溃不重复占额；启动结果未知标记 unknown 并升级，不猜测重试 | `phase-auto.json`、`phase.state.json.autoContinue` |
| O1-6 阶段验收门 | `pi_board.py decide --decision accept` 对阶段事件强制 `--phase` 与 `--contract-hash`，并要求 readiness 投影 ready、无 worker/supervisor 锁；旧候选/旧契约/已处理事件拒绝；`continue --contract-file` 切换到新 phaseId 前要求 board 上旧阶段已 accepted 且 acceptedHead 等于最新候选 | board card `phase.status/acceptedHead`、continue 错误信息 |
| O1-7 分工与文档 | 源码、测试、文档、提交均由 Pi 完成；本文档与 `references/runtime.md` 标示 O2/O3 待验证 | 本文件 |

### 阶段契约最小字段

必填：`schemaVersion=1`、`phaseId`、`goal`、`result`、`baseline`、`scope`（相对路径列表）、
`designRef`、`designSha256`、`acceptanceItems`、`budgetSeconds`、`commandTimeoutSeconds`、
`resourceLimits`（可为空列表，显式声明无目录预算）、`autonomousRepair`、`escalateWhen`。
可选：`preconditions`、`nextPhaseRef`。
验收项字段：`id`、`description`、`checkId`（默认同 id）、`command`、`passCondition`、`evidence`、
`minRun`、`forbidSkip`。校验只做形状、引用与版本一致性，不执行 `command`，不评价设计好坏。
设计文件 hash 与基线 commit 在派工时解析；设计变更必须换 `contractSha256`，旧候选的验收决定不沿用。

### 常用命令

```sh
# 派发一个有契约的阶段
python3 runtime/pi_task.py start --repo REPO --task TASK --worktree WT \
    --prompt-file brief.md --contract-file phase-contract.json

# Pi 的结构化进度（快照 helper 内也可用）
python3 runtime/pi_task.py progress --repo WT --task TASK --round 1 \
    --activity checking --step '...' --completed-criteria A1 --next '...' --evidence-ref path

# 机械就绪检查（只读）
python3 runtime/pi_task.py readiness --repo REPO --task TASK --round 1

# 阶段投影（预算、配额、进度、readiness、board 验收）
python3 runtime/pi_task.py phase-status --repo REPO --task TASK

# GPT 阶段验收：必须绑定契约与候选
python3 runtime/pi_board.py decide --repo REPO --task TASK --event-id EVENT \
    --decision accept --reviewed-head FULL_SHA --phase PHASE --contract-hash HASH

# 下一阶段派工（前一阶段未 accepted 会被拒绝）
python3 runtime/pi_task.py continue --repo REPO --task TASK \
    --contract-file next-phase.json --prompt-file repair.md
```

### 进度回音与异常可见性（同阶段增补）

- 里程碑只取自真实事实：候选绑定的通过回执（`verified_receipt`，必须同时通过 exit/timeout/cancel、候选 head/dirty、log 内容哈希核对，缺失或篡改日志不得触发）、带证据引用的 `checking/repairing` 进度（`pi_self_report_unverified`），或持续超过异常阈值且没有可验证修复进展的失败事实（`observed_receipt`，低优先级“修复中”）。普通 `implementing` 叙述、重复写入、日志增长都不是里程碑，不排队。
- active round 的当前候选由有界只读 `git rev-parse HEAD` 核对并投影为 `currentHead`；阶段内提交后，新 HEAD 的回执/进度才被视为当前。若 active HEAD 探测失败，候选身份为未知（不回退 startHead），不发证据进度回音也不把旧回执/旧事件表述为当前；终态以 exact `endHead` 为准。短看板 `evidence.candidateHead` 与 `phase.candidate` 同源，全部来自 `pi_task.normalize_candidate`。
- `decide accept` 在决定前会用 `build_status` 重新读取实时状态，逐一核对 round、contract revision、候选、readiness、worktree 当前 HEAD 与 worker/supervisor 锁，并与储存在 board 上的投影比较；任何一项不一致（包括 board 未刷新、终态后又有新提交、契约换版或新 round）都拒绝，不依赖可能陈旧的看板缓存。
- 每个阶段最多两次进度回音；两次之间默认至少 10 分钟（可用 `CODEX_PI_PROGRESS_NOTIFY_SECONDS` 调整，仅运维/测试）。额度与 `lastAt` 持久在 board card、按 `phaseId` 计数，不随 round、supervisor 重启或重复刷新重置；忙时新里程碑合入尚未投递的同阶段进度事件，不追发。
- 用户 pause（card 或 Interrupt route）同时阻止进度回音、自动续接和显式 `continue`；普通对话不解除 pause。pause 在决策与启动之间到达时 fail closed：不启动补齐轮，配额保守保留并升级为 `phase_blocked`。
- 需要决策或可最终验收的 `review_required`/`phase_blocked` 事件不占进度额度，也不受 10 分钟间隔压制；进度事件仍走既有 queue `uncertain` 语义，失败不自动重发。
- 已声明的命令超时/资源上限/检查启动失败仍由 `pi_check` 有界保护；可在 supervisor 存活时数分钟内观察。持续超过默认 3 分钟且无修复进展的同类失败只回音一次，后续同样失败不重复提示。

### 自动补齐与事件边界

- 自动补齐条件（全部满足）：上一轮 `state=completed` 且 `exitCode=0`；readiness 缺项仅为
  “没有适用于当前候选的回执”；无 failed/skipped/unknown 验收项；scope 检查无越界；阶段未被
  `pi_board pause` 或 Interrupt route 暂停；剩余预算大于下限；`phase-auto.json` 中该 phaseId
  尚未占用。
- 补齐轮只传缺项与设计引用（`compose_gap_prompt`），复用同一 `sessionId/sessionDir/worktree`，
  不重置 `phase.state.deadlineAt`，不跨阶段。
- 第二次仍缺项、必需检查失败、预算耗尽、非正常退出、暂停、配额/启动结果未知，都产生
  `phase_blocked` 事件交主会话；`decide` 可用 `changes_requested` 走原 Pi 会话返修。
- 运行中的普通失败/单次超时留在本地；已排队的旧事件在 `decide` 时按 phase/contract/candidate/round
  判定陈旧并拒绝，`dispatch` 也不会投递陈旧阶段事件。队列发送的 `uncertain` 保守语义未改动。

### 兼容与边界

- 不带 `--contract-file` 的旧任务完全走 0.4 路径：无 phase 投影、无自动补齐、`review_required`
  与 `decide`（无需 `--phase`）行为不变；`readiness` 返回 `no_contract`。暂停和显式 `continue` 的
  pause 门适用于所有已注册任务（包括旧任务）。
- `forbidSkip`/`minRun` 只适用于能产出可解析计数的检查（当前 `pi_check` 解析 Go 顶层计数）。
  非测试命令（如 `git diff --check`、lint）不要声明这两项：未声明时 exit 0 + 已校验 log 即为
  通过；声明后回执无解析计数则记为 unknown 并阻止就绪，不会把缺失计数写成 0 skip。
- scope diff 覆盖全部变更文件；超过 `MAX_SCOPE_DIFF_FILES=600` 时返回 unknown 并阻止就绪，
  不对前缀做“部分通过”。
- 自动补齐只针对“进程已退出后的机械缺项”，不限制阶段内 DS 自行修复次数；也不启动下一阶段。
- 预算以 phaseId 为界，`phase.state.startedAt` 不因续接/返修重置；同 phase 的新契约修订保留
  原启动时间（正文预算取新契约值，需 GPT 显式换版）。
- 本机源代码版本为 0.5.0；尚未安装、发布或迁移任何托管缓存与业务任务。

## 尚未验证（O2/O3 待办）

- **真实小任务与成本归因（O2）**：本阶段只用确定性替身验证机制，未运行真实 DS 小任务，也未
  对照统计 GPT/DS 的输入、缓存输入、输出、请求数与耗时。不得用 DS token 占比代替 GPT 实际节约。
- **真实桌面进度回音**：进度回音仅通过确定性隔离测试与 board/queue 投影验证；本阶段没有自然的
  主会话里程碑，未制造额外模型调用，因此“真实送达桌面并被主会话读到”标为未实测。
- **长期成本下降幅度**：未做同范围同质量基线的对照测量，未验证长期降幅。
- **发布与项目接入（O3）**：未走正式插件安装流程，未验证两个业务任务的迁移、helper hash 与
  原 session/worktree 保持；活跃 worker 热替换未做。
- **真实 queue 传输**：沿用 0.4 已验证的忙/闲桌面与 uncertain 恢复证据；本阶段未重复烧模型
  验证传输层（也没有改变传输层）。
- **复杂多阶段连续运行**：只验证了门禁与一次补齐；多阶段长链、预算边缘、用户中途指令等需要
  在 O2 的受控两阶段夹具中继续验证。

## 原始证据与使用量指针

- 测试：`tests/test_phase.py`（确定性行为）、既有 `tests/test_*.py`；完整套件运行结果与
  `pi_check.py` 回执见本轮 `round.checks/` 与任务目录，不在本文件复制长日志。
- 本阶段实施会话的 Pi usage 在对应 `rounds/<n>/round.summary.json` 中（`usage_complete` 为真时
  才可用）；GPT 侧使用量由主会话自己的计录提供，本文件不合成、不估算。
- 设计权威：`docs/kanban-coordination-proposal.md`，SHA-256
  `c26f289e1218d5e6a4041f8a901cb78fcd93286d985d05ea5c37993bf5295e05`。
