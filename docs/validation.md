# 验证记录（2026-09-26）

最终结构为 skill → Python 标准库脚本 → 本机 Pi CLI。没有 MCP server、Node 依赖包或 Codex CLI 调用。

- [52 项行为测试](validation/runtime-tests.log)通过，涵盖配置、进程分离、并发锁、失败原证、取消及进程组清理、同会话续接，以及其他模型配置/冻结任务/内部 worker 的拒绝。
- 主会话复核并复现了重复启动污染旧状态、主检出目录隔离和 supervisor 消失后容量释放问题；修复后相应回归通过。bake 自身为 linked worktree 的配置定位也已验证。
- [真实 DeepSeek 冒烟](validation/smoke.json)：两轮都完成，原始事件报告 `deepseek/deepseek-flash`，第二轮正确复述第一轮随机标记，session 标识相同。只读 fixture 无业务写入；`not_verified` 仍正确表示没有产品验收。
- [独立只读 Pi 规则复核](validation/independent-rules-review.md)为 PASS；其提示的 check helper 文档缺口已补齐，调用语法对照实际 `--help` 核验。
- 插件及 skill validator 通过；bake `make skills`、`make preflight` 通过；harness skill validator 与变更空白检查通过。未运行与文档接入无关的 harness 全量产品测试。
- 最后的 worker 合约文本补充了禁止其他模型、嵌套调用与 fallback；仅文字变化，Python 编译检查通过，沿用仍有效的 52 项行为证据。

两项目的 `project --repo` 实读结果均指向各自 `.agents/codex-pi.json`，实施模型固定为 DeepSeek Flash。
部署到生产、后台自动唤醒 Codex 和业务验收不在这些测试的证明范围内。

本地安装配置：`~/.agents/plugins/marketplace.json` 已登记 `codex-pi@personal`，
策略为 `INSTALLED_BY_DEFAULT`；`~/.codex/config.toml` 已启用插件。当前应用的安装/加载尚未确认。
电脑操作工具禁止控制 Codex 应用，用户也禁止调用 Codex CLI，因此不能据此宣称 UI 安装已完成。
从个人插件市场打开插件并完成安装/刷新，在新任务中确认 `collaborate` 可用即可核验加载。

原始测试及 smoke 日志位于 `/tmp/pi-collab-research-20260926`，属于可清理的本机临时证据；
关键结果和测试输出已保存在本目录。常规项目运行的原始证据保存在各自 Git common dir 的 `codex-pi/tasks/` 中。
