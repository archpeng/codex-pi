# O2 eventfold 完整阶段验证：真实结果与边界

2026-09-26。O2 在本插件之外的受控夹具仓库中执行：Pi（`deepseek/deepseek-flash`，thinking
`max`）实施完整小任务，GPT 主会话冻结设计与阶段契约并独立审查。本文件记录该阶段的真实结果、
成本观测与限制，供 0.5 发布准备引用；不复制长日志，也不修改 O2 仓库。

## 阶段身份与候选

- 阶段 `O2-EVENTFOLD`；契约 SHA-256
  `2c0dd4ea8682664d8889b64a22502eeaf0b81bd7ce746ab89b37c82945387905`；基线
  `95310f442b1eec92b9919f98491d889b8d2e234b`；范围 `eventfold/`、`tests/`；预算 1800 秒、
  命令超时 120 秒、worktree 10 MiB 资源上限。
- 设计 `DESIGN.md` SHA-256
  `fa2f6feea2de99bcba52a1d4e6b9d37ad24e66e3f2486fc8857e0bd2e1904f84`。
- R1 候选 `710d58d1adcb4e9d68a99574de3f2515a2e708bd`：Pi 单提交，R1 回执 parse=54、
  fold=28、full=110 项测试，exit 0。GPT 独立审查用合法 4301 位正整数复现 CLI traceback，
  看板以 `changes_requested` 拒绝并绑定该 reviewedHead。
- R2 候选 `343a87935a9ec4d70646de151bcfa88f618e6a6b`：同一 Pi session/worktree 在 R2 修复
  （`_intconv.py` 分块解析/渲染，不改全局 digit 限制；解析、fold、CLI 与测试相应适配）。
  GPT 随后在同一契约 SHA 下接受：看板 `phase.status=accepted`、`acceptedHead=343a879...`、
  `codex.review=accepted`。

## 最终候选检查（Pi 冻结回执）

R2 干净 HEAD 上的三项验收；所有回执 `head=343a879...`、`dirty=false`、`exit_code=0`、
日志哈希已核对、无 skip：

| 验收项 | 命令 | 结果 |
| --- | --- | --- |
| parse_final | `python3 -m unittest discover -s tests -p 'test_parse.py' -v` | 58 tests, OK |
| fold_final | `python3 -m unittest discover -s tests -p 'test_fold.py' -v` | 35 tests, OK |
| full_final | `python3 -m unittest discover -s tests -p 'test_*.py' -v` | 127 tests, OK |

GPT 主会话还独立复跑 127 项测试，并执行隐藏的正常/错误 CLI 与 4301 位整数用例；这些是主会话
提供的测量，不是 Pi 在本阶段独立推导。

## 事件交接观察

- 看板使用 `cli-queue`，owner/desktop 任务为 `01a0da81-3b76-7fd0-aac2-0169cb6de7a1`；
  `board.queue.json` 记录 3 个 packet（进度回音 `bc5ec450...`、R1 交付 `3fa8b320...`、R2 交付
  `687bc064...`）均由 queue 以 exit 0 排入同一桌面任务。
- 真实观察：一次进度回音真实送达；R1/R2 完成事件都产生并处理（R1 `changes_requested`、
  R2 `accepted`），决定记录在同一看板卡；后续里程碑按 10 分钟间隔规则合并/压制，符合反噪音
  设计。未观察项：桌面读取延迟、长时间运行下的送达稳定性、O3 安装后的宿主行为。

## 成本记录（避免误读）

Pi 使用量取自 `rounds/1`、`rounds/2` 的 `round.summary.json`（两轮均 `usage_complete=true`，
全部 41+25 turns 报告固定模型 `deepseek/deepseek-flash`）：

| 轮次 | turns | input | cache read | cache write | output |
| --- | --- | --- | --- | --- | --- |
| R1 | 41 | 29,010 | 1,646,720 | 0 | 40,356 |
| R2 | 25 | 12,418 | 2,402,560 | 0 | 34,067 |
| 合计 | 66 | 41,428 | 4,049,280 | 0 | 74,423 |

