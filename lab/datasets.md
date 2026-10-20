# 数据集

公开基准与透明公司业务集分别报告，公开数据不是业务授权/中文事实正确性的替代。

| 数据集 | 官方来源 | 用途 | 当前状态 |
|---|---|---|---|
| tau2-bench | https://github.com/sierra-research/tau2-bench | 多轮工具、业务规则、状态变更、协作 | 已获取源码与数据，版本见 datasets.lock.json；尚未接入本项目模型/执行图 |
| BFCL | https://github.com/ShishirPatil/gorilla/tree/main/berkeley-function-call-leaderboard | 工具选择、参数、无关工具、多轮调用 | 已定位官方来源，待独立环境接入 |
| ToolSandbox | https://github.com/apple-aiml-research/ToolSandbox | 状态依赖、缺信息和澄清 | 已定位官方来源，待接入 |

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
