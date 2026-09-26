复核完成：已重读 README、plugin.json、公共 SKILL、runtime reference、两个项目 skill/config、harness memory-policy 开头；仅做存在性与一致性核验，未读 runtime 实现、未执行命令。

## Findings

**1. `skills/collaborate/SKILL.md` 引用的 check helper 缺公开调用语法（低影响）**
- 现象：SKILL 要求"Use the bundled check helper for actual acceptance commands; it records real exit and log hashes"，但 README、runtime.md 的 Commands 段和 SKILL 均未给出该 helper（`runtime/pi_check.py`，文件存在）的调用命令；README 只列 `project/start/continue/result/wait/cancel` 六个命令。
- 影响：主会话若照规则执行验收，可能猜参数或退回手跑命令、丢失 exit/log hash 回执。属文档补全问题，不影响新入口主干可用。

**2. 残留措辞（不构成误用）**
- `SKILL.md` 中 "transport response" 为 MCP 时期措辞，新 shell 流程下无对应物；语义仍落在"任何命令输出都不是验收结论"，不会误导模型换模型或回退 MCP。

## 通过的核验点

- **命令语法自洽**：README 六命令与 runtime.md 示例一一对应；`project --repo`、`start --repo/--task/--worktree/--prompt-file[/--read-only]`、`wait --timeout-ms 60000`、`result`、`continue --prompt-file`、`cancel` 在 SKILL 与 runtime.md 间无冲突；wait 上限 60 秒一致。
- **无 MCP/Node 旧承诺**：`.mcp.json`、`node_modules/`、`package*.json` 均已消失；plugin.json 无 `mcpServers` 字段；README/runtime 明确"shell → Python 脚本 → Pi CLI、无 MCP、无额外 Node 依赖"，且准确注明"Pi 自身仍使用原安装的 Node"。README 中 `pi-delegate-mcp`、`pi-mcp-server` 仅出现在社区调查段（调查对象）。
- **模型硬约束一致无退路**：README"只允许此模型、其他配置/旧任务模型拒绝、不自动回退"；SKILL"所有 Pi worker 实施/调查/复核 MUST use deepseek/deepseek-flash，禁 nested model calls 与 automatic fallback"；runtime reference"only allowed model，其他 project model 或 frozen task model 在启动 Pi 前拒绝"；两个项目 skill 各有一段同等约束；两份 config 均 `"model": "deepseek/deepseek-flash"`。不存在"按项目显式选择其他模型"的旧措辞。
- **不执行 Codex CLI**：公共 SKILL 明确覆盖安装/恢复/helper/legacy runner；两项目 skill 均保留该禁令与"不自动唤醒/不轮询"边界。
- **入口与恢复边界**：harness memory-policy 开头已改为公共脚本入口，旧 runner 段落明确降为历史；README 不自称已热加载（"仅写入 marketplace 不证明当前会话已经热加载"），安装注册事实仍需单独核验。
- **实际文件**：`runtime/pi_task.py`、`pi_check.py`、`pi_summary.py`、`tests/test_*.py`、Pi 绝对路径均存在；`scripts/` 为空目录且未被任何文档引用，无影响。

PASS
