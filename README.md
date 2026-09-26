# Codex + Pi

个人本地插件：当前 Codex 主会话负责设计、派工与验收，Pi 使用
`deepseek/deepseek-flash` / `max` 实施完整任务段。本机制只允许此模型；
其他模型配置或旧任务模型将被拒绝，不自动回退。公共运行时负责进程、会话、
原始证据和精简结果；各项目只维护 `.agents/codex-pi.json` 与自己的领域约束。

## 使用

在已加载插件的 Codex 任务中说：

> 用 `$collaborate` 按当前 PLAN 委派 Pi 实施；先确认项目约束，集中验收结果。

插件提供 `project`、`start`、`continue`、`result`、`wait`、`cancel` 六个脚本命令。任务使用独立 Git worktree；返修续接同一 Pi 会话。
详见 [协作规则](skills/collaborate/SKILL.md) 和 [运行说明](skills/collaborate/references/runtime.md)。

每个新项目增加以下薄配置，并将实际验收要求写入 `checks`：

```json
{
  "schemaVersion": 1,
  "model": "deepseek/deepseek-flash",
  "thinking": "max",
  "constraints": ["AGENTS.md"],
  "checks": ["当前任务的真实验收命令与独立复核要求"],
  "maxWorkers": 1,
  "timeoutSeconds": 14400
}
```

`maxWorkers` 只是容量上限，不自动授权并行任务。插件不执行 `checks` 中的自然语言，
由 Pi 按任务运行检查，Codex 核验原始回执。Pi 的进程退出成功不等于产品验收通过。

## 本机安装与维护

本机源目录为 `/Users/jlpeng/plugins/codex-pi`，通过个人 marketplace 注册。
插件通过 shell 直接调用公共 Python 脚本，再启动已有 Pi CLI。没有 MCP 配置，
没有额外的 Node 依赖，不含模型密钥；Pi 使用已有本地认证。运行中的任务使用自己的 Python helper 快照，更新插件不热改既有运行。

公共脚本依赖 Python 3.10+、Git、Pi（Pi 自身仍使用原安装的 Node）；当前适用于 macOS/Linux（使用 `flock` 和进程组）。
行为验证：`python3 -m unittest discover -s tests -p 'test_*.py' -v`，无需安装 Python 第三方包。

安装是否成功应以应用能加载 `collaborate` skill，且脚本能够真实启动 Pi为准。仅写入 marketplace
不证明当前会话已经热加载；应用可能需要刷新插件或重启。全过程不需要 Codex CLI。

## Token 与恢复边界

- 一次交付完整任务段；合并返修意见，减少 Codex 的长上下文回合。
- 首先读取有上限的结果；按问题读取局部 diff、日志和原证，不反复拉回整段 Pi 日志。
- `wait` 最长等待 60 秒，软件在内部等待；禁止用密集模型轮询替代。
- 启动命令退出后，独立 Pi worker 继续运行。主会话已经结束时，插件没有经过验证的
  自动唤醒通道；需要下一次主会话读取结果，不启动额外 Codex、心跳或轮询任务。
- 每轮日志、失败结果和检查回执保留在 Git common dir 下；未确定状态不能自动重跑或认定成功。

本插件减少可避免的协调输入，不承诺固定的 token 降幅。原始、缓存、输出与等效成本必须
按相同阶段范围分别统计，不能把订阅额度直接当作 API 美元费用。

## 社区调查

现有 `pi-delegate-mcp`、`pi-mcp-server` 和 `pi-subagents` 已有相近能力。
本插件复用本机已验证的 Pi 生命周期实现，保留持久运行、同会话返修、项目约束与原证，通过直接 CLI 使用。
选择依据及局限见 [调查记录](docs/community-research.md)。
