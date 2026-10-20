# 数据集

公开基准与透明公司业务集分别报告，公开数据不是业务授权/中文事实正确性的替代。

| 数据集 | 官方来源 | 用途 | 当前状态 |
|---|---|---|---|
| tau2-bench | https://github.com/sierra-research/tau2-bench | 多轮工具、业务规则、状态变更、协作 | run_tau2.py已支持原生参考与生产图外部执行适配，均完成10条评分 |
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

已增加train内分组：`SHA256(source_id)%5==0`为holdout，其余为dev。`ragtruth-partitions.json`固定seed42下各100条ID；两组source不交叉，官方test另行冻结。

```powershell
python lab/run_ragtruth.py --split train --pool dev --limit 100
python lab/run_ragtruth.py --split train --pool dev --limit 100 --prompt-suffix lab/prompts/semantic-entailment.txt
```

`--prompt-suffix`仅在评测进程中追加候选提示词，manifest保存实际提示词SHA256，不修改生产源文件。当前候选在开发集上误报与漏报都增加，已拒绝采用；holdout尚未运行，不用它反复挑提示词。

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

## τ² 生产执行图适配

保留两个独立依赖环境。原生τ²进程中的`GraphAgent`通过JSON行协议启动后端环境中的`tau_graph_worker.py`：

```powershell
$env:AGENT_LAB_BACKEND_PYTHON='E:/program/touming/.venv/Scripts/python.exe'
lab/.venv/Scripts/python.exe lab/test_tau_native.py
python lab/test_tau_graph.py
lab/.venv/Scripts/python.exe lab/run_tau2.py --agent graph --limit 1
lab/.venv/Scripts/python.exe lab/run_tau2.py --agent graph --limit 10
```

将上方路径换成安装了后端依赖的Python绝对路径。密钥仍从忽略的`lab/.env.local`加载，不经桥接消息传输。
复用生产Phase2图、模型网关、分类器、核验器和ToolRouter；从原图builder编译检查点，在`execute_tool`前暂停。
真实调用交给原生τ²环境执行，再校验回执ID并恢复原图后续节点。多个并行工具请求顺序交给原生环境，避免遗漏调用或隐藏状态修改。
工厂不向worker传task对象、标准答案、评估条件或隐藏状态，只传可见政策与工具schema；初始化只接受标准可见历史。

为复用已验证的内容来源类型，只有原生工具元数据标为READ、且mutates_state=false的成功结果，才在核验器输入的副本中映射成table_content来源；原始工具回执与原生评分轨迹不改写。
失败结果和写入结果不提升为事实来源。这个映射仅存在于评测适配器，不修改生产公司的工具目录或授权。

生产图已知的FAILED状态通过明确的平台终止消息和原生AGENT_STOP传给评分器，保留失败标记；真正的桥接异常单独计为errors。
summary分别报告环境reward、`graph_runtime_failures`、`reliable_reward_one`，并提供错误按0计算的整体均值，不能只看排除错误后的得分。

仍不是整个平台端到端成绩：公司IAM、Web入口、生产数据库和记忆服务不在该适配范围。使用原生领域政策及适配指令；每用户轮120秒/8次工具的生产图预算，原生参考没有这组额外限制，也没有额外分类/核验调用。
因此可以比较同任务的两套完整配置，但不能宣称严格相同token预算的架构因果收益，亦不遵循官方默认评审协议。
每个worker的依赖版本、只读工具名单、真实模型输入及输出保存在本地`graph_traces/`；任务结束关闭worker。
