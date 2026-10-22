# 公司表格回执的执行状态与业务结果分离

日期：2026-10-22。冻结实现8c89a2c，仅实验仓库，未部署。

## 实现

ToolResult增加execution_status和business_outcome，旧五参数构造继续可用；默认值returned/unknown表示执行器返回了结果，业务结果尚未得到新的结构合同确认。没有把success=True批量改标业务成功。ToolExecutionError也可携带这两项，缺省unconfirmed/unknown。

ToolDefinition增加effect_class，默认unknown；公司项目列表与表格读取显式标read_only，表格写入/清空标mutates。该元数据进入工具快照，不是模型可填参数，不猜测未知工具只读。

| 可信路径 | execution_status | business_outcome |
|---|---|---|
| 公司表格读取/列表成功 | returned | not_applicable |
| 公司表格写入/清空服务完整确认 | returned | succeeded |
| 身份/权限解析拒绝、路由参数拒绝、缺少必要读取 | not_dispatched | not_attempted |
| 服务版本冲突，未执行本次变更 | returned | conflict |
| 已有OPERATION_OUTCOME_UNKNOWN | unconfirmed | unknown |
| 未迁移工具的旧成功结果 | returned | unknown |

not_dispatched指业务操作派发前的可信拒绝，不能用来声称没有发生任何配置查询或授权查询。returned/conflict表示可信服务返回冲突结果，不表示远端变更成功。succeeded只表示该次操作已获确认，不代表值永远保持不变，也不解决跨库原子性。

图内tool_results与相应完成/失败事件记录两轴状态及副作用类别。已迁移工具结果中附加runtime_outcome，由运行时覆盖结果内容中同名的伪造字段；回答核验的tool_receipt再单独传递可信根级runtime_outcome。核验规则明确：mutates的returned不能证明完成，只有succeeded能支持完成声明；结果文本中的“成功”不能覆盖可信状态。

未知错误码仍强制覆盖为unconfirmed/unknown、success=false、非retryable，保留原有硬停止。没有新增自动重试，也没有按自由文本推断结果。其余元数据本轮主要用于回执、审计与回答核验，未将所有旧工具的unknown自动转为硬停止；完整恢复消费仍需逐个适配器迁移。

## 兼容与范围

原图回归最初发现4处兼容失败：给旧工具的成功原文统一包装JSON，会破坏既有字符串返回合同。已修正为未声明新合同的旧成功工具保留原文；声明了业务/副作用合同的工具才附加状态，失败路径沿用已有恢复包装。未通过修改原有断言掩盖兼容问题，修正后原82项全部通过。

BusinessEnvironment的SQLite提交成功路径也显式标注succeeded，保持实验夹具与本轮合同一致。其他业务拒绝、sandbox、原生tau桥接没有被自动当作业务成功。workspace文件变更生产者仍未逐个迁移，默认unknown；读数据的not_applicable不表示读取失败。

通用ValueError与未分类异常仍保守保持unknown，未据错误消息推断“执行前拒绝”。resource_key、observed_version、共享证据视图、校验器自身错误分流尚未实现。旧success和原审计completed表示仍可能被旧消费者使用，不能只凭这些旧字段统计业务成功率；新状态未完成全平台下游迁移或数据库审计模型升级。

## 验证

111项本地检查通过：原图82、存储7、读取版本5、恢复2、冲突夹具2、新鲜度4、业务入口6、新回执3。新检查使用原图和源码提取的真实workspace适配器方法，替换身份/服务边界；验证旧成功默认unknown、注册副作用、已确认写入与读取状态、权限拒绝、未知结果、可信元数据覆盖伪造文本，以及未知结果继续停止。不是整个平台IAM或数据库集成测试。

真实模型合同冒烟：qwen3.8-27b，6条固定草稿，全部判断正确。3条不支持的“写入完成”声明分别基于returned/unknown、returned/rejected和伪造成功内容，均被拒绝；succeeded确认、执行前拒绝和未知结果的正确说明均获批准。

结果目录lab/results/receipt-contract-real-6/，manifest记录冻结提交、dirty=false及样本指纹。6次模型调用、13526输入token、349输出token、共13875 token，串行约5.12秒。6条只验证合同语义，不是新的能力成功率，不能替代480条业务验证、2700条公开验证或1963条封存测试。本轮没有重跑大样本集，不声称65条误拒已下降。

复现：`python lab/test_outcome_metadata.py`；真实模型冒烟使用 `python lab/run_freshness_eval.py --receipt-contract --output lab/results/<新目录>`。来源状态与期望标签分开，标签不进入模型输入。

## 后续

下一步补充可信资源/版本绑定，将写入版本门禁与回答核验的两套时序扫描合并为共享证据视图。再逐步迁移其他工具、校验器故障及恢复控制；用开发集修改，用大样本验证比较，测试集继续封存。PostgreSQL测试保持停止。
