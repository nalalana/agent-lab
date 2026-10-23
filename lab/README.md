# Agent 架构研究与评测

2026-10-23后续[显式提交凭证与写入对账](experiments/2026-10-23-reconciliation.md)：新增opt-in原子业务/receipt事务与管理员正向对账，固定真实双库24/24、最终263项离线检查通过。综合分支`solution/reconciliation-20261023`；旧四入口与默认Agent未切换，旧无凭证pending继续阻断，无新增模型成绩。完整授权/HTTP/runtime/UI联调仍待完成。

2026-10-23后续[旧写入口收拢与跨入口版本保护](experiments/2026-10-23-unified-writes.md)：raw完整单元格属性写入与删行纳入围栏，旧入口合同2/13→13/13、实际并发0/2→2/2；审阅又复现仅带旧cell版本的跨入口覆盖，修正后0/3→3/3。综合分支`solution/unified-writes-20261023`最终231项离线检查、既有安全10/10及崩溃2/2通过。权威对账、生命周期入口与完整平台联调仍待完成，无新增模型成绩。

2026-10-23新增[真实双库对照与受管写入故障隔离](experiments/2026-10-23-real-storage.md)：本机PG 18.6与immudb 1.11.2均已运行。固定安全场景原方案1/10、整批预检2/10、持久围栏9/10、组合10/10；另列进程退出0/2→2/2、PG锁2/2，综合入口204项离线检查通过。分支 `solution/real-storage-20261023` 仅覆盖受管写入/清空/读取；旧入口收敛、权威对账和完整平台联调仍待完成，无新增模型成绩。

2026-10-22新增[本地数据库验证与写前拒绝修正](experiments/2026-10-22-local-databases.md)：PostgreSQL/psql 18.6本地TLS连接与真实行锁2场景通过，179项离线检查通过；immudb v1.11.2已下载校验，但启动被Windows应用控制阻止，真实双库测试仍不可用。综合分支solution/local-storage-20261022保留严格unknown门禁，仅准确区分8处明确的写前参数拒绝。

2026-10-22新增[分支对照与综合方案](experiments/2026-10-22-harness-study.md)：隔离比较合同修复、证据作用域与组合，保留端到端回归和语义反例；综合实现增加可信unknown变更硬停、故障分流、失败用量及续跑指纹。静态最高分组合保持显式实验，未部署。来源见[一手资料核查](experiments/2026-10-22-harness-sources.md)。

同日[引用与语义核验后续对照](experiments/2026-10-22-verifier-followup.md)在四个独立分支测试原文分块、专用事实核验、短引用与修复片段目录。目录修复3条构造引用故障，但公开核验没有整体收益，完整冲突交付下降；均保留实验，不替换综合默认。综合复核分支为solution/harness-reviewed-20261022，167项离线检查通过。

大批量数据集现有18,396条：开发13,253、验证3,180、封存测试1,963；按原始参考材料或企业/表格组隔离。业务合成样本3200条只有8类结构模板，不能当作3200个独立场景。构建、分组、标签、去污染及断点批量运行见[大批量数据集说明](eval-corpus.md)，冻结指纹见[eval-corpus-v2.lock.json](eval-corpus-v2.lock.json)。

Harness下一阶段合同与现有调用链映射见[harness-contracts.md](harness-contracts.md)。已统一失败恢复指导、迁移公司表格执行/业务双轴回执，并让写入门禁与回答核验共用当前run表格证据视图。共享证据阶段120项本地检查、8组源码边界及6400次程序轨迹变体等价检查通过，不是新模型成绩，见[共享证据报告](experiments/2026-10-22-shared-table-evidence.md)。之后的故障分流与综合检查见上方报告；其他工具与跨run资源绑定仍待实施。

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

### 真实表格源码的跨库边界诊断

`python lab/run_table_boundary.py --output lab/results/<新目录>` 从当前后端源码提取未改写的upsert_table_cells及其版本/审计辅助函数，配合真实Django ORM与独立SQLite远端替身运行。IAM和内容审核使用允许夹具，未导入生产配置，未连接真实immudb，不是整个平台集成或PostgreSQL锁测试。

源码显示单元格存于immudb，审计/版本位于Django数据库；本地atomic不能回滚远端提交。诊断覆盖响应丢失后的审计回滚、多单元格部分成功、盲重试覆盖中间写入、持久unknown阻断、新意图不能绕过同任务门闩、已确认回执重放前重新授权及保留原版本的冲突检查。

`lab/legacy_write_guard.py` 仅为实验包装器：写前持久化unknown，未确认则不允许重试；已收到完整成功回执才缓存历史结果。它不为旧接口创造业务端幂等能力，未接入生产Executor；缺少权威操作查询时不提供自动解除门闩。

