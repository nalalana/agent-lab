# 引用合同与语义核验的后续对照

日期：2026-10-22。综合分支：`solution/harness-reviewed-20261022`。功能基底为上一轮 `edef74e`（功能代码最后提交 `31e0d1c`）；本轮没有把未获端到端支持的候选设为默认。新增可复用的配对诊断构建器、隔离实验与本报告，所有候选代码保留在独立分支。

## 综合决定

继续采用上一轮的证据作用域、核验故障分流及已报告用量、续跑身份锁定、可信未知变更硬停。生产核验器仍为 `strategy=evidence_scope`，逐字引用、来源类型、路径、权限和时间归属合同不放松。详见[首轮综合报告](2026-10-22-harness-study.md)。

本轮四种方法均不替换默认：原文分块未改善开发结果；专用事实核验器通过大量拒绝减少漏检，同时增加误拒；短引用提示的开发单例收益未在验证集延续；原文片段目录修复了三个构造样本的转义引用故障，但公开长文故障仍在，冲突场景还出现成功写入后无法交付。保留这些负结果比叠加全部提示更符合现有证据。

这不是通用幻觉问题已解决的结论。当前综合版本本轮公开验证为34/48，仍有2条相对标签的误批准；标签本身也有口径和遗漏问题。业务、静态核验和程序合同成绩分别报告。

## 问题定位与资料依据

公开开发样本 `ragtruth-10321` 的来源是原始 JSON 文档。模型引用中的两段文本来自 JSON 解码后的 review：普通引号和实际换行，与原始 content 中的转义字符不同。因此原校验器拒绝是正确行为。不能增加 JSON 解码、模糊匹配或自动改写模型引文来“修好分数”。

另有语义判断问题：模型把 great service 到 excellent service、分手到 ex-boyfriend 等忠实改写按词面不同拒绝；把自身引用复制失败说成草稿合并句的分号不在来源。这表明“草稿语义支持”与“引文必须逐字”的职责仍有串扰。缩短提示或只强调事实核验没有消除它。

本轮沿用同日已核查的[一手资料与阅读深度记录](2026-10-22-harness-sources.md)，没有声称完整复现作者系统：

