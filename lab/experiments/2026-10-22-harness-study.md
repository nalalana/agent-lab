# Harness 分支对照与综合实现

日期：2026-10-22。起点 be8f9e0。最终功能提交 31e0d1c，所在分支 solution/harness-20261022；本报告之后只补文档。仅修改实验快照，未部署、未推送 upstream，未启动 PostgreSQL/immudb。

## 结论与采用范围

采用：可信表格证据作用域提示、核验故障类型分流与已报告用量落盘、完整实现/依赖续跑指纹，以及可信变更结果 unknown 的确定性停止门禁。

新合同修复提示及其与证据提示的组合保持显式实验模式。组合在固定草稿上最好，但完整业务任务反而下降，不能因为静态分数漂亮就设为默认。默认 strategy=evidence_scope，保留原先的有界 reask 提示；contract_repair/combined 只有显式指定才启用。baseline 模式只恢复原模型输入/重询方式，回到完整历史行为应使用冻结的 baseline 分支。

本轮没有证明通用事实核验、生产并发或跨 run 幂等已经解决。业务开发/验证仍来自8个共享模板；公开标签口径与语义蕴含问题没有靠这轮改动消失。

## 一手资料与实现对应

完整来源、阅读深度和局限见[资料核查](2026-10-22-harness-sources.md)。本轮采用的工程假设主要来自：

