# Harness 改进的一手资料核查与实验依据

检索截止：2026-10-22（Asia/Shanghai）。阅读对象包括本仓库 `AGENTS.md`、`lab/harness-contracts.md`、`lab/research.md`、`response_verification.py`、`table_evidence.py`、`tools/recovery.py` 和 `run_corpus_eval.py`。本记录提供工程假设和来源核查，不是论文全文复现，也不代表候选改动已改善真实模型成绩。各分支的代码、检查结果和效果对照应以本轮实验总报告为准。

## 采用原则

优先在现有生产执行图、来源校验器及评测入口内作单项改动，不引入新 Agent 框架或额外裁判。论文的新旧、作者机构和项目知名度不能替代本地验证。下列资料均来自原论文、作者代码或官方文档；预印本与小规模 pilot 的证据强度明确保留限制。

## 与三个隔离分支对应的来源

| 方案 | 一手来源与日期 | 实际阅读深度 | 对当前实现的具体启发 | 可验证范围与限制 |
|---|---|---|---|---|
| `contract-repair`：校验器合同修复独立于草稿判断 | [VReAct v3](https://arxiv.org/html/2609.31937v3)，2026-10-18 修订，首稿 2026-09-25 | 摘要、§3–5、§6.4、§7；未运行作者系统，未找到并审计可用作者实现 | 区分确定性 gate 与语义 judge；错误归属哪一层，就修哪一层。现有 JSON/schema、来源 ID、原文片段、引用类别属于校验器输出合同。对它们进行有限重询，保持同一草稿与来源；时序审核异常不能触发主审核格式重试；分别记录合同故障与 `revise`。 | 论文仅 47 次执行、10 个问题，37 次可评分，且部分架构尚未实现；不支持规模化准确率结论。本地方法是借鉴职责分离，不是复现论文架构。格式正确仍可能产生错误语义拒绝；不能仅靠本地解析验证降低 65 次误拒。 |
| `evidence-view`：给模型简明可信证据视图，同时保留原始来源 | [GroundEval v2](https://arxiv.org/html/2606.22737v2)，2026-07-02 修订，首稿 2026-06-22；[作者代码](https://github.com/tenurehq/groundeval)、[LangGraph 适配说明](https://raw.githubusercontent.com/tenurehq/GroundEval/main/docs/langgraph.md) | 论文 §3–6、§9–10；README 与 LangGraph 适配说明。未完整审计评分器或执行其基准 | 把时间、权限和证据路径约束写成可检查合同。当前最小实验是从现有 `table_observations` 派生历史/最近观察范围、失效事件和来源类别提示，帮助模型理解时序；原始 `sources`、逐字引用与 Python 类别校验继续作为准入依据。 | GroundEval 是评测与观察框架，不能据此声称会自动提升在线核验。本轮视图只是表达方式假设，未建立跨租户资源认证。作者也说明漏采集的证据不可检查，从单条轨迹生成的合同仍需审核。 |
| `repro`：锁定完整实现与依赖，禁止不同实现混入续跑 | [Finding the Right Fit v1](https://arxiv.org/html/2610.00917v1)，2026-10-01；[作者代码](https://github.com/liyix/finding-the-right-fit)、[experiment.py](https://raw.githubusercontent.com/liyix/finding-the-right-fit/main/experiment.py) | 论文 §5.1 的失败反馈与恢复分析；仓库 README；源码 `canonical_hash`、`file_sha256`、`tree_manifest_sha256` 与环境/版本检查相关段落。未执行作者基准 | 论文要求在模型、harness、任务的组合上判断表现；代码以配置、版本和树指纹锁定实验身份。本仓库仅锁定 `response_verification.py` 会漏掉 `table_evidence.py` 等依赖，因此扩大实现/依赖指纹，并在 resume 前验证。预算、选择的样本、模型/端点也要保持可比较。 | 提升可复现性和结果可信度，不会直接提高单次回答成功率。当前远程 `main` 链接可变；若后续复制或运行作者代码，应先锁定 commit。论文的 CLI 任务结果不能外推到本项目业务。 |

上述三种方案分别改变错误修复归属、模型所见证据表达和实验身份约束。它们可以独立比较后组合；不能把三者同时修改后的成绩全部归因于某一论文。

## 下一轮评测可采用的配对方法

[HERA v1](https://arxiv.org/html/2610.06563v1)（2026-10-05；[作者仓库](https://github.com/lhannnn/HERA-Bench)）的 §3、附录 A.4/B，把可完成与不可完成任务组成受控环境变更对，且禁止 harness 优化修改工具语义、评分器、分组和预算。[AgentAbstain](https://agentabstain.github.io/)（[论文](https://arxiv.org/abs/2607.10059)、[代码](https://github.com/AntiQuality/agentabstain)）区分实际提交动作与最终回复，避免“先写入再拒绝”被评为成功。本轮阅读了这些方法/评分说明和仓库说明，没有下载执行基准或复现自动演化。

可在本项目手工建立少量配对开发任务：只改“新鲜读取/冲突后的旧读取”“明确授权/缺关键参数”“成功回执/结果未知”“合法原文引用/模型伪造引用”。对每对同时要求该行动时成功、该停止时停止，并检查最终业务状态、重复写入和回复。不要把“全部拒绝”当可靠性提升。HERA 的大规模自动环境生成与 harness 演化成本较高，当前先借鉴配对设计；AgentAbstain 的部分回答评分仍用 LLM，不能充当完全独立的确定性真值，其生成流水线也未开源。

## 副作用未知与幂等：后续工作，不能靠提示词闭合

[AWS Builders' Library：Making retries safe with idempotent APIs](https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/) 是较早但直接相关的官方工程资料。本轮读了请求标识、原子性、语义等价响应、延迟请求及相同 ID 不同参数的段落。其关键约束是：调用方稳定操作 ID、服务端同租户去重、参数一致性验证，以及去重记录与业务变更的原子提交。相同参数不一定表示同一次业务意图；“当前值已相同”也不一定证明由本次操作写成。

[LangGraph 官方 Checkpointers 文档](https://docs.langchain.com/oss/python/langgraph/checkpointers) 的 Replay、Pending writes、Durability modes 说明保存图状态与恢复方式；Replay 会重新执行检查点之后的节点，包括 API 调用。检查点不是外部业务写入 exactly-once 合同；不能因为有 `MemorySaver`、Postgres checkpointer 或同步落盘，就宣称重复副作用已经消除。

后续应在可信业务适配器增加与持久操作 ID 绑定的查询/对账路径，注入“提交前中断、提交后回执丢失、去重记录前后崩溃、并发重发、同 ID 改参数、跨租户相同 ID、过期/延迟请求”故障。对账不充分时保持 `unknown` 与停止后续写入；只有服务端合同证明安全，才考虑有界重发。本轮没有为此启动/重启 PostgreSQL，没有把本地替身检查当生产集成验证。

## 旧 research.md 引用核查

| 旧条目 | 核查结果 |
|---|---|
| `2610.00917` Finding the Right Fit | arXiv 标题与 2026-10-01 日期匹配；本轮增加了 §5.1 与作者源码阅读。 |
| `2605.27922` Harness-Bench | [arXiv 摘要](https://arxiv.org/abs/2605.27922)标题与 2026-10-15 v2 修订日期匹配，但摘要写 106 项，当前 [v2 HTML 正文](https://arxiv.org/html/2605.27922v2) Table 1、§3.1–3.2 写 300 项、1,408 modules。来源内部有矛盾；旧架构调研的 300 来自正文，不能直接判为捏造或简单改成 106。接入前应以锁定下载版本和任务清单实数为准。 |
| `2608.11727` Harness-IF | [arXiv](https://arxiv.org/abs/2608.11727)标题与 2026-08-12 日期匹配；本轮只核对摘要，未复现。 |
| `2510.04618` ACE | [arXiv](https://arxiv.org/abs/2510.04618)标题与首稿 2025-10-06 匹配，最新显示 2026-03-29 v3；[正文](https://arxiv.org/html/2510.04618v3)提供[作者代码](https://github.com/ace-agent/ace)。本轮未采用自动经验更新，避免与合同修复消融混合；没有新的 ACE 效果结论。 |
| `2408.04682` ToolSandbox | [arXiv](https://arxiv.org/abs/2408.04682)标题与首稿 2024-08-08 匹配，后有 2025-04-16 v2；本轮只核对摘要。 |
| `2506.07982` tau2-bench | [arXiv](https://arxiv.org/abs/2506.07982)标题与 2025-06-09 日期匹配；本轮只核对摘要，不增加本仓库既有桥接实验以外的复现声明。 |

检索和日期核对不能证明来源全部主张正确。对于最新预印本，暂作为明确标注的实验动机；官方工程文档用于约束实现语义，也不提供本项目的性能保证。

## 判断实验结果时的边界

- 程序合同测试、已保存原始输出回放和真实模型重新运行必须分栏。回放可以证明格式/引用修复路径，对同一草稿的真实语义误拒是否下降仍需新模型运行。
- 固定草稿核验、完整 Agent 任务完成和外部业务副作用是三类指标。某类测试通过不能替代另外两类。
- 开发集用于修改，验证集在实现冻结后作对照，封存测试留到最终方案冻结。重复使用一小批失败样本调参不能给出泛化结论。
- 至少同时报告正确草稿保留、错误声明放行、合同故障、调用数/token/耗时；端到端另加未授权/重复写入和最终业务状态。零误批准的小样本不代表生产风险为零。

本调研不读取密钥、不运行收费模型、不变更模型/数据库配置；三分支的实际执行由对应实验报告记录。没有复制或引入以上项目依赖。
