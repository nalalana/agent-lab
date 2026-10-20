# Agent 架构研究与评测

基线：`https://github.com/kuibu/touming.git` 的 `develop`，2026-10-20 获取，提交
`e67c05a81d753d0d06c43c96872d28854e1af60a`。保留当前后端代码与业务依赖，实验主分支为 `main`。
上游历史含密钥，被 GitHub 推送保护拒绝；独立仓库使用不继承历史的代码快照，并排除本地 `backend/properties.py` 配置。
`origin` 为 [nalalana/agent-lab](https://github.com/nalalana/agent-lab)；`upstream` 仅用于同步原后端，不向 upstream 推送实验。

## 当前能运行的内容

从仓库根目录执行（使用已安装后端依赖的 Python）：

```powershell
python lab/run_baseline.py --repeat 3
python lab/run_baseline.py --suite apps.agent_runtime.tests.test_response_verification
```

入口直接测试生产执行图、工具校验、权限、响应核验，不启动 Web、队列、数据库服务器或沙箱服务。
只用最小 Django 设置和现有测试依赖；新环境先在独立虚拟环境安装 `backend/requirements.txt`。
结果写入 `lab/results/baseline.json`，包括源码版本、工作区状态、逐条用时、失败详情。
这属于脚本化模型的程序回归，**不能作为真实模型的幻觉率或公开基准成绩**。

## 真实模型业务评测

`run_eval.py` 使用生产 `Phase2AgentRuntimeFacade`、模型网关、请求分类器、响应核验器、工具注册与参数校验。
工具执行器是独立 SQLite 合成环境，不访问公司实际数据库；每次 trial 都重置。
30 条案例在 `cases/business.jsonl`，涵盖事实、缺少证据、空结果、故障、权限、写入、历史冲突、文档指令注入与澄清。
这些是开发案例，不是真实失败记录，不是冻结保留集，也不是 tau2 官方基准。

在 `lab/.env.local` 配置（文件已被 Git 忽略）：

```dotenv
AGENT_LAB_BASE_URL=https://your-compatible-endpoint/v1
AGENT_LAB_MODEL=your-model-id
AGENT_LAB_API_KEY=your-local-key
# 仅当明确使用 HTTP 服务时设置：
# AGENT_LAB_ALLOW_HTTP=1
```

```powershell
python lab/test_eval.py
python lab/run_eval.py --dry-run
python lab/run_eval.py --case fact-01
python lab/run_eval.py --concurrency 2 --repeat 1
```

环境变量优先于本地配置文件。缺少任一模型配置直接退出；不回退假模型。
默认并发1，每次 trial 最长120秒，工具调用最多8轮；分类、核验及核验重试也会调用模型并消耗 token。
取消后已完成的 trial 保留在 JSONL 中。重复运行必须用新的输出目录，不覆盖旧报告。

输出在 `lab/results/eval-*`：

- `manifest.json`：模型、源码版本与指纹、依赖版本、案例文件指纹、任务选择和预算。
- `trials.jsonl`：完整模型输入与事件、工具调用企图（包括被拒绝的企图）、真实执行回执、最终数据库、断言与耗时。
- `summary.json`：按类别汇总、失败断言、token 和 p50/p95 耗时。

通过要求运行完成、匹配指定回答模式、满足所需工具调用、没有禁止的调用，并且数据库全部单元格符合预期。
这只是可复查的规则评分，**不能直接解释为幻觉率或语义正确率**；全部案例仍需语义复核。
核验器的 supported 不是评分真值。权限案例只验证工具路由与合成资源边界，不证明生产 IAM 正确。
多轮案例目前是固定历史，不包含动态用户模拟或完整跨轮记忆服务。
报告汇总所有模型事件中的 usage，包括核验失败重试；未返回 usage 的接口可能低估 token，不估算未知价格。
本地报告包含合成任务的完整对话，仅提交脱敏汇总，不提交轨迹、密钥和公司快照。

## 两条工作线

1. [论文与业界实践](research.md)：记录来源、阅读深度、可检验假设、实验和结论。先留基线，再一次改变一个变量。
2. [数据集与批量测试](datasets.md)：原始数据版本锁定，优先原生评测器，业务测试独立报告。

公开数据入口：`python lab/run_ragtruth.py --dry-run` 验证200条核验器诊断计划；τ²使用独立环境运行 `lab/.venv/Scripts/python.exe lab/run_tau2.py --dry-run`。
安装、评分口径和限制见[数据集说明](datasets.md)。τ²默认使用原生参考scaffold；`--agent graph`通过后端worker运行生产Phase2图并交由原生环境执行工具。

## 后续实现顺序

1. 运行现有离线基线，记录失败，不预先更改生产语义。
2. 已接入真实模型与固定业务环境；先运行小集，再分析完整基线。
3. 已接入τ²原生环境与评分器的生产图适配；先修复已复现的多轮来源与核验问题，再用独立任务确认。
4. 将线上真实失败脱敏转为案例，增加独立保留集；当前30条仅为合成开发集。
5. 数据库集成使用独立测试实例与生产同类引擎，每例重置数据。离线 SQLite 设置只用于无需数据库的回归，不用于证明生产 SQL 正确。
6. 比较 baseline 与单变量改动：成功率、无依据事实率、虚假完成率、权限违规、p50/p95 时延、token 和费用。固定任务、模型、预算和种子；随机性案例重复运行，报告分母与不确定性。

保留开发集与冻结保留集；保留集不得参与提示词调整或训练。业务权限违规和虚假完成单列，不能被平均总分掩盖。
当前已建立真实模型基线并开展单变量实验；开发集结果仍需保留集、多次重复与独立语义复核验证。

## 已运行实验

- [首轮真实模型业务基线](experiments/2026-10-20-qwen-baseline.md)：澄清坐标后的30条案例，规则通过23条。
- [表格证据契约实验](experiments/2026-10-20-table-evidence.md)：同集规则通过28条，包含剩余失败、针对性重复和语义抽查问题；尚未回迁生产。
- [公开数据首轮结果](experiments/2026-10-20-public-datasets.md)：RAGTruth200条核验器诊断与τ²零售10条原生参考，分别报告口径与剩余工作。
- [开发集候选与生产图多轮结果](experiments/2026-10-20-dev-and-graph.md)：提示词候选被拒绝；生产图适配完成评分但正常完成率更差，未推广。

### 跨轮只读证据实验

`run_tau2.py --agent graph --domain retail --carry-read-evidence --limit 10` 启用任务内只读证据缓存，默认关闭。缓存仅接收实际成功的非变更 READ 工具回执，保留工具、参数、观察轮次和原始结果；作为事实来源，不能支持操作完成声明。任何非 READ（含未知）工具在交给环境执行前清空缓存，同时撤销本轮更早读取的事实别名。当前仅允许 retail，避免 telecom 用户工具从另一侧修改状态时无法观察失效事件。既有对话历史仍可见，但不被当作权威事实来源。

此实验只改变核验器可用证据，生成模型仍使用相同可见对话历史；不改核验提示词或裁决规则。核验器新增可覆盖的 `build_input` 方法，默认仍使用原有来源构造器。结果必须与相同任务的基线分别报告，不能作为生产环境缓存一致性证明。

首轮实验结果见 [跨轮证据报告](experiments/2026-10-20-read-evidence-lifecycle.md)：同组10条目标达成3→8，正常完成2→5，仍有5条核验终止；默认保持关闭。

### 操作结果归因实验

`run_tau2.py --agent graph --carry-read-evidence --attribute-operation-results --limit 10` 在跨轮READ缓存配置上，仅增加核验器的操作结果归因说明；默认关闭，不改变来源类型、工具执行或精确引用校验。真实回执中本次操作直接返回的字段按operation核验，不能替代普通company_fact来源，不能由调用成功推断银行到账、未来交付或未返回字段。

先运行 `python lab/run_operation_eval.py --output lab/results/<新目录>`，比较原核验器与候选在18条人工构造诊断案例上的表现。案例含直接返回状态/金额、虚构字段、错误资源、业务拒绝、状态未变、超时未知、只读假装写入、后续状态冲突及结果注入；这是合成诊断，不是公开基准或整体安全性证明。`python lab/test_operation_eval.py` 检查显式启用及来源类型边界。

操作归因提示候选已否决：[实验报告](experiments/2026-10-20-operation-attribution.md)。虽然预设18条诊断全部通过，补充反例出现超时被误认为“无副作用”的错误放行；tau2目标达成8→6。开关仅用于复现，保持默认关闭。

### 未知写入结果停止实验

`run_tau2.py --agent graph --carry-read-evidence --stop-on-unknown-operation --limit 10` 在原READ缓存配置上启用任务内硬停止，不启用已否决的归因提示。执行状态分为已返回returned（不代表业务成功）、执行前拒绝not_executed、可能有副作用但无确认unknown，以及只读失败read_failed。unknown由可信执行元数据判定，不解析工具文本作为授权或成功标志；一旦触发，原图进入FAILED/reconcile_tool，输出固定不确定提示，取消后续调用，拒绝同任务的新用户重试，不将其计为正常完成。

`python lab/run_outcome_faults.py --output lab/results/<新目录>` 用真实模型、实际SQLite提交和人工注入的丢失回执比较开关两种配置。覆盖提交后超时、提交前超时、执行前拒绝、正常返回、业务拒绝和只读超时；不接触外部业务数据库。这是合成故障诊断，不是公开基准。`python lab/test_operation_outcome.py` 验证模型无法覆盖硬停止及已排队调用不会继续。

边界：门闩只在当前worker任务内有效，尚无跨进程持久恢复、幂等业务状态查询或自动对账；会停止全部后续工具，而非自动恢复业务。仅作为默认关闭的实验，不能推广为生产级幂等保障。

首轮结果见[未知操作结果实验](experiments/2026-10-20-unknown-operation-outcome.md)：两条真实模型未知写入均被硬停止，106项检查通过；普通tau2中门闩触发0次，不能将成绩变化归因于该机制。仍默认关闭，尚无跨进程幂等与自动对账。

### 持久操作ID与对账合同实验

`python lab/run_durable_eval.py --output lab/results/<新目录>` 运行独立Python子进程崩溃、重启与并发重试检查；加 `--real-model` 会额外复用Phase2图，调用真实模型查询两种重启场景的权威操作状态。没有模型配置不会回退假模型。

受控工具实现位于 `lab/durable_counter.py`，仅用标准库：运行时journal在发送前持久化unknown，业务端把计数变更和幂等操作记录放在同一SQLite事务。scope、intent与操作ID由可信调用方绑定，不是模型工具参数；重复intent不能换payload。查不到记录仍是unknown，不能据此断言没有副作用；仅此已具备原子幂等合同的工具允许可信恢复流程复用原键重试。恢复图只开放绑定当前操作的查询，不开放写入。

这是受控后端合同验证，未接入原生tau2工具或生产数据库；未实现完整LangGraph检查点恢复、平台IAM或任意第三方工具的幂等。操作ID只有在可信调用方跨会话保持同一逻辑intent时才有效，不能自动识别用户换个说法重复提出的请求。

首轮结果：[持久操作恢复报告](experiments/2026-10-20-durable-operation-recovery.md)。5组跨进程/并发流程检查通过，2条真实模型恢复任务正常完成且未写入业务；仅验证受控SQLite合同，未恢复整个平台执行流程。