- 两轮 wall-clock 分别约 173.1 秒与 148.5 秒，合计约 321.6 秒（brief 记录约 321.5 秒）；
  轮间调度间隔约 108.1 秒不计入该合计。
- GPT 主会话记录（主会话提供的测量，Pi 未独立推导）：O2 起始到接受窗口内 20 条 response 记录、
  四个 turn ID、input 2,101,476（其中 cached input 2,066,944）、output 8,560（reasoning 子集
  2,876）、uncached input 34,532。
- cache read 不是增量成本；本记录不做美元换算、不与其它基线对比、不声称固定节约比例。

## 限制

- O2 未运行长时间业务工作负载，也未证明固定百分比的 GPT 节约；以上只是这一受控两轮夹具的
  机制观测。
- R1 的失败与其被拒候选保留在证据中，只有 R2 被接受；Pi 轮次摘要按设计仍自报
  `acceptance=not_verified`，接受来自 GPT。
- 事件交接证明 queue 排入与看板决定，不证明用户界面读取延迟或长期稳定性。
- O2 仓库为只读参考；本插件阶段不修改它。

## 证据索引（只读路径）

- 任务目录：`/private/tmp/codex-pi-o2-eventfold-20260926/repo/.git/codex-pi/tasks/CODEX-PI-O2-EVENTFOLD-20260926/`
- 契约与状态：`phase.json`、`phase.state.json`
- R1 回执：`rounds/1/round.checks/{parse_final-1c64aa17a4bf,fold_final-a4f608d621be,full_final-a2a144c9fff8}.{json,log}`
- R2 回执：`rounds/2/round.checks/{parse_final-21594238c791,fold_final-9ee55cf7a8f1,full_final-40ae43e369a4}.{json,log}`
- 进度与轮次：`rounds/{1,2}/progress.json`、`rounds/{1,2}/round.summary.json`、`rounds/{1,2}/round.meta`
- 看板与队列：`/private/tmp/codex-pi-o2-eventfold-20260926/repo/.git/codex-pi/board.json`、`board.queue.json`
- 设计与工作树：`/private/tmp/codex-pi-o2-eventfold-20260926/worktree/DESIGN.md`、`PLAN.md`

## 附：O3 发布准备的机械校验（本仓库）

最终候选为本文件所在提交；确切 SHA 由本轮最终报告与 `round.meta` 的 `endHead` 给出。校验命令、
限额与实际回执如下：

| id | 命令 | 限额 | 结果与冻结回执 |
| --- | --- | --- | --- |
| full_suite | `python3 -m unittest discover -s tests -p 'test_*.py' -v` | `pi_check --timeout-seconds 14400`；本仓库套件实测约 368 秒，高于契约声明的 180 秒命令超时，故按 brief 的 helper 模板使用 14400 秒；无目录预算（`resourceLimits=[]`） | 预期 219 tests OK（基线 `7331da9` 的 O1 R6 回执已为 219 tests / 368.0 秒 / 干净 HEAD）；最终提交后复跑，回执见 `rounds/1/round.checks/full_suite-*.{json,log}` |
| plugin_validate | `python3 /Users/jlpeng/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py /private/tmp/codex-pi-o3-release-prep-20260926` | `pi_check --timeout-seconds 14400` | 基线预验证 exit 0、输出 `Plugin validation passed`；最终提交后复跑，回执见 `rounds/1/round.checks/plugin_validate-*.{json,log}` |
| diff_check | `git diff --check`；`git diff --check 7331da9..HEAD` | 直接运行 | 预期 exit 0、无输出（提交后在干净 HEAD 上运行） |

上述 `round.checks` 目录指
`/Users/jlpeng/plugins/codex-pi/.git/codex-pi/tasks/CODEX-PI-O3-RELEASE-PREP-20260926/rounds/1/round.checks/`；
`pi_check` 每次运行生成带 nonce 的确切文件名，本轮最终报告列出其完整路径。`exit 0` 只表示执行
结束，不代表 GPT 验收。