| 来源 | 日期/地位 | 实际借鉴 |
|---|---|---|
| [VReAct v3](https://arxiv.org/html/2609.31937v3) | 2026-10-18修订，预印本、小规模pilot | 区分确定性合同和语义判断，错误归属哪层就修哪层；未复现作者系统 |
| [GroundEval v2](https://arxiv.org/html/2606.22737v2)、[代码](https://github.com/tenurehq/groundeval) | 2026-07-02修订，论文与作者项目 | 时间、权限、证据路径合同；在已有来源之上派生简明作用域，保留原始证据 |
| [Finding the Right Fit](https://arxiv.org/html/2610.00917v1)、[代码](https://github.com/liyix/finding-the-right-fit) | 2026-10-01，预印本 | 对模型×harness组合做对照；锁定实现、环境和选择列表 |
| [Google harness工程](https://developers.googleblog.com/the-anatomy-of-harness-engineering-how-to-evaluate-iterate-and-guard-ai-coding-agents/) | 2026-09-09，官方工程文章 | 同时检查具体行为和最终状态；小型行为回归补充总分 |
| [Pydantic AI](https://pydantic.dev/docs/ai/core-concepts/output/#output-validators)、[Guardrails源码](https://github.com/guardrails-ai/guardrails/blob/main/guardrails/actions/reask.py) | 官方文档/代码，2026-10-22访问 | 有限输出修复和结构化错误重询；未引入框架依赖 |
| [AWS幂等API](https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/) | 较早的官方工程资料 | unknown不能当无副作用；安全重发还需要业务端原子幂等合同 |

“最新”“一手”不等于已被独立验证。尤其VReAct只有47次执行、10个问题；GroundEval是评测框架，不是核验准确率改进保证。

## 隔离分支与冻结版本

每种方法使用独立Git worktree，没有在一个正在运行的目录切换实现。所有结果目录独立，旧报告均保留。

| 分支 | 冻结提交 | 用途/最终决定 |
|---|---|---|
| exp/harness-baseline | 0e76869 | 原语义对照；只有公共评测指纹补丁 |
| exp/harness-repro | 30ddfb6 | 续跑实现/环境身份，采用 |
| exp/harness-contract-repair | e9c38b3 | 独立结构化reask，保留实验 |
| exp/harness-evidence-view | ba542ed | 证据作用域，采用为实验默认 |
| exp/harness-combined | cc3f2a9 | A+B组合，保留实验，不默认启用 |
| exp/harness-fault-routing | 7b3f2e5 | 故障类型、已报告usage/metrics链路，采用 |
| exp/harness-outcome-gate | aa6153c | 可信unknown变更硬停，采用 |
| exp/harness-fixture-contract | df7e227 | 迁移夹具两处确定性拒绝元数据，采用；不算模型提升 |
| solution/harness-20261022 | 31e0d1c | 综合功能版本，当前工作分支 |

main保留在be8f9e0。study/harness-20261022保留研究/运行基础设施阶段。每个实验目录manifest记录实际提交、dirty和指纹，不能用最终提交冒充所有历史运行版本。

## 协议与样本

原[冻结协议](2026-10-22-harness-study-protocol.md)先于模型结果提交。沿用显式配置qwen3.8-27b，temperature=0，固定草稿每分支并发4、单条120秒；不回退fake。开发集选两个完整实体组64条，验证选三个实体组96条，均覆盖8模板×正反×中英文；公开开发/验证分别24/48条，按三种任务与标签平衡并避免同批参考组重复。

选择与数据指纹在 lab/results/harness-study-20261022/corpus/manifest.json。没有打开或运行封存test文件。现有验证集此前已用于研究，不宣称全新未见集；按验证表现选择方案也意味着仍需独立新数据验证。

原计划先比较静态核验再跑基线与最终候选端到端。发现组合回归后，补跑了A/B端到端；发现未知副作用被误归因后，又单独实施工程门禁和夹具迁移。这些补充不是运行前全部预注册，也没有据此重新调提示、修改旧gold或删除失败。

## 固定草稿结果

“正确”表示相对冻结布尔标签，合同/执行错误计错，不等于真实语义准确率。误拒/误批仅计实际得到相应裁决的样本，错误另列。

| 方案 | 业务开发正确/64 | 业务验证正确/96 | 验证误拒 | 验证误批 | 验证错误 | 公开开发/24 | 公开验证/48 |
|---|---:|---:|---:|---:|---:|---:|---:|
| baseline | 58 | 85 | 7 | 0 | 4 | 15 | 33 |
| contract-repair | 60 | 90 | 5 | 0 | 1 | 14 | 33 |
| evidence-view | 61 | 92 | 3 | 0 | 1 | 15 | 34 |
| combined | 64 | 96 | 0 | 0 | 0 | 13 | 33 |
| 收窄采用后的综合核验器 ca4a142 | 未重跑开发集 | 93 | 2 | 0 | 1 | 未重跑开发集 | 33 |

综合核验器相对baseline：85/96→93/96，88.54%→96.88%；逐例9改善、1退化，净8条。不能忽略那1条退化，也不能把48条错误声明中0次明确误批解释为生产风险为0。只涉及3个实体组，行并不独立。

复跑同一开发集：baseline业务58→59，combined为64→64；公开baseline15→13、combined13→14，说明temperature=0也有波动。公开workspace.read输入未触发表格scope；不应把两三个样本的波动当作稳定提升或退化。

上述最后一行来自ca4a142。后续unknown门禁仅修改工具执行节点，最终response_verification.py字节摘要仍为 f07e2448900cfb2fcf0a13216d80d5f2bfdf8519d8d772561dee6ca671ca9388；没有重新把这些固定草稿包装成最终端到端任务成绩。

| 业务验证成本 | 调用数 | 已报告token | p50秒 | p95秒 |
|---|---:|---:|---:|---:|
| baseline | 128 | 304296 | 1.61 | 4.01 |
| contract-repair | 133 | 309403 | 1.41 | 3.94 |
| evidence-view | 130 | 317520 | 1.64 | 3.78 |
| combined | 132 | 316544 | 1.55 | 3.55 |
| 收窄综合核验器 | 128 | 310882 | 1.15 | 3.04 |

延迟是实际运行观察，受并发和服务波动影响，不能据此声称架构带来确定性加速。综合核验器token比baseline约高2.2%。

## 端到端、语义审阅和否决依据

每组使用原Phase2图、30条业务案例和6条写入/冲突恢复案例。SQLite夹具不等于生产数据库；30条普通业务夹具不证明真实版本门禁/IAM，冲突夹具另外检查版本与条件。

| 方案 | 业务机械通过/30 | 冲突状态合同/6 | 冲突正常完成/6 |
|---|---:|---:|---:|
| baseline | 29 | 6 | 6 |
| contract-repair | 28 | 6 | 5 |
| evidence-view | 29 | 6 | 5 |
| combined | 27 | 6 | 6 |
| 收窄综合版本ca4a142，尚无新门禁 | 29 | 6 | 6 |

组合write-05已经成功写入20/4，只执行一次，但引用类别修复失败，答案未交付。不能将其称为业务写入失败，也不能建议重复原写入。A/B各有一次persistent-conflict核验终止；状态合同通过并不表示已正常交付。

更严重的是，ca4a142的failure-04机械通过，却把business_outcome=unknown的清空超时说成“未清空、结构未受影响”。没有写后重读；模拟最终状态碰巧未变，不能为该声明提供模型可见的确认。另一个failure-03在unknown后继续读表并询问重试，仍没有权威操作对账。

因此没有选择静态最高分组合为默认，也没有把29/30当语义正确率。[开发审阅](2026-10-22-harness-dev-audit.md)及[端到端审阅](2026-10-22-harness-e2e-audit.md)保留全部归因；它们是代理辅助审阅，不是外部独立人工盲标。

## 新增确定性unknown门禁及夹具合同迁移

门禁由可信ToolDefinition.effect_class和适配器execution_status/business_outcome决定，返回值、类型化异常共用一个小helper。可能变更且结果未知时规范为OPERATION_OUTCOME_UNKNOWN：retryable=false、不登记完成去重、停止排队工具和模型续写，原始source_error_code/source_execution_status/source_business_outcome保留。模型参数、回执content内伪造的成功字段都不能覆盖这些状态。

只读unknown、可靠执行前拒绝、明确conflict和成功变更保持各自路径。这不是跨run幂等、原子回滚或自动恢复；它只保证当前运行不会继续把未知结果当已知结果使用。

真实复测 outcome-e2e/business：28条正常完成，failure-03/04两条安全终止；旧规则机械分数28/30原样保存，因为它要求COMPLETED和非空答案。额外安全断言单列在 outcome-safety.json，两个未知变更的停止合同从0/2→2/2，六项断言分别检查规范错误码、无重试提示、无草稿发布、无后续模型/工具、无后续回执、原始错误保留。不将“28正常+2安全停止”改写为30/30任务完成。

初跑新门禁+旧冲突夹具只有2/6正常完成、5/6状态合同：夹具明明在写前发现版本冲突，却沿用通用failure，返回unknown。保留 outcome-e2e/conflict 原失败报告。仅在夹具TABLE_READ_REQUIRED提前返回处声明not_dispatched/not_attempted、版本冲突处声明returned/conflict，与生产适配器一致；不改CASE、预期状态、评分、提示或通用失败默认。

迁移夹具后的 outcome-fixture-v2-e2e/conflict 实际为6/6正常完成、6/6状态合同：绝对授权冲突后重读并提交，条件变化保留他人值，持续冲突0次提交且最多重试一次。这里2→6体现夹具可信合同修正，不能算作模型能力提升。

## 可靠性与复现

- 最终166项离线检查通过，包含82原图基线、9项新unknown门禁检查，以及引用、历史证据、权限、恢复、存储未知、表版本、计费、评分与数据隔离等合同。报告 acceptance/final-local-checks.json。
- 另外1884组事件组合审计确认scope不改变原始sources、不升级历史证据、不修改state；脚本和摘要在 evidence-audit/，属于程序合同检查，模型能力分数为空。
- 核验器自身合同/模型故障现在分别进入VERIFIER_CONTRACT_ERROR/VERIFIER_MODEL_ERROR，不消耗草稿修订预算、不暗示重发业务；已报告usage/metrics经过异常→图→run.failed事件→AgentRun已知input/output持久字段。外层超时、取消及任意直接抛出异常的遥测回收仍未完全覆盖，不估算未返回用量。
- 续跑身份覆盖runtime/lab Python、提示资源、Python和已安装依赖版本；数据/选择、模型和端点另锁。无关报告与结果不改变执行指纹，代码/依赖改变则拒绝续跑。旧manifest应新建运行目录；全体包版本绑定较保守。

本机使用 E:/program/touming/.venv/Scripts/python.exe；lab/.venv是独立tau环境，不含完整后端依赖。没有为本轮安装新Agent框架、复制业务配置或把密钥移入Git。

从原仓库目录复现，必须使用新的输出目录：

```powershell
../.venv/Scripts/python.exe lab/prepare_harness_study.py --output lab/results/<新的子集目录>
../.venv/Scripts/python.exe lab/run_harness_study.py --checkout E:/program/touming/agent-lab-evidence --phase validation --corpus lab/results/<新的子集目录> --output lab/results/<新的运行目录>
../.venv/Scripts/python.exe lab/run_harness_study.py --checkout E:/program/touming/agent-lab --phase business --output lab/results/<新的业务目录>
../.venv/Scripts/python.exe lab/run_harness_study.py --checkout E:/program/touming/agent-lab --phase conflict --output lab/results/<新的冲突目录>
```

主运行器只把原目录显式本地配置注入子进程，worktree无需复制密钥。不同分支的--checkout可替换；冻结历史对照请使用上表对应worktree/提交，而非将当前实现称为历史baseline。

## 结果存放与剩余工作

原始结果位于忽略目录 lab/results/harness-study-20261022/：35个运行目录、comparison.json、study-totals.json、选择manifest、两份语义审阅的原始轨迹、outcome-safety.json及离线检查均保留。本轮共1470次真实模型trial、2818次模型调用尝试，接口已报告7422148 token；包含失败和旧夹具诊断成本，无价格配置，不估金额。轨迹、数据和密钥不提交。

下一步优先级：

1. 对公开数据另建经过独立语义复核的版本，拆分implicit_true、标注遗漏、引用转义故障和真正漏检；不修改本轮gold来美化分数。当前公开验证仍33/48，其中2次明确误批，不能宣称通用幻觉已解决。
2. 对成功写入后的核验交付建立独立恢复接口，只修复/重新核验草稿，不能重跑业务变更；新的reask提示尚无可靠端到端收益。
3. 跨run未知操作仍需权威查询、稳定意图/操作ID及业务端原子幂等；本轮硬停不提供自动解除或重复写入保证。
4. 可信资源键、跨租户/run认证、真实PostgreSQL锁和immudb边界仍待独立环境验证。封存测试未解封，暂不提出生产部署结论。
