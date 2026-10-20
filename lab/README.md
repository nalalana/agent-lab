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
