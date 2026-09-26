# 验证记录（2026-09-26）

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
