# 验证记录

## 0.5.1 三次验收失败转由 Codex 实施（2026-09-27）

按同一交付目标的正式质量否决计数，不按Pi工具调用或总轮号。第三次交付未过验收时产生接手事件；普通continue、自动补证和内部worker均不得再启动Pi。原Codex主会话确认writer释放后整体重审方案并直接实施，原生Pi的Flash限制保持，未引入第二个Codex模型。

- [全套241项原始回归](validation/takeover-full-20260927.log)：240通过，1项既有Python计数夹具失败。根因是导入sample_gate生成未跟踪的__pycache__，导致候选receipt正确标dirty并触发缺证续行；没有放宽运行时准入或修改判分。
- 对该夹具设置PYTHONDONTWRITEBYTECODE后，[原测试完整复跑通过](validation/takeover-count-fixture-20260927.log)，仍核对真实通过、skip/失败/未知计数、board与accept的拒绝/恢复。其他240项代码和结果同范围复用，未将初次全量运行改写成全绿。
- [7项接手专项](validation/takeover-focused-20260927.log)通过，包括三个实际进程替身交付轮次后第四轮零启动、重复事件不重复计数、合同变化不清零、外部缺料分类不可重放篡改、历史phase决定、resume/记录裁剪不清接手、自动/内部worker拒绝。全部使用本地进程替身，无真实模型或provider调用。
- Plugin/Skill结构校验与diff检查通过；项目监督Skill、配置和执行合同同步。安装刷新与现有冻结helper采用是不同边界，不能将源码完成冒称所有活动会话已热升级。

## 0.3.0 有界等待与两个真实任务迁移

2026-09-26 的桌面事件暴露 0.2 的缺口：Bake 任务在 11:07:07 收到 `turn/steer`，但执行日志停在 10:02，同步 Stop 持续超过 70 分钟；Pi 的测试仍有推进。0.2 的已完成任务续行验证不能证明等待中可响应追问。0.3 改为有界普通工具等待与快速 Stop，并提供旧等待释放、实时检查标记和低成本状态读取。

Pi `deepseek/deepseek-flash` / max 实施提交 `62a516f`、`e65930d`，由当前 Codex 主会话复核并以 `aaf5984`、`5b9a8bb` 接入 canonical 源码。返修始终续用 `RESPONSIVE-WAIT-20260926` 同一 Pi 会话和工作树。主会话维护文档、版本、集成及迁移，没有调用 Codex CLI 或额外 Codex agent；插件未引入 MCP server 或 heartbeat。

验证范围：

- [初版完整 94 项测试](validation/responsive-initial-94-tests.log)通过，覆盖快速 Stop、完成、失败、取消、超时、重复通知、owner 校验、旧 generation 释放与有界等待。该日志来自返修前的工作树，不冒充最终 revision 的全量回执。随后仅状态读取和相应测试发生返修，复用未受影响的生命周期覆盖；插件实施第 1 轮由主会话主动收束重复全套验证，取消记录保留，不计为验收成功。
- [状态及检查回执 19 项](validation/status-suite-r2-b7c226ab3baa.log)通过；[原始回执](validation/status-suite-r2-b7c226ab3baa.json)的真实退出码和日志 hash 已核对。
- [最终状态读取 14 项](validation/status-suite-r3-715982a03953.log)通过；[原始回执](validation/status-suite-r3-715982a03953.json)的真实退出码和日志 hash 已核对。覆盖未知退出不冒充成功、超大/损坏元数据、输出上限、陈旧运行标记、旧日志不确定性、活动时间及有界 wait 不取消 Pi。
- [实际已安装 0.2 hook 兼容释放](validation/old-installed-hook-release-20260926.json)：先确认旧进程正在等待，再调用新版 release，约 0.07 秒退出，离线 Pi 替身仍存活。该项证明旧代码兼容，不当作桌面任务回复证明。
- 两个真实桌面任务已收到迁移指令。旧 hook PID 36744、71624 均退出；[释放前](validation/live-migration-before-20260926.json)、[释放动作](validation/live-migration-release-20260926.json)和[释放后](validation/live-migration-after-20260926.json)证明两个业务 Pi 的 PID、session、round 与 frozen task hash 保持不变。新 status 读取各约 0.03 秒、输出约 3–6 KB；这是两次观测，不是普遍性能保证。
- [真实任务回复与调用记录](validation/live-migration-responses-20260926.json)：北京时间 11:39，两任务均重新回复并实际读取 canonical skill、调用 canonical `status`。Harness 报告 Room 对话测试仍超时；Bake 报告 PG 分片待核验。证明本次旧等待释放恢复响应，不证明业务验收通过，也不代替新安装 hook 的终态续行宿主验证。
- 插件 manifest、collaborate skill、diff whitespace 校验通过。Bake 的项目约束与生成镜像提交 `96e06c3b2`，`make skills`、`make preflight` 通过；Harness 薄约束提交 `af47fef`，skill 与 diff 校验通过，其他工作区改动保留。