- [VReAct v3](https://arxiv.org/html/2609.31937v3)（2026-10-18修订）启发区分确定性合同与语义判断；只是小规模预印本证据。
- [GroundEval v2](https://arxiv.org/html/2606.22737v2)及[作者代码](https://github.com/tenurehq/groundeval)启发保留来源、时间和权限合同；不提供在线核验准确率保证。
- [Finding the Right Fit](https://arxiv.org/html/2610.00917v1)及[作者代码](https://github.com/liyix/finding-the-right-fit)支持在固定模型和预算下比较 harness 组合。
- [HERA](https://arxiv.org/html/2610.06563v1)与[作者代码](https://github.com/lhannnn/HERA-Bench)启发正反配对诊断；这里是手工小样本设计，不是运行其基准。
- [Pydantic AI 输出校验](https://pydantic.dev/docs/ai/core-concepts/output/#output-validators)及[Guardrails reask 源码](https://github.com/guardrails-ai/guardrails/blob/main/guardrails/actions/reask.py)启发有界输出修复。没有引入这些框架或增加模型轮次。

原文分块、短引用指导、专用事实提示和片段目录是本项目的工程假设，不应当记为上述论文已经证明有效的方法。

## 隔离、冻结与协议

先运行并保存82项生产图离线基线，再提交[本轮协议](2026-10-22-verifier-followup-protocol.md)与配对构建器 `lab/prepare_verifier_followup.py`。四种方法使用独立 worktree，冻结提交后运行，运行期间未切换其代码。

| 分支 | 冻结提交 | 改动范围 |
|---|---|---|
| exp/verifier-followup-baseline | edef74e | 当前综合实现对照，非最初历史 baseline |
| exp/verifier-source-blocks | 54bab25 | 模型输入按来源分块；原文和 validator 不变 |
| exp/verifier-faithfulness | c491a11 | 固定草稿专用事实核验；只作职责消融，不替代完整权限/任务核验 |
| exp/verifier-short-quotes | 94badd3 | 仅追加短连续引文指导 |
| exp/verifier-quote-catalog | c47a578 | 仅在已有一次合同修复轮追加原文片段目录 |

片段目录是假设补充，依据前三种开发失败后登记，不冒充最初预注册。目录仅含原 source_id、start/end 和原始 content 切片；不解码、不生成新 sources、不自动批准、不修改引文。优先实际内容来源，最多64项、每项160字符、额外消息不超过8000字符。该字符限制不是整个请求或 token 上限。长文前缀可能占满目录，片段可截断限定语，因此仍须核对完整原始来源，不能据目录缺失推断事实不存在。

新增32条双语正反诊断，开发/验证各16条、各4类结构，验证不复用开发结构。开发覆盖引号/换行、字面路径、七天营业与冲突归属；验证覆盖Unicode/反斜杠、规则例外、部分支持及嵌入指令。只有8类结构，不能视为32个独立自然任务或新公共基准。

公开开发24条、验证48条沿用上一轮，原来源、草稿、gold不改；这些数据已暴露，不声称独立未见。显式模型 `qwen3.8-27b`、temperature=0、固定草稿并发4、每条120秒和核验预算保持相同；封存test未读取、未运行。各目录manifest锁定源码、环境、数据、选择、模型和端点身份。

## 固定草稿结果

正确数相对于冻结布尔标签；合同错误计错。误拒/误批准只计实际返回对应裁决的样本，错误另列。

| 方法 | 诊断开发/16 | 公开开发/24 | 公开误拒/误批/错误 | 诊断验证/16 | 公开验证/48 | 验证误拒/误批/错误 |
|---|---:|---:|---|---:|---:|---|
| 当前综合基线 | 14 | 15 | 5 / 3 / 1 | 15 | 34 | 12 / 2 / 0 |
| 原文分块 | 13 | 15 | 5 / 3 / 1 | 未运行 | 未运行 | — |
| 专用事实核验 | 14 | 14 | 9 / 0 / 1 | 未运行 | 未运行 | — |
| 短引用提示 | 14 | 16 | 5 / 2 / 1 | 15 | 32 | 14 / 2 / 0 |
| 修复轮片段目录 | 16 | 14 | 6 / 3 / 1 | 16 | 33 | 13 / 2 / 0 |

分块与专用事实核验未通过开发筛选，停止扩大运行；未把未运行记为0。专用事实核验公开21/24判拒绝，新增3次标签命中伴随4次新增误拒，净退1；0误批不等于可靠性全面改善。短引用开发唯一改善2440有实际年龄冲突依据，但验证净退2，未据验证继续调提示。

目录确实修复了开发中英文 quoted-review 及验证 unicode-escape 的三个合同故障，原本错误批准为0的诊断仍无新增错误批准。但公开10321仍失败：64项在原文前缀约739字符处已耗尽，目录尚未覆盖后面的review正文。不能说模型忽略了已经提供的相关引文。

目录公开开发唯一新增误拒1461发生在首轮，未使用目录；其首轮请求与对照相同。目录公开验证48条全部首轮结束，没有触发目录，而两组总分仍不同。temperature=0下相同方案也会出现输出差异，不能把这些差分或 token 降低归因于目录本身。

原标签疑点单独保留：13389数值、8442营业时间与原始来源存在冲突；14897涉及implicit_true口径。它们没有被改gold或删除。代理辅助审阅不是外部独立人工盲标，不能用“修正分数”消除真正的同义误拒和标注争议。

## 完整执行图与采用否决

两组均复用原Phase2图、相同30条业务和6条冲突夹具，无新Agent循环；工具后端是独立SQLite替身，不等于生产数据库。

| 方法 | 业务正常完成/30 | 未知变更安全停止 | 冲突正常交付/6 | 冲突状态合同/6 |
|---|---:|---:|---:|---:|
| 当前综合基线 | 28 | 2/2 | 6 | 6 |
| 修复轮片段目录 | 28 | 2/2 | 5 | 6 |

业务旧评分原样保留28/30；不能把28正常完成+2安全停止改成30次任务完成。两组failure-03/04均满足规范unknown错误、不可重试、无草稿发布、无后续模型/工具、无后续回执、保留原始超时码这六项安全断言。旁路结果 `outcome-safety.json` 没有替换原grader。

目录候选normal-write已提交一次，但后续核验未正常交付。两组都曾语义revise后追加读取，未重复提交；基线把历史观察、操作成功和当前观察分别引用后交付。候选却把三者合成company_fact并引用写入回执，触发真实来源类别错误；修复轮确实收到目录，随后又将目录碎片拼成三条不存在的原文引文，最终VERIFIER_CONTRACT_ERROR、答案为空。没有进入时间归属审核，不能把它归因于该审核。

状态合同通过不能掩盖交付回归，也不能重跑成功变更来修答案。这一实测足以否决其默认采用；局部引用修复能力保留在实验分支。详细故障归因见本轮忽略目录中的语义审阅。

| 观察成本 | 基线调用/已报告token | 目录调用/已报告token | 基线p50/p95秒 | 目录p50/p95秒 |
|---|---|---|---|---|
| 诊断开发16 | 18 / 46668 | 18 / 50651 | 1.44 / 3.81 | 1.16 / 3.26 |
| 诊断验证16 | 17 / 43195 | 17 / 45223 | 1.48 / 3.00 | 1.22 / 2.88 |
| 公开验证48 | 50 / 218888 | 48 / 202113 | 1.78 / 9.22 | 1.44 / 6.84 |
| 冲突6 | 54 / 149787 | 58 / 172187 | 5.73 / 10.32 | 6.33 / 11.50 |

目录诊断的调用上限不变，但修复请求更长，token分别增加约8.5%/4.7%；冲突调用、token及延迟增加且交付下降。延迟是服务/并发条件下的观察值，不是确定性加速结论。未配置价格，不估算金额。

## 复现、检查与结果位置

最终综合代码沿用已测功能；本轮167项离线检查通过（82项生产图基线及85项辅助检查），记录在 `lab/results/verifier-followup-20261022/acceptance/final-local-checks.json`，执行提交966ecf3，之后仅改文档和分支名称。这是程序检查，不是模型准确率。目录分支另外通过8项新合同检查，原82项基线、既有核验及业务harness检查也通过；原文分块的旧脚本化模型测试有1项因硬编码字符串JSON输入而失败，候选专用图检查通过，未隐藏或改旧测试。

在本机使用 `E:/program/touming/.venv/Scripts/python.exe`，从仓库根目录运行，输出须用新目录：

```powershell
../.venv/Scripts/python.exe lab/prepare_verifier_followup.py --output lab/results/<新配对语料目录>
../.venv/Scripts/python.exe lab/run_harness_study.py --checkout E:/program/touming/agent-lab-quote-catalog --phase dev --corpus lab/results/<新配对语料目录> --output lab/results/<新开发运行目录>
../.venv/Scripts/python.exe lab/run_harness_study.py --checkout E:/program/touming/agent-lab-quote-catalog --phase validation --corpus lab/results/<新配对语料目录> --output lab/results/<新验证运行目录>
../.venv/Scripts/python.exe lab/run_harness_study.py --checkout E:/program/touming/agent-lab-quote-catalog --phase business --output lab/results/<新业务运行目录>
../.venv/Scripts/python.exe lab/run_harness_study.py --checkout E:/program/touming/agent-lab-quote-catalog --phase conflict --output lab/results/<新冲突运行目录>
```

改checkout可重跑其他冻结分支；faithfulness仅固定草稿runner接入候选，不能用其业务入口冒充职责消融端到端成绩。配置由原仓库忽略的显式本地环境注入，不向worktree复制密钥。

全部原始数据、轨迹、逐ID配对和usage保留于忽略目录 `lab/results/verifier-followup-20261022/`：`comparison.json`、`study-totals.json`、`outcome-safety.json`、`dev-review-faithfulness.md`、`dev-review-catalog.md`（含端到端归因）与20个结果目录。不提交数据、业务快照、轨迹或凭据，未推送、未部署、未启动或测试真实PostgreSQL/immudb。

本轮464次真实模型trial、794次调用，接口已报告2389075 token（输入2245699、输出143376）；包括失败和被否决方案。与上一轮合计1934次trial、3612次调用、9811223 token，55个结果目录。这只是消耗总量，不能把不同样本与重复运行合并成准确率；usage逐trial只累计一次，不重复加verdict/runtime_usage。

本轮剩余问题的优先级是：建立独立语义复核数据，拆分真实漏检、同义误拒、引用故障和gold口径；为成功写入后的草稿核验提供独立恢复入口，保持业务不重发；在权威查询和业务端原子幂等合同完善前，unknown继续硬停。本轮不继续扩大提示拼接或据同一验证集追分。
