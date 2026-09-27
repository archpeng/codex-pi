# Codex + Pi

当前 Codex 主任务负责设计、派工与验收；Pi 固定使用 `deepseek/deepseek-flash` / `max` 完成实施。项目只需 `.agents/codex-pi.json` 和自己的领域约束，公共插件维护进程、同会话返修、看板和原始证据。

## 协作方式

1. Codex 给 Pi 一个完整任务段，明确目标、范围、验收和资源上限。
2. Pi 在独立工作树内实施；现有 supervisor 每约 15 秒刷新本地看板。
3. 普通进度和未变化状态不调用主模型。完成、超时、资源超限或需决策的故障，才通过 `codex queue` 向原桌面任务发送精简事件。
4. Codex核验准确轮次、提交和原始回执；同一交付目标三次正式回报未通过后，由原Codex主会话整体重新判断、完善方案并直接实施，不再续派Pi。普通红测、进度或真实外部缺料不计失败轮次。

这里的 Codex CLI 只传递消息，不运行第二个模型。没有新增 MCP、heartbeat 或常驻服务。桌面空闲时可以自动续行；桌面正在对话时，消息等当前轮次结束。主任务可以随时回答用户追问。

看板保留任务目标、当前阶段、执行轮次、候选提交、检查回执、待处理事件和主任务决定。高频刷新由脚本完成；主模型只读有事发生的精简事件，必要时再打开对应日志。详见 [协作规则](skills/collaborate/SKILL.md)、[运行命令](skills/collaborate/references/runtime.md) 和 [交接恢复](skills/collaborate/references/handoff.md)。

## 项目配置

```json
{
  "schemaVersion": 1,
  "model": "deepseek/deepseek-flash",
  "thinking": "max",
  "constraints": ["AGENTS.md"],
  "checks": ["该任务的真实验收命令与项目复核要求"],
  "maxWorkers": 1,
  "timeoutSeconds": 14400
}
```

`checks` 是验收指引，插件不会把它当作通过证明。`maxWorkers` 是容量上限；项目的依赖和并行资格仍由项目约束决定。整轮超时和单个命令的时限分别设置。Pi 退出码 0 仅表示执行结束。

## 故障与费用边界

- 声明了时限、目录和容量上限的检查，由本地脚本执行保护；文件复制保留符号链接。普通测试失败先由 Pi 在范围内修复。
- 中断主任务会暂停自动交接；Pi 是否取消需另行决定。发送结果不明时保留事件，显式恢复，避免重复唤醒。
- supervisor 自身被杀死时不能自行上报；下次正常交互可检查恢复证据。不能承诺任意故障都在两三分钟内发现。
- 每次真正唤醒仍会输入主会话上下文。节省来自取消空轮询和缩小证据读取；不承诺未经对照测量的固定降幅。

## 安装与维护

本机源目录 `/Users/jlpeng/plugins/codex-pi`，注册在个人 marketplace。依赖 Python 3.10+、Git、已有 Pi CLI；自动通知另需支持 `queue` 的 Codex CLI 和桌面应用。认证沿用现有本地配置，插件不复制密钥。运行时适用于 macOS/Linux，使用标准库、文件锁和进程组。

更新通过正式插件安装流程完成；若应用要求重新审核、信任 Hooks，由用户在应用内完成。不得手改缓存或信任记录。运行中任务保留原 helper 快照，在安全终态后采用新版本。已有任务的迁移需验证原会话和工作树没有改变。

测试：`python3 -m unittest discover -s tests -p 'test_*.py' -v`。脚本测试不等于桌面验收；实际入口验证见 [CLI queue 实测](docs/validation/cli-queue-20260926.md)。社区产品比较见 [调查记录](docs/community-research.md)。
