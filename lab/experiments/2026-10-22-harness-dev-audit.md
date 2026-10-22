# Harness 开发样本错误审阅

日期：2026-10-22。范围仅限本轮 `baseline-dev` 与 `evidence-dev` 的 synthetic、ragtruth 开发样本。证据来自相应 `trials.jsonl`、`summary.json` 和 `failures/*.json`；公共标注只按这 9 个已选开发样本 ID 查询原始记录，未审阅 validation 或 test。

这是执行代理作出的人工/模型辅助语义审阅，**不是外部独立人工标注，也不是盲审**。没有修改数据真值、候选代码、提示或运行结果。下面的分类用于解释机械分数，不另行生成“修正准确率”。

## 业务开发集：4 条改善、1 条退化

原始结果：[baseline synthetic](../results/harness-study-20261022/baseline-dev/synthetic/summary.json)、[evidence synthetic](../results/harness-study-20261022/evidence-dev/synthetic/summary.json)。

baseline 为 58/64，其中 5 次误拒、1 次错误；evidence 为 61/64，其中 3 次误拒、0 次错误。两组本轮均没有机械判定的错误批准。逐 ID 对比发现 4 条改善、1 条退化，净增 3 条：

| 完整 ID | baseline → evidence | 轨迹所见与解释 |
|---|---|---|
| `synthetic-entity-0092-unknown-clear-zh-1` | `revise` → `supported` | baseline 将当前值未确认归为公司事实，随后以无证据为由拒绝；evidence 正确保留不确定性说明。 |
| `synthetic-entity-0082-unknown-clear-zh-1` | `revise` → `supported` | baseline 把当前值未确认标成 metadata，再将工具回执与类别不兼容归罪草稿；evidence 正确批准。 |
| `synthetic-entity-0082-write-conflict-zh-1` | `revise` → `supported` | baseline 为不确定性引用不可引用的 `invalidated_by` 属性，重询后拒绝；evidence 正确区分历史读取、冲突回执及当前不确定性。 |
| `synthetic-entity-0092-snapshot-en-0` | `ValueError` → 正确 `revise` | baseline 首轮输出大量感叹号直至长度截断，重询后错路由为知识库权限报告；evidence 首轮发现库存数值不符。该改善包含一次输出/路由异常的消失，不能全部归因于时序推理增强。 |
| `synthetic-entity-0082-refresh-timeout-zh-1` | `supported` → `revise` | evidence 将当前未知列为无证据 metadata；重询明知该不确定性应从 claims 省略，却仍把自身 claims 问题作为拒绝草稿的理由。 |

evidence 的其余两条失败是 `synthetic-entity-0092-refresh-timeout-zh-1`、`synthetic-entity-0092-write-conflict-zh-1`。三条剩余失败都属于同一个问题：校验器把正确的不确定性说明强行列入必须举证的 claims，再把自身输出合同错误转成草稿错误。write-conflict 还曾尝试把 `table_evidence_scope` 当作引用来源，虽然它不在合法 sources 中；本地 validator 继续阻止了该引用。

这支持“简明证据视图对部分样本有帮助”的开发假设，但没有证明误拒已解决。该批仅两个实体组，模板相同，且只有一次模型运行；61/64 不能解释为稳健的泛化收益或端到端任务完成率。

## 公共 RAGTruth：相同 15/24 的组成

原始结果：[baseline ragtruth](../results/harness-study-20261022/baseline-dev/ragtruth/summary.json)、[evidence ragtruth](../results/harness-study-20261022/evidence-dev/ragtruth/summary.json)。

两组均为 15/24：9 次正确拒绝、6 次正确支持、5 次机械误拒、3 次机械漏检、1 次合同错误。24 条的预测 verdict 与 correct 标记逐 ID 完全相同。9 个失败样本首轮 system/user 输入经逐字比较也完全相同；公共样本通过 `workspace.read` 提供来源，没有触发表格时序视图。两组回答理由个别措辞不同，但相同得分符合候选未改变这些输入的情况，不能作为证据视图对相关表格问题无效的独立结论。

| 开发样本 ID | 机械类别 | 本次语义审阅分类 | 依据与限制 |
|---|---|---|---|
| `ragtruth-13389` | 误拒 | 原标遗漏/严格口径差异 | 原标没有幻觉 span，但草稿水量为 `1-4/5`，原材料为 `1-1/4`。本次拒绝存在明确数值依据，不适合简单归为纯误拒。 |
| `ragtruth-8442` | 误拒 | 原标遗漏 | 原标无幻觉 span，但草稿将工作日概括为 18:00 开门，材料中的 Monday 是 19:00。拒绝有明确依据。 |
| `ragtruth-5080` | 误拒 | 评测口径扩大，混有过严判断 | 原标未标注错误，草稿自称 92 words，正文约 52。核验另对合理概括是否逐字出现作了过严判断，但自报字数确实不符；当前核验范围超出原标注的单纯来源幻觉口径。 |
| `ragtruth-10260` | 误拒 | 真正语义误拒 | 来源列齐七天营业时间。baseline 认为没有明说每天营业，evidence 更错误声称没有 Sunday；两者都没有正确理解结构化记录支持的概括。 |
| `ragtruth-6966` | 误拒 | 过于逐字/语义口径争议 | 来源 `lot=true`、`street=true`；草稿提及 off-street 与 lot。仅因没有逐字出现 off-street 而拒绝，存在不接受合理语义推断的问题；该例有解释空间，不充当确定真值修订。 |
| `ragtruth-16173` | 漏检 | 真正语义漏检 | 来源只给出带例外的词尾一般规则，核验却将其当成某个具体名词的确定证明；引用原文存在不代表完整结论得到充分支持。 |
| `ragtruth-2440` | 漏检 | 原标争议，同时存在真实语义缺陷 | 原标只标年龄 55 冲突，但来源同时记载警方给 54、其他报道给 55。草稿还将“相信溺亡”提升成确证，并添加 swimming；核验没有保留这些证据的不确定性。 |
| `ragtruth-14897` | 漏检 | 明确标注口径差异 | 唯一 span 是 website，且原标 `implicit_true=true`。材料描述下载按钮、下一页和插件安装，website 属于可理解的隐含信息；当前以 `bool(labels)` 一律记错误，保留了上游口径，也把该类隐含真实信息计成机械漏检。 |
| `ragtruth-10321` | 错误 | 引用合同/适配脆弱性 | 两次输出都把 JSON 序列化来源中的转义内容按解码文本引用，不是来源 content 的严格子串。本地拒绝符合原文字串合同；不能据此认定该草稿语义已被证明错误。 |

## 分数可相信到什么程度

机械分数可以相信为“当前运行结果与当前布尔标签的一致程度”。但 15/24 混合了标注遗漏、`implicit_true` 口径、摘要自报字数检查、语义误拒/漏检及引用格式故障，不能直接解释为真实语义准确率或官方 RAGTruth span-level 成绩。

业务集的净增 3 条值得保留为开发信号，但剩余错误说明格式重询与草稿修订仍可能混淆；公共样本还展示了严格逐字合同在多层 JSON 表示上的脆弱性。应保留原始分数和这份审阅并列呈现，不在本轮冻结后修改标签、放宽引用合同或继续追着这些样本调提示。下一轮若要建立独立语义真值，应预先确定口径并由未参与候选优化的标注者审阅。

本文件只保存已完成的开发样本审阅，没有新模型调用、候选修改或进一步数据调研。