补充诊断还执行存储客户端upsertToTable的原始重试方法：异常后会自动重试，最多10次；外层门闩不能阻止调用内部重试。该保护只覆盖外层重发，不能宣称端到端避免重复写入。

正式6组诊断见[公司表格跨库边界报告](experiments/2026-10-20-table-write-boundary.md)。已复现远端提交后失败、部分写入及隐藏SDK重试；实验guard仅覆盖外层重发，尚未接入生产路径。

### 存储写入不确定时停止重发（2026-10-21）

实验后端的 `toumingClient.upsertToTable` 对未确认写入只发送一次，抛出专用UnconfirmedWriteError；SELECT原有重试保持不变。company.table.write/clear_cells在旧服务返回False（可能部分成功）时保守保留unknown；执行前参数检查仍为参数错误。类型化未知经过表格服务、workspace执行器传至原执行图，停止排队调用和模型续写，retryable=false，未记为已完成去重键。公司表格写入的外层执行超时也保留unknown。

`python lab/test_storage_retry.py` 验证实际SDK方法、异常/回执形态、读取重试及原图终止；`python -O lab/test_storage_retry.py` 验证确认检查不依赖assert。`run_table_boundary.py` 现在验证修改后的源码，昨日原始重试复现保留在原结果目录及报告中。仅修改agent-lab快照，尚无真实immudb/PostgreSQL验证或生产回迁；insertToTable等其他写接口的重试不在本次修复范围。

结果见[存储写入重试修正报告](experiments/2026-10-21-storage-write-retry.md)：113项本地检查、7组源码边界检查通过；应用层upsert未知时不再重发，原执行图停止后续调用。仅修改实验仓库快照，跨库原子性与跨run幂等仍待解决。

### 读取版本绑定写入（2026-10-21）

实验快照中 `company.table.write` 必须有当前run内最近一次成功读取目标表的版本。执行图从真实工具回执提取table_version，通过ToolContext传给现有OpenGate乐观校验，不接受模型参数中的版本；缺少读取返回TABLE_READ_REQUIRED，并发版本变化返回TABLE_WRITE_CONFLICT且未发起远端写入。写入/清空尝试或读取失败后，旧读取不再可用于下一次写入；重新读取才可继续。仅table.write接入，clear_cells自身尚无同等版本保护。

运行 `python lab/test_table_versions.py` 和 `python lab/run_table_boundary.py --output lab/results/<新目录>`。这是本地合同与替身远端诊断，不代表真实数据库或模型成绩；整表版本可能因其他单元格变动而冲突，绕过审计的写入无法被该版本检测。

结果见[读取版本绑定报告](experiments/2026-10-21-trusted-read-version.md)：117项本地检查、7组边界检查通过，无新增真实模型成绩。

### 清空单元格版本保护（2026-10-21）

后续修正把同一可信读取版本合同接入 company.table.clear_cells。缺少当前run读取时拒绝清空；OpenGate清空入口在与upsert相同的表锁内检查整表版本，再执行原清空及审计逻辑。过期版本在远端调用与创建审计前被拒绝；成功清空的审计推进整表版本，使之前的读取不能用于后续写入。

`test_table_versions.py` 包含清空前置读取、模型版本不能覆盖、清空后必须重读检查；`run_table_boundary.py` 增加清空冲突、新版本清空、写入拒绝清空前版本及清空丢回执仍为unknown的组合诊断。仍未验证真实PostgreSQL行锁或immudb；本地事务无法回滚远端清空。

本轮94项本地检查、8组边界诊断通过，见[清空版本保护报告](experiments/2026-10-21-clear-read-version.md)，无真实模型调用。

### PostgreSQL锁集成入口（待环境运行）

`python lab/run_postgres_locks.py --output lab/results/<新目录>` 只连接本机127.0.0.1的专用数据库agent_lab_test，用户同名，默认端口55432。运行前在本地环境设置AGENT_LAB_PG_ENABLED=1、AGENT_LAB_PG_PASSWORD，非默认端口用AGENT_LAB_PG_PORT；不读取生产配置或lab/.env.local。测试账号须有该专用库的CREATE权限。每次新建UUID schema，测试结束仅删除该schema；不会创建或删除整个数据库。

入口通过原源码方法与真实Django ORM检查写入/清空两个顺序的竞争，查询pg_stat_activity确认等待锁，放行后要求等待方拒绝旧版本且未调用远端。远端仍为SQLite替身、IAM和审核为允许夹具，不能称为完整immudb集成。连接或配置缺失返回unavailable且退出非零，不回退SQLite；仅通过真实锁场景才设置postgresql_lock_behavior_tested=true。

