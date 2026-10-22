# Harness合同设计与现有调用链映射

日期：2026-10-22。目标是复用现有Phase2执行图，把工具回执、证据和恢复规则连接起来。下文明确区分已实现与待验证设计；没有重写Agent循环，也没有新增多Agent或长期记忆框架。

## 已确认的问题

480条正式业务验证为412正确、65误拒、0误批准、3合同错误。主要是校验器故障与草稿错误混淆，不能只用额外审核兜底。该批验证用于问题定位，后续调参应使用13253条开发集；测试1963条继续封存。

源码还有一处直接冲突：OpenGate版本冲突在执行前被拒绝，表格适配器要求重新读取；但ToolExecutionError进入统一失败处理后，会被附加“不要重复调用该工具”。模型即使已重读且用户仍授权，也会收到不一致指导。

## 现有链路

```mermaid
flowchart LR
    A[模型产生工具调用] --> B[route_tool 参数与权限校验]
    B --> C[execute_tool 派发与超时边界]
    C --> D[Backend或Sandbox执行器]
    D --> E[可信业务适配器与服务]
    E --> F[ToolResult或ToolExecutionError]
    F --> G[运行回执与审计事件]
    G --> H[模型续写或终止]
    H --> I[回答证据校验]
    I --> J[发布或有界修订]
```

| 职责 | 当前实现 | 差距 |
|---|---|---|
| 输入权限与参数 | tools/registry.py、definitions.py、project_tables.py | 已有，不应重复实现 |
| 回执合同 | contracts.ToolResult、ToolExecutionError | success、content、error_code、retryable混合运输与业务含义 |
| 图内工具结果 | orchestration/tool_nodes.py | 返回值和异常两条路径、失败指令不同；只有特定写工具超时进入unknown |
| 业务写入版本 | ToolContext.expected_table_version、_table_read_version | 使用可信读取，已实现；按工具名列举，尚无通用副作用元数据 |
| 回答证据时序 | response_verification._invalidated_table_reads | 和版本检查各自扫描回执；规则可能漂移 |
| 基准历史读取 | lab/tau_graph_worker.ReadEvidenceCache | 独立适配缓存，不能当作生产统一证据合同 |
| 原生基准回执 | lab/operation_outcome、tau_graph_worker | returned明确不等于业务成功，与核心回执尚未统一 |
| 回答校验修复 | response_verification.ModelResponseVerifier | 自身引用合同可修复一次，但仍可能把自身错误改判为草稿revise |
| 主循环限制 | Phase2AgentRuntimeFacade、MAX_RESPONSE_CORRECTIONS | 已有时间、工具轮数、答复修订上限；尚无按错误类型恢复预算 |
| 未知操作持久恢复 | lab/durable_counter、legacy_write_guard | 实验合同，未成为生产跨run幂等或自动对账 |

## 合同一：工具结果分开表达执行与业务结果（待实现）

保留ToolResult外形以便迁移，逐个可信执行器补充结构化元数据，不能从模型文本或错误关键词推断业务状态。

| 字段 | 拟定含义 | 谁负责 |
|---|---|---|
| execution_status | not_dispatched、returned、unconfirmed | 可信派发器/执行器 |
| business_outcome | not_attempted、not_applicable、succeeded、rejected、conflict、unknown | 可信业务适配器；缺确认则unknown |
| effect_class | read_only、mutates、unknown | 工具注册定义，不接受模型参数 |
| resource_key | 公司/资源类型/资源ID的可信绑定 | 权限解析后的适配器 |
| observed_version | 读取或成功变更的业务版本，可空 | 权威服务回执 |
| recovery_action | 规范恢复动作，不是自动重试授权 | Harness按可信错误分类选择 |

业务拒绝可能是execution_status=returned，但business_outcome=rejected；传输失败也可能已经提交，故不能把success=false翻译为not_attempted。现有任意工具返回success=true也不能直接迁移为业务succeeded；必须核实该工具生产者的合同。

迁移顺序为公司表格工具→workspace变更→sandbox及原生基准适配器。先审计生产者，再新增可选字段；不根据工具名猜测未知工具只读。此次没有实现这些新字段，也没有改变ToolResult的构造签名。

验收：同一状态的返回值、抛异常、外层超时都生成一致回执；只有可靠的执行前拒绝才声称未执行；unknown不能成为成功去重键或后续自动写入的依据。审计保留模型调用ID和实际工具调用ID。

## 合同二：共享证据状态（待实现）

