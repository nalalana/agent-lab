# 企业办公 Agent 架构调研与本项目差距（2026-10-20）

## 判断

公开材料支持的判断是：WorkBuddy、Claude 和 Microsoft 365 Copilot 在运行基础设施、办公集成、企业治理方面展示了更完整的能力；不足以证明其未公开的推理、规划或核验算法必然强于本项目。没有做同模型、同数据、同权限及同预算的产品对测，不能报告它们比我们高多少分，也不能把产品功能说明当作独立性能证明。

本项目已有现代Agent骨架。当前优先级是改善运行、证据和验证合同，而非因产品宣传推倒LangGraph重写，或直接叠加多Agent。

## 阅读范围与来源

下表均为原始论文或厂商一手资料。论文阅读范围是针对本项目问题的章节阅读，没有完整复现、审计所有附录或重新计算论文成绩。论文报告的收益不直接外推到qwen3.8-27b。2026年10月论文目前为预印本；AgentArch为较早的补充对照，不称其为最新论文。

| 来源 | 时间/版本 | 本轮阅读范围 | 可用信息与局限 |
|---|---|---|---|
| [Finding the Right Fit](https://arxiv.org/html/2610.00917v1) | 2026-10-01，v1 | 摘要、§1、§4.3、§5、§6相关段落 | 模型与harness存在交互，反馈、继续执行和结束条件值得单独比较；以终端任务为主，不能据此给办公产品排名 |
| [Harness-Bench](https://arxiv.org/html/2605.27922v2) | 2026-10-15修订，v2；初稿为5月 | 摘要、任务/评分说明、§5.3–5.4、§6 | 以300项沙箱工作任务的最终产物及模块检查衡量交付，同时计成本；适合作为办公产物评测候选，尚未接入本仓库 |
| [Enterprise Reference Architecture](https://arxiv.org/html/2610.14818v1) | 2026-10-10，v1 | §3–4职责与边界、§5案例、§6评估相关段落 | 区分编排/执行、运行状态/可复用知识、系统身份/资源授权、证据生产/关联；案例验证架构边界，不证明通用任务更高分 |
| [AgentArch](https://arxiv.org/html/2509.10769v1) | 2025-09，v1 | §3方法、§4结果、§5讨论 | 企业模拟任务比较单/多Agent、function calling/ReAct等组合；最终判断正确和工具执行完全正确并非同一指标，多Agent收益依模型和任务变化 |
| [WorkBuddy Managed Agents产品架构](https://intl.cloud.tencent.com/zh/products/workbuddy-managed-agents) | 访问2026-10-20 | 四层Harness、Runtime和FAQ文字 | 厂商披露的能力，不是内核源码或独立性能验证 |
| [WMA使用说明](https://cloud.tencent.cn/document/product/1831/138968) | 更新2026-09-30 | 创建、ADK、Skills、连接器、凭证、版本管理 | 可验证公开接口与配置对象；文档仍注明白名单内测，不推断已全面可用 |
| [Claude Cowork架构](https://support.claude.com/en/articles/14479288-claude-cowork-architecture-overview) | 访问2026-10-20，页面显示当周更新 | 执行位置、云端与本地隔离、权限边界 | 云端默认模式与既有本地模式需分开；不能沿用“全部只在本地运行”的旧描述 |
| [Scaling Managed Agents](https://www.anthropic.com/engineering/managed-agents) | 2026-04-08 | Session/Harness/Sandbox解耦、恢复、凭据、上下文段落 | 公开了具体工程机制；Managed Agents是平台服务，不能把全部细节无证据地归给Cowork |
| [Microsoft Copilot架构](https://learn.microsoft.com/en-us/microsoft-365/copilot/microsoft-365-copilot-architecture) | 访问2026-10-20 | grounding数据流、用户授权范围 | Microsoft Graph提供工作数据，访问受用户权限约束；不是所有Copilot产品的统一内部实现 |
| [Microsoft连接Agent模式](https://learn.microsoft.com/en-us/microsoft-365/copilot/employee-self-service/connecting-agents-overview) | 更新2026-06-12 | Hub、handoff、层级建议 | 适用Employee Self-Service场景；建议保持浅层，不等于全产品只有此架构 |
| [Declarative agent架构](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/declarative-agent-architecture) | 更新2026-07-13 | 配置、控制权、适用性限制 | 声明式Agent与自定义引擎不同；不能把该模式的限制外推给Copilot Studio/Cowork全部能力 |

## 产品采用什么架构

### WorkBuddy：应用、Harness、托管运行时

官方明确披露编排、记忆、行动、管控四层。WMA提供云端隔离执行、长任务和生命周期管理；Skills、MCP/OAuth/A2A连接器与版本管理构成扩展及交付接口。文档描述凭证集中托管及运行时换发令牌。上述信息足以说明平台范围，尚不能确认内部采用哪种规划算法、是否所有任务都启用多Agent，或是否有与我们相同的逐条事实核验。产品页的性能与可靠性宣传未独立测量。[架构](https://intl.cloud.tencent.com/zh/products/workbuddy-managed-agents)、[使用说明](https://cloud.tencent.cn/document/product/1831/138968)。

### Claude：会话、决策循环、执行环境分别管理

Managed Agents工程文明确分离持久Session日志、Harness循环、Sandbox执行；运行进程可依据日志恢复，凭据通过外置代理/vault使用。Session保存的证据不应等同于当前上下文窗口。[工程说明](https://www.anthropic.com/engineering/managed-agents)。

Cowork当前公开架构支持默认云端会话沙箱，既有桌面部署另有本地循环与VM隔离路径；每次本地工具访问仍受权限约束。该文主要披露执行与隔离，没有公开足够信息推断其完整规划/核验拓扑。[Cowork说明](https://support.claude.com/en/articles/14479288-claude-cowork-architecture-overview)。

### Microsoft：授权工作数据与任务编排

Copilot用Microsoft Graph取得用户有权访问的工作数据进行grounding。Employee Self-Service另公开Hub统筹和领域Agent接管两种模式，建议控制层级深度。声明式Agent则复用平台编排，有自己的复杂流程限制；它不是微软全部Agent的能力上限。[数据流](https://learn.microsoft.com/en-us/microsoft-365/copilot/microsoft-365-copilot-architecture)、[连接模式](https://learn.microsoft.com/en-us/microsoft-365/copilot/employee-self-service/connecting-agents-overview)、[声明式模式](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/declarative-agent-architecture)。

## 与现有代码对照

以下判断来自agent-lab快照main `3eceb8e`，继承后端develop `e67c05a`；不是对仍在变动的线上部署进行审计。代码存在不等于生产能力已验证，未运行的集成项明确保留未知。

| 维度 | 当前代码/实验事实 | 差距与判断 |
|---|---|---|
| 决策与工具循环 | orchestration/graph.py：LangGraph状态图、function calling循环、预算和取消；ToolRouter负责schema与权限 | 基本模式与现代Agent相符；没有证据说明换框架本身有益 |
| Skills与配置版本 | config/resolver.py、prompt_builder.py、snapshot.py：技能绑定、版本与配置校验和 | 并非没有Skills。当前prompt构造会展开已绑定技能正文；大技能库按需发现/加载仍需单独设计和测量 |
| 工作数据与授权 | company表格/文件工具、ToolRouter、后端资源授权路径 | 已有资源权限基础；连接企业应用的覆盖广度、身份委派及跨系统治理仍需逐项验证 |
| 记忆 | memory/namespaces.py、nodes.py：company/agent/user/session隔离、检索注入及长度限制 | 已有长期记忆，不能再说需要从零加memory；tau2实验禁用它，尚无长期记忆效果结论 |
| 执行与隔离 | tools/executors.py有Backend/Sandbox executor；registry声明网络策略 | 有沙箱接口和既有测试，不宣称没有隔离；本轮未验证其与厂商云端恢复/隔离的等价性 |
| 追踪与恢复 | runtime.py持久化AgentRunEvent；lab桥接用MemorySaver暂停恢复 | 已有事件记录不等于可在任意进程崩溃后恢复同一业务执行。需做杀进程、恢复及不重复写入的故障注入 |
| 证据生命周期 | lab新增任务内READ缓存和写前失效，默认关闭 | 10条正常完成2→5是积极信号；并发变更、资源版本、写后观察仍未解决 |
| 核验与完成 | tool_nodes.py每次无工具草稿进入核验，纠错后可再生成；核验器兼顾多类判断 | 已复现事实、政策、操作结果归因混杂；应先建立可检查合同，不能把多加一个LLM裁判当作根治 |
| 多Agent | 本轮主要评测单执行图；未比较专门领域Agent方案 | 适合跨领域职责或独立任务时再测；现在没有证据表明增加协作者能修复写后核验问题 |
| 评测交付物 | RAGTruth、tau2及合成业务案例；有轨迹、状态和成本指标 | 缺少真正办公产物、跨应用流程和重复运行可靠性验证；Harness-Bench可作为新增候选 |

相关本地代码：`backend/apps/agent_runtime/`；先前实验：[跨轮证据报告](2026-10-20-read-evidence-lifecycle.md)。

## 对本项目最有价值的研究启发

1. 不按“是否多Agent”判先进。Finding the Right Fit支持交叉评估模型和harness；AgentArch提示企业任务的正确判断、正确工具和正确参数应分别测。本项目只评一个qwen配置，不能区分模型限制和执行框架不匹配。
2. 保留证据只是起点。Harness-Bench要求检查真实交付物；我们的reward成功但回答终止同样说明局部成功不等于完整交付。应保留原始证据可追溯引用和未完成要求，避免摘要抹掉关键字段。
3. 分离职责不代表拆成十个微服务。参考架构区分运行状态与知识、编排与执行，支持在现有代码边界中演进；这不是要求照搬全部层数。
4. 厂商核心可学之处是运行合同与恢复机制。是否有MCP/A2A并不自动提升正确率；对我们，可信执行回执、资源版本和失败后的状态核对更直接。

以上为结合资料和本地失败轨迹的工程判断，不是论文已证明本项目应采用的最终方案。

## 建议按顺序做的实验（本轮没有实现）

| 顺序 | 单独改动 | 如何判断有用 |
|---|---|---|
| P0 | 操作结果归因：将真实回执直接返回字段、读后状态和推断分开；不能把任意写入输出变成通用事实 | 同组10条开发任务比较；增加“回执成功但状态未变”“超时后实际成功”“总结新增未返回字段”的反例；同时统计误报和漏报 |
| P0 | 核验职责分离：硬权限由执行器执行，事实核验不承担所有政策/任务裁决；业务不变量用确定性规则检查 | 单独消融，不与缓存/模型切换混跑；比较正常完成、错误放行、核验终止及成本 |
| P1 | 模型×现有harness小矩阵，固定任务和预算；原生参考、现图、候选分别记录 | 重复运行并使用独立裁判或确定性状态/产物评分；目前没有新增模型授权和配置，不擅自接服务 |
| P1 | 故障恢复：持久运行状态、原始证据、工具幂等键；写入结果未知时先核对状态 | 在工具调用前后杀worker，验证恢复后不会重复业务写入；不能仅重试HTTP请求 |
| P1 | 增加办公产物套件，先审核Harness-Bench许可证/环境和任务可运行性，再选小规模固定开发集 | 检查表格单元格、文档必备内容、源数据覆盖和禁改资源；不能只让LLM读最终回复打分 |
| P2 | 工具/技能数量确实造成上下文负担后，再测按需加载；跨HR/财务/项目等职责明确后，再测浅层领域Agent | 证明工具选择、事实正确率、延迟或成本有可重复收益，才增加架构复杂度 |

建议目标形态：保留现有单Agent循环和独立工具执行，补上任务状态、证据来源/失效、可靠恢复、清晰的验证合同与可检查产物；只在独立业务域有实证收益时引入浅层协作。

本轮仅调研与记录，没有改生产架构、下载新数据集、切换模型或启动收费评测。