当前机器未发现Docker/Podman/PostgreSQL服务或专用测试配置，真实锁场景尚未运行。`python lab/test_postgres_runner.py` 的3项检查只验证缺配置和连接失败时不回退、不暴露密码；不能算作PostgreSQL并发验证。

后续支持显式云测试配置：`python lab/run_postgres_locks.py --config lab/.postgres.local.json --output lab/results/<新目录>`。JSON键使用AGENT_LAB_PG_ENABLED、PASSWORD、HOST、PORT、DATABASE、USER的完整AGENT_LAB_PG_前缀；本地文件已忽略。数据库须为agent_lab_test或test_开头，连接强制TLS require（不代表已校验证书主机名），默认连接仍为本机。当前用户提供的云测试实例TCP连接超时，未登录、未建schema；实际并发验证仍待网络放通。新增配置检查共4项，不是数据库测试成绩。

### 版本冲突后的真实模型恢复（2026-10-22）

PostgreSQL测试已按用户要求停止。`python lab/run_conflict_recovery.py --output lab/results/<新目录>` 使用原Phase2图、真实模型和独立SQLite工具夹具，检查先读后改、条件变化保留他人数据及冲突重试上限；不连接外部数据库。`python lab/test_conflict_recovery.py` 验证夹具冲突与提交行为。

首轮6项数据状态合同全部满足，但审阅发现连续冲突后模型将过期读取值当作当前值，校验器误放行。机械通过与语义正确必须分开，见[真实模型冲突恢复报告](experiments/2026-10-22-conflict-recovery.md)。

### 表格证据时间归属（2026-10-22）

旧读取遇到后续同表操作后标记为historical_table_content，历史声明与当前事实分开引用；存在历史表格证据时，对首轮批准的整段回答增加时间归属审核。新读取可提供新的观察，其他表操作不使本表证据失效。`python lab/test_evidence_freshness.py` 验证来源与审核合同；`python lab/run_freshness_eval.py --output lab/results/<新目录>` 调用真实校验模型，不连接数据库。

最终6个完整场景未再发现旧值冒充当前值，固定草稿7/8正确，仍误拒1条正确不确定性说明。额外审核增加成本，尚未推广生产，详见[证据新鲜度报告](experiments/2026-10-22-evidence-freshness.md)。

### 本地 PostgreSQL + immudb 集成诊断入口

`python lab/run_realdb_integration.py --config lab/.realdb.local.json --output lab/results/<新目录>` 仅接受显式本地 JSON；该配置路径已忽略。必须包含 `AGENT_LAB_PG_ENABLED="1"`、`AGENT_LAB_IMMUDB_ENABLED="1"`，以及两个完整前缀下的 `HOST`、`PORT`、`USER`、`PASSWORD`。另需显式 `AGENT_LAB_PG_DATABASE` 为 `agent_lab_test` 或 `test_` 开头的已有测试库，复用原 PG 入口检查。两种 HOST 均限制为 loopback，PG 强制 TLS require；immudb 使用本地 gRPC。建议端口分别为 55432、53322，禁止填生产凭据。

该入口复用原 PG 锁流程、source-extracted OpenGate/ImmudbService 方法、生产 toumingClient 和实际 SDK。生成独立 PG schema、`test_<UUID>` immudb 数据库和 UUID 表，最终清理。覆盖两个顺序的锁竞争与过期拒绝、正常写/清空、实际提交后 SDK 丢确认、第二单元格被真实序列化器拒绝后的部分提交。输出记录 PG 审计/版本和远端事实，确认 unknown 不等于未提交；不会尝试自动补偿或把跨库非原子行为称为解决。

IAM/审核仍为允许夹具，持久 unknown 拦重放使用 lab-only SQLite journal，不是生产对账实现，也未覆盖完整 runtime/SSE/recorder。连接不可用时返回 unavailable/非零，无替身回退；`python lab/test_realdb_runner.py` 仅检查离线配置、SDK 故障边界与原服务合同，不能充当真实双库通过结果。

### 冻结迁移诊断（2026-10-22）

`python lab/run_freshness_eval.py --transfer --output lab/results/<新目录>` 使用12条运行前冻结的新样本，覆盖不同表、中英文、混合表格、未知清空、刷新超时、引用与单元格注入。当前验证器未据此调参。首轮11/12正确：错误声明6/6拒绝，正确草稿5/6批准，误拒1条；19次模型调用、37713 token。详见[迁移诊断报告](experiments/2026-10-22-freshness-transfer.md)，不等于公开基准或端到端业务成绩。
