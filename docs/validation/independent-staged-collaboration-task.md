# V-02：独立分阶段协作验证任务（执行前设计）

状态：未执行；本文件不是通过回执。任务放在全新的本地临时 Git 仓库及其独立 linked worktree，不使用 bakery 业务仓库、不修改本插件源码、不开外部发布。只验证 GPT 设计/验收、DeepSeek Flash 实施、阶段进度、证据与交接的协作机制。

## 总体目标和角色

GPT 主会话只冻结 `DESIGN.md`、`PLAN.md`、任务 brief 与验收命令，启动一次 Pi worker；不写、不修实现文件和测试。Pi worker 固定 `deepseek/deepseek-flash`、thinking `max`，独立完成三个阶段的源代码、测试、普通修复和一个完整结果的本地提交。GPT 只在完成或真正越界阻塞时收取结果、审阅 diff/原始回执和独立验收；若发现缺陷，合并意见后在**同一任务、同一 Pi session/worktree**续接一次返修，不能由 GPT 自己修代码。

准备：GPT 在临时仓库提交仅包含 `DESIGN.md`、`PLAN.md`、`.agents/codex-pi.json`（模型 Flash/max、30 分钟上限、三阶段检查）和忽略 `progress.ndjson`/`__pycache__` 的 `.gitignore` 的冻结基线；建立干净的 linked worktree，任务 brief 存在仓库外。基线不得含实现代码或代写的测试。

测试预算：一个小型 Python 标准库任务，预期 10–20 分钟；worker 执行上限 30 分钟；不接第三方服务、数据库、真实支付/金额或模型嵌套调用。时间预算只是停止并诊断的条件，不是降低验收标准的理由。

## 可执行产品设计：`eventfold`

交付 Python 3 包 `eventfold`，公开 `fold(ndjson_text: str) -> dict[str, int]` 和 `python3 -m eventfold PATH`。输入为 UTF-8 NDJSON，至少一行。每个非空行必须是一个 JSON object，且键**恰好**是 `seq`, `op`, `value`：`seq` 为从 1 开始连续递增的 JSON 整数；`op` 只能是 `add` 或 `subtract`；`value` 为正的 JSON 整数。布尔值不算整数，禁止多余字段、空行、重复/跳号、零值、负数、小数、字符串数值和损坏的 JSON。执行事件时余额初始为 0，`subtract` 不得使余额为负；所有运算只用整数。成功返回恰好 `{"balance": <最终余额>, "events": <事件数>}`。

CLI 只接一个路径参数；成功只输出一行按键排序的 JSON 加换行，退出 0，stderr 为空。输入错误、非法 UTF-8、文件不存在或参数数目错误：退出 2、stdout 为空、stderr 有简短错误且无 traceback。所有行为不依赖当前工作目录以外的隐藏 fixture；不得以写死样例输出满足验收。

## 阶段与进度交接

| 阶段 | DS 独立完成的结果 | 真运行的阶段检查 | 进度条目 |
| --- | --- | --- | --- |
| 1：输入约束 | 解析器及正/负例单测（连续 seq、精确键、类型与损坏行） | `python3 -m unittest discover -s tests -p 'test_parse.py' -v` | 已变更范围、命令/退出码、下一步 |
| 2：状态归约 | `fold` 与纯逻辑单测（含负余额拒绝、多事件汇总） | `python3 -m unittest discover -s tests -p 'test_fold.py' -v` | 同上，失败不得写“完成” |
| 3：真实入口 | CLI、端到端正/负例、完整测试、一次本地提交 | `python3 -m unittest discover -s tests -p 'test_*.py' -v`，再在提交后跑一次 | 提交 SHA、完整检查回执路径、剩余问题 |

一个 `start` brief 覆盖全部三阶段；普通失败由 DS 自行修复，不要求 GPT 每阶段派工。每阶段 DS 在忽略提交的 `progress.ndjson` 追加一条小记录：stage、实际已跑的命令、真实 exit、可定位的收据/日志、next；该记录是进度提示，**不是**通过证明。每阶段检查和提交后完整检查都使用该任务冻结的 `pi_check.py`，保留原始日志/退出码/hash。GPT 等待时只看有界 `status` 和短进度，不拉整段对话或逐函数指挥。插件若不能自动展示阶段，不得把手工进度文件称为插件内建阶段能力。

## GPT 独立验收（预先固定，不交给 DS 作为待实现代码）

1. 核对 worker 的真实模型/思考档位、Pi session、三个阶段的进度与阶段检查回执；阶段进度不得单凭文本算 PASS。核对一个 DS 实施提交、相对冻结基线的完整 diff、主会话未编辑实现/测试、提交后工作树干净。查看所有检查尝试，包括失败与重跑。
2. 核对提交后完整 unittest 实际运行：至少包含解析、归约和 CLI 的正负例，不接受零测试、skip、仅组件测试或没有可验证退出码/hash 的回执。GPT 自己再执行相同完整命令。
3. 用**验收时新建、未提供给 DS 的数据**运行 CLI：`add 15, subtract 6, add 2` 得 `balance=11, events=3`；独立验证负余额、seq 跳号、布尔数值、非法 UTF-8 和不存在文件都退出 2、stdout 为空且无 traceback。验证 stdout JSON 恰好两键且有且仅有一个换行。
4. 若有缺陷，GPT 一次性给出复现、期望与证据，调用同任务 `continue`，由 DS 修复、重跑受影响及完整检查、提交返修；GPT 再验收精确新 HEAD。不为证明续接而捏造缺陷或故意设计无法通过的任务。没有真实缺陷时，续接能力只由插件专门的 lifecycle 测试证明，不记作此任务的实测。
5. 记录两方**分别**的请求数、未缓存输入、缓存输入、输出 token、总耗时与检查次数；无法读取 GPT 侧计数则标为未知。大多数 token 消耗在 DS 侧或一次冒烟成功，不等于相对基线的 GPT 节省；节约量需要与同范围、同验收质量的对照任务比较。

## 协作链结论的三层门槛

- **执行链 PASS**：冻结设计 → 一次派工 → DS 三阶段实际实施/进度/检查/提交 → GPT 独立验收均有原证。只证明这个小任务，不证明长业务阶段。
- **宿主交接 PASS**：额外证明此任务的真实交付事件进入原 Codex 桌面会话、事件去重且用户可继续追问；离线 `result` 或手调 hook/queue 不能代替桌面实测。未装载/未信任相应插件时标 `NOT VERIFIED`，不能用执行链冒充。
- **效率 PASS**：同口径对照下 GPT 实际 token 减少，验收质量没有降低；无对照或缺计数时标 `NOT MEASURED`。

留存路径：临时仓库内 `DESIGN.md`、`PLAN.md`、DS 提交及 Git common dir 的原始任务回执；将结论和各层边界记录到就近验证报告。测试前先核查当前安装版、CLI 身份与是否存在仍活跃的同仓任务；禁止对未知结果重复启动新 worker、替业务任务认定 PASS、推送或发布。
