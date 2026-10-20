# 数据集

公开基准与透明公司业务集分别报告，公开数据不是业务授权/中文事实正确性的替代。

| 数据集 | 官方来源 | 用途 | 当前状态 |
|---|---|---|---|
| tau2-bench | https://github.com/sierra-research/tau2-bench | 多轮工具、业务规则、状态变更、协作 | 已有独立环境及原生参考入口 run_tau2.py；生产执行图适配尚未完成 |
| RAGTruth | https://github.com/ParticleMedia/RAGTruth | 核验器拒绝与人工幻觉标注的对照 | 已完成200条适配诊断；入口 run_ragtruth.py |
| BFCL | https://github.com/ShishirPatil/gorilla/tree/main/berkeley-function-call-leaderboard | 工具选择、参数、无关工具、多轮调用 | 已定位官方来源，待独立环境接入 |
| ToolSandbox | https://github.com/apple-aiml-research/ToolSandbox | 状态依赖、缺信息和澄清 | 已定位官方来源，待接入 |
| AgentDojo | https://github.com/ethz-spylab/agentdojo | 工具输出中的指令注入与任务完成 | 已定位官方来源，待接入 |

## 本地数据与复现

`lab/datasets/` 保存上游 checkout，不提交第三方整份数据或运行产物。`datasets.lock.json` 记录实际 commit。
本轮检查原始 `domains/*/tasks.json`：airline 50、retail 114、telecom 2285、banking_knowledge 97、mock 10；
intake 的文件顶层不是同类任务列表，不直接计入可运行任务数。原始池大小不等于官方冻结 benchmark 的任务量，执行时按官方 split/benchmark 选择。
复现 tau2 数据：

```powershell
git clone https://github.com/sierra-research/tau2-bench.git lab/datasets/tau2-bench
# 用 datasets.lock.json 中的 revision 替换下方值
git -C lab/datasets/tau2-bench checkout --detach <revision>
```

按该版本官方 README 建独立环境并使用官方命令与评分器。首次运行前检查数据及代码许可和模型调用成本。
任何裁剪/转换记录来源、task id、split 与转换规则；不能把内部适配后的得分标为官方榜单成绩。
不要在业务知识库、训练样本或记忆中放入保留集答案。

## RAGTruth 核验器诊断

复现前先按 `datasets.lock.json` 克隆并检出 RAGTruth 对应提交；使用后端依赖环境：

```powershell
git clone https://github.com/ParticleMedia/RAGTruth.git lab/datasets/RAGTruth
git -C lab/datasets/RAGTruth checkout --detach c103204b9ce28d6bbad859304bf30de72b8ed8fe
python lab/test_public_evals.py
python lab/run_ragtruth.py --dry-run
python lab/run_ragtruth.py --limit 200 --concurrency 4
```

选择规则固定为 seed42、test split、quality=good、按任务与人工正负标签轮流抽样，底层 source_id 不重复。
200条包含 QA66、Summary66、Data2txt68，正负样本各100；采样利用标签平衡，模型输入绝不包含标签或标注说明。
同上游 prepare_dataset.py，存在任意标注 span 即为阳性，包括 implicit_true；此定义判断来源支持性，不是现实世界真假。

适配复用生产 ModelResponseVerifier：资料作为一次成功的 workspace.read 结果，保留原回答，仅追加统一 Source 文件路径以满足生产引用契约。
请求根据 task_type 重建为核对资料支持的任务（QA保留问题）；不是原始生成指令逐字重放，不运行检索或重新生成答案。
revise 作为检测阳性的代理，supported 为阴性；核验器同时检查任务完成、归因等，因此 revise 不一定仅因幻觉，误报需要人工复核。
无法输出有效证据对象的样本独立计为 errors，不计为检测阳性。混淆矩阵与 F1 仅针对已判定样本，同时报告全样本错误数和将错误视为不正确的准确率。

输出保存 manifest、原始模型调用、逐条判断和 summary。含样本级 precision/recall/F1、误报漏报、按任务分类和全部调用 token。
这不是官方词级 span 检测成绩，也不是当前公司 Agent 的整体成绩。平衡采样的 precision 不能直接外推线上分布。
本轮已使用公开 test 子集；不得根据这批标签反复调参。后续优化从 train 分离开发集，并冻结独立保留集。

## τ² 原生参考入口

独立安装，避免 LiteLLM/OpenAI 版本影响后端环境（Windows 示例）：

```powershell
python -m venv lab/.venv
lab/.venv/Scripts/python.exe -m pip install -e lab/datasets/tau2-bench
lab/.venv/Scripts/python.exe lab/run_tau2.py --dry-run
lab/.venv/Scripts/python.exe lab/run_tau2.py --limit 1
lab/.venv/Scripts/python.exe lab/run_tau2.py --limit 10
```

共用已忽略的 `lab/.env.local` 模型配置，默认 retail/train、seed42、每例最多50步/180秒，顺序运行。
直接复用原生 Agent scaffold、用户模拟器、业务环境和评分逻辑；不把案例转成普通问答，不把隐藏目标动作提供给 Agent。
用户、Agent、通信裁判与自然语言断言裁判使用同一可用模型；上游写死的自然语言断言裁判模型通过模块常量显式覆盖，仅换模型配置、不改评分逻辑，该差异写入 manifest。
这缺少独立裁判，不遵循官方默认模型协议，也尚未使用我们的生产 Phase2 图。结果仅作参考与接线验证，不能宣传为生产 Agent 或官方排行榜成绩。
可用 `--task <id>` 指定任务；`--split` 默认 train，不将公开 test 当作开发集。
原始轨迹、环境评分、运行配置、依赖版本与任务ID写入新的 `lab/results/tau2-native-*`。