发布候选为 `0.3.0+codex.20260926032910`，`runtime/VERSION` 为 `0.3.0`。截至本次验证，应用安装缓存仍为 `0.2.0+codex.20260926015337`。两个现有主任务已直接采用 canonical 0.3 命令；现有 Pi 的冻结 helper 保持 0.2，新的运行标记随新任务快照生效。应用刷新插件及重新审核/信任 hook 仍待用户在 UI 完成；没有手改托管缓存、信任记录或绕过工具限制。新定义的 Stop 上限为 10 秒，正常路径只检查一次；它不能在主任务闲置后独立唤醒模型。可信定义变更需要重新审核，见 [官方 hooks 说明](https://learn.chatgpt.com/docs/hooks)。


最终结构为 skill → Python 标准库脚本 → 本机 Pi CLI。没有 MCP server、Node 依赖包或 Codex CLI 调用。

## 0.2.0 同步 hook

Pi 实施提交 `bc03a45` 已由 Codex 主会话复核，并以 `8076c8c` 接入本地插件源。
两个提交的运行时代码、hook 配置和测试内容一致。测试使用真实脚本子进程、真实 lifecycle supervisor
与可控 `PI_BIN` 进程；测试中的替身不调用模型。另有一次真实 DeepSeek CLI 冒烟。

| 场景 | 核验行为 |
| --- | --- |
| 完成 | 等待运行中任务结束，生成一次 `decision: block`；收取确认不等于验收 |
| 失败 | 非零退出仍交回终态与结果命令，不冒充成功 |
| 中断 | `Interrupt` 及时暂停交接；锁竞争下仍响应，Pi 保持运行，恢复须显式 `--resume` |
| 超时 | worker 超时交回失败；hook 等待过期保留 Pi 和证据，不自动重新等待 |
| 重复通知 | 并发、重复 Stop 只提供一次通知；重复 ack 幂等，其他会话及通知前 ack 被拒绝 |

[29 项 hook 回归](validation/handoff-regressions-ffb1919e9d63.log)通过；
[回执](validation/handoff-regressions-ffb1919e9d63.json)记录真实退出码、干净 revision 和日志 hash。
最终[完整 81 项测试](validation/full-unittest-suite-bc045a65f844.log)通过，
[完整回执](validation/full-unittest-suite-bc045a65f844.json)的退出码为 0，无超时，测试 revision 为 `bc03a45`。
主会话重新核对日志 hash、测试源码与接入提交的一致性，并在 canonical 插件目录执行真实 Pi 交接冒烟；
不重复运行内容相同的完整测试。插件 validator、skill validator 和 `git diff --check` 均通过。
补充覆盖会话绑定并发、恢复代次、旧 hook 不覆盖已交付状态、遗留进程锁、supervisor 消失、
多个任务中的就绪任务、零等待、错误身份/记录、带空格的实际 hook 命令。
早期测试曾在 worker 写入 `piPid` 前读取该字段；已改为等待真实 PID，
[失败回执](validation/handoff-regressions-5702d1e97669.json)及原始日志保留。

[真实 Pi → hook 冒烟](validation/handoff-real-pi-smoke.json)使用已有 Pi CLI，原始事件报告
`deepseek/deepseek-flash`，`model_check=matched`，任务退出 0 并返回预期文本。
随后执行 `hooks/hooks.json` 中的实际 shell 命令：第一次返回 `block`，重复执行返回 `{}`，
同会话两次 ack 均成功。此测试的 session 是隔离的测试身份，**不冒充桌面宿主的续行**。

宿主端状态：`0.2.0+codex.20260926015337` 已加载，安装缓存的 manifest、hook 定义和运行时代码
与已验证源码一致。用户在应用中完成信任后，[真实桌面续行](validation/handoff-desktop-smoke.json)通过：
应用执行已安装插件的 Stop hook，在同一个 Codex 任务
`01a0da81-3b76-7fd0-aac2-0169cb6de7a1` 生成续行提示；主会话收取真实 DeepSeek 结果并完成 ack，
状态为 `acked`、通知次数为 1。没有人工调用 hook 来模拟该宿主事件。
这次桌面验证使用已完成的真实 Pi 轮次；等待、失败、中断、超时及并发边界由上述进程测试覆盖。

同步 Stop 在宿主处理停止事件期间等待，不提供应用关闭后的自动唤醒，
也不保证通知记录写入后宿主必然收到。没有修改受管理的插件缓存或信任记录。
全过程没有调用 Codex CLI。

## 0.1.0 既有运行时证据

- [52 项行为测试](validation/runtime-tests.log)通过，涵盖配置、进程分离、并发锁、失败原证、取消及进程组清理、同会话续接，以及其他模型配置/冻结任务/内部 worker 的拒绝。
- 主会话复核并复现了重复启动污染旧状态、主检出目录隔离和 supervisor 消失后容量释放问题；修复后相应回归通过。bake 自身为 linked worktree 的配置定位也已验证。
- [真实 DeepSeek 冒烟](validation/smoke.json)：两轮都完成，原始事件报告 `deepseek/deepseek-flash`，第二轮正确复述第一轮随机标记，session 标识相同。只读 fixture 无业务写入；`not_verified` 仍正确表示没有产品验收。
- [独立只读 Pi 规则复核](validation/independent-rules-review.md)为 PASS；其提示的 check helper 文档缺口已补齐，调用语法对照实际 `--help` 核验。
- 插件及 skill validator 通过；bake `make skills`、`make preflight` 通过；harness skill validator 与变更空白检查通过。未运行与文档接入无关的 harness 全量产品测试。
- 最后的 worker 合约文本补充了禁止其他模型、嵌套调用与 fallback；仅文字变化，Python 编译检查通过，沿用仍有效的 52 项行为证据。

两项目的 `project --repo` 实读结果均指向各自 `.agents/codex-pi.json`，实施模型固定为 DeepSeek Flash。
部署到生产、后台自动唤醒 Codex 和业务验收不在这些测试的证明范围内。

本地安装配置：`~/.agents/plugins/marketplace.json` 已登记 `codex-pi@personal`，
策略为 `INSTALLED_BY_DEFAULT`；`~/.codex/config.toml` 已启用插件。旧 skill 已确认加载。
电脑操作工具禁止控制 Codex 应用，用户也禁止调用 Codex CLI，因此新版安装刷新与 hook 信任
由用户在应用中完成；本次已用实际 Stop 续行补充运行证据，skill 可用仍不能代替 hook 运行证据。

原始测试及 smoke 日志位于 `/tmp/pi-collab-research-20260926`，属于可清理的本机临时证据；
关键结果和测试输出已保存在本目录。常规项目运行的原始证据保存在各自 Git common dir 的 `codex-pi/tasks/` 中。
