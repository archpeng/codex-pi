# Codex + Pi

个人本地插件：当前 Codex 主会话负责设计、派工与验收，Pi 使用
`deepseek/deepseek-flash` / `max` 实施完整任务段。本机制只允许此模型；
其他模型配置或旧任务模型将被拒绝，不自动回退。公共运行时负责进程、会话、
原始证据和精简结果；各项目只维护 `.agents/codex-pi.json` 与自己的领域约束。

## 使用

在已加载插件的 Codex 任务中说：

> 用 `$collaborate` 按当前 PLAN 委派 Pi 实施；先确认项目约束，集中验收结果。

任务运行器提供 `project`、`start`、`continue`、`status`、`result`、`wait`、`cancel`。主任务通过最多 60 秒一段的普通工具调用等待，期间优先处理用户追问；Stop hook 只检查已经结束的轮次，不再长时间占住会话。任务使用独立 Git worktree；返修续接同一 Pi 会话。
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

插件原有 `collaborate` skill 已在主会话加载，脚本也已真实运行 DeepSeek Flash。
更新版本后，仍须由应用刷新插件并审核、信任当前 hook 定义；旧 skill 可用不证明新 hook 已加载。
全过程不需要 Codex CLI，也不手改安装缓存或 hook 信任记录。

## 有界等待与快速交接

0.3 用普通工具等待取代 0.2 的数小时同步 Stop 等待。`status` 提供当前检查、日志更新时间和失败回执的紧凑快照；`wait` 每次最多 60 秒，返回后主任务处理新输入，再决定继续等待或诊断。Pi 始终作为独立进程运行，不因等待超时而取消。

Stop hook 仅在已有终态时提供去重的交接提示，不承担后台调度。旧的活动 hook 通过 owner-scoped `release` 释放，保留原 Pi 会话、工作树和证据。详见 [交接与迁移说明](skills/collaborate/references/handoff.md)。

插件刷新及 hook 信任由应用管理；更新源码不会热替换旧 hook 进程或运行中任务的冻结工具。桌面追问响应与单元测试分别验证，不能以进程存活、文件增长或 exit 0 替代实际推进和验收。

## Token 与恢复边界

- 一次交付完整任务段；合并返修意见，减少 Codex 的长上下文回合。
- 首先读取有上限的结果；按问题读取局部 diff、日志和原证，不反复拉回整段 Pi 日志。
- `wait` 最长等待 60 秒，软件在内部等待；每次调用之间处理用户输入，不读取整段历史、不输出重复长报告。
- 启动命令退出后，独立 Pi worker 继续运行。Stop hook 快查即退；主任务若已结束，不承诺后续自动唤醒。
  有界工具等待会产生协调 token，换取处理用户追问的机会；不启动额外 Codex 或心跳任务。
- 每轮日志、失败结果和检查回执保留在 Git common dir 下；未确定状态不能自动重跑或认定成功。

本插件减少可避免的协调输入，不承诺固定的 token 降幅。原始、缓存、输出与等效成本必须
按相同阶段范围分别统计，不能把订阅额度直接当作 API 美元费用。

## 社区调查

现有 `pi-delegate-mcp`、`pi-mcp-server` 和 `pi-subagents` 已有相近能力。
本插件复用本机已验证的 Pi 生命周期实现，保留持久运行、同会话返修、项目约束与原证，通过直接 CLI 使用。
选择依据及局限见 [调查记录](docs/community-research.md)。