最小做法是从当前run可信回执派生共享视图，供写入版本检查、回答校验和恢复决策共同读取；先用普通结构和已有state，不另建向量库、证据数据库或通用事件平台。

每项证据至少包含source_id、可信resource_key、run范围、observed_version、原内容、来源工具调用ID、observed/historical/unconfirmed状态及失效原因。公司/用户/Agent隔离由原权限上下文提供；tid相同不能跨租户共享。

状态规则：

- 成功读取产生“最近一次观察”，不保证无人于之后修改。
- 后续同资源读取替代旧观察；后续修改尝试使旧观察失效，即使操作失败也先保守失效。
- 版本冲突证明不能沿用旧版本，但没有证明新值是什么；unknown也不能推断没有副作用。
- 新的成功读取才恢复该资源可用观察。其他资源不被牵连。
- 历史观察可以说明当时读取值；操作回执只能说明对应操作状态，不能替代内容证据。

迁移时先对当前两套扫描函数做等价测试，覆盖不同表、版本0、失败刷新、连续冲突、部分写入、恶意内容。基准缓存只在能提供同等资源绑定与生命周期事件时接入；不能把别名来源自动当作新读取。跨run持久恢复暂不在此步骤承诺。

验收：版本门禁与回答校验对同一回执序列给出相同失效边界；历史证据不会授权新写入，也不会独自证明最新值。不能靠删掉所有历史材料来避免时序问题，否则正确历史说明仍会被误拒。

## 合同三：按类型恢复（已落地第一步）

本轮新增tools/recovery.py中的单一recovery_policy，按可信错误码选择动作。核心执行图的ToolResult失败与ToolExecutionError失败都附加runtime_recovery，并保留顶层recovery_action及生产者retryable提示。runtime_recovery由运行时覆盖，不能由结果文本伪造。

| 错误 | 动作 | 限制 |
|---|---|---|
| TABLE_READ_REQUIRED | read_before_write | 先读，再按用户原始授权与条件决定是否修改 |
| TABLE/CELL/ROW版本冲突 | reread_and_recheck_intent | 不得原样重试；条件变化保留他人修改，遵守用户重试限制 |
| TOOL_INVALID_ARGUMENTS | correct_arguments | 只改授权范围内的明确参数，不猜测缺失内容 |
| 权限/风险拒绝 | explain_denial | 不提升权限、不绕过限制 |
| OPERATION_OUTCOME_UNKNOWN | stop_and_reconcile | 保留原硬停止；不重发，不声称已回滚 |
| 未分类错误 | stop_and_explain | 无可靠副作用元数据时不自动重试 |

所有动作的automatic_retry_allowed=false。retryable只是生产者提示，与自动重发许可分开；unknown一律保留非retryable。规则不会自动读取、自动修改或创造新的授权。已有unknown终止控制仍由代码执行，不依赖模型理解指令。

这只是当前核心图恢复语义的一致化，不代表状态机已覆盖所有适配器。tau桥接原生工具边界暂未统一；runtime_recovery目前是可信JSON指导，模型仍可能不遵守。后续需要对错误类型设置独立恢复预算，并通过实际图行为验收。

## 下一步实施次序

1. 回执归一化：审计表格生产者，补充effect_class及执行/业务两轴状态，再扩展其他工具。保留旧字段，不一次改所有调用方。
2. 证据共享：将两个时序扫描点合并为共享视图，以现有事件序列的等价测试证明行为一致，再接入基准缓存。
3. 校验器故障分流：引用不存在、类别不兼容、JSON不合法属于verifier_contract_error，只进入校验器自身修复预算；真正不支持的内容才进入draft_revision。不能通过放松来源合同来减少误拒，也不能靠关键词识别“当前未知”。
4. 分别衡量校验器与完整Agent：开发集修改；3180条验证集比较；再构建实际工具、故障和混合任务的端到端集。1963条封存测试只在最终冻结使用，不把固定草稿成绩当作任务完成率。

每步需要同时看正确草稿保留率、错误声明批准率、合同错误、未授权/重复写入、任务完成、token与延迟。重试提示统一的本轮改动不声称降低65条误拒，也没有新的真实模型成绩或部署结论。

本轮验证：原图基线82、存储未知结果7、读取版本5、冲突夹具2、恢复合同2，共98项本地检查通过。恢复合同检查覆盖返回失败/抛异常两条路径的一致性、保留原始错误、覆盖结果文本伪造的runtime_recovery，以及retryable提示不等于自动重试授权。现有7项存储检查继续验证unknown停止排队调用与模型续写。
