# 单元格写入不确定时停止自动重试（2026-10-21）

## 结果

在agent-lab后端快照中移除了upsertToTable的异常重发，并将未知结果分类贯通到Agent执行图。应用层遇到未确认写入只调用一次sqlExec，随后当前run停止后续工具和模型续写；读取重试保持原样。未回迁原后端或部署。

113项本地检查通过（既有106项+新增7项），新增7项在python -O下重复通过。正式7组源码边界检查通过。检查验证确定性控制路径，不是模型能力分数；本轮模型调用0。

## 变更链路

1. `common/immudbIO/client.py`：upsertToTable不再循环重试。执行异常、没有提交事务回执或仍处于ongoing状态时抛出UnconfirmedWriteError。普通成功仍返回原响应；显式活动事务仍由事务调用者管理，保留阶段性响应。读取simpleSelect的原有重试未改变。
2. `common/immudbIO/errors.py`：轻量异常携带OPERATION_OUTCOME_UNKNOWN及固定不确定说明，保留底层异常cause，不根据任意工具文本猜测结果。
3. `apps/opengate/services.py`：upsert_table_cells不吞掉类型化未知；Django事务退出后异常向上传递。该变化不使远端写入随本地事务回滚。
4. `tools/project_tables.py`：write/clear_cells调用旧服务得到False时保守视为未知，因为可能已有部分远端效果；调用服务前的参数校验仍为ValueError。read失败不提升为写入未知。
5. `tools/workspace.py`：把类型化未知保留为工具回执error_code，不转为TOOL_INVALID_ARGUMENTS或泛化INTERNAL_ERROR。
6. `orchestration/tool_nodes.py`：收到未知回执或类型化工具执行错误后FAILED/reconcile_tool、retryable=false；不再调用模型，不执行排队工具，不将该调用记为完成去重键。公司表格write/clear_cells的外层执行超时也进入同一路径。失败回执保留真实tool_call_id，便于关联审计。

以上行为在实验仓库的相关代码路径默认生效；原生tau2外部执行适配器仍有自己的边界，不能把这里的修改视为它已经执行了真实immudb。

## 验证

新增 `lab/test_storage_retry.py` 使用实际toumingClient类、替代RPC端点及原Phase2图：

- 异常写入调用一次，不sleep，不重发，保留异常cause。
- 空事务回执、缺失回执、ongoing事务回执不能冒充已提交；确认判断不用assert，python -O不改变结论。
- 普通写入和显式活动事务维持相应返回合同；只读查询首次失败后仍能重试成功。
- 执行器源码方法保留未知错误码；表格源码方法将旧False保留为未知，但执行前非法参数与读失败仍走原类别。
- 原Phase2图的工具返回未知、抛类型化执行错误、表格写入超时三条路径，都只调用一次生成器和一次执行器；queued写入未执行，output为空，没有完成去重键，终止码和审计错误码一致。

为避开生产配置依赖，workspace/project_tables部分测试提取原源码方法并替换身份与服务边界，未做完整IAM集成。图与客户端类则直接导入执行。

正式边界运行代码 `5c1873a`，启动工作区干净，结果在本地忽略目录 `lab/results/table-upsert-no-retry-final-7/`。它使用真实Django ORM、独立SQLite远端替身和源码方法，未连接真实immudb/PostgreSQL。

| 场景 | 修改前观察 | 修改后观察 |
|---|---|---|
| 存储封装层异常重试 | 外层1次执行、sqlExec2次，中间写入200被覆盖回100 | sqlExec1次，保持unknown；之后写入200及被阻断的外层重试不改变该值 |
| 类型化未知经过表格服务 | 可能泛化为普通失败 | 保留专用异常；远端已写但本地审计0条的窗口仍存在 |
| 多单元格部分失败 | 第一格成功、第二格失败，整体False | 物理部分写入仍存在；公司工具层不再据False推断未执行 |
| 外层未知重发/新intent绕过 | 已有实验门闩 | 继续通过；门闩仍未接入生产Executor的跨run持久恢复 |
| 回执重放授权/原版本检查 | 已有实验检查 | 继续通过；不因此宣称完整IAM或跨库原子性成立 |

不同类型检查分别报告。7/7包含“仍能复现跨库窗口”等预期，不表示7项风险全部消除；修改前结果保留在2026-10-20报告和原目录。

## 调试记录

初次烟测 `table-no-retry-smoke-7` 捕获新增导入的缩进错误，未用于结论。修正导入位置并逐一解析全部改动Python文件后，修正烟测7组通过；提交冻结代码后又完成上述正式运行。所有原始目录均保留，没有以重试模型输出来挑选高分。

## 复现

```powershell
E:/program/touming/.venv/Scripts/python.exe lab/test_storage_retry.py
E:/program/touming/.venv/Scripts/python.exe -O lab/test_storage_retry.py
E:/program/touming/.venv/Scripts/python.exe lab/run_table_boundary.py --output lab/results/<新目录>
```

## 限制与下一步

- 只移除了自有upsert封装中的自动重试。insertToTable等其他写入口及实际SDK/gRPC内部行为未验证，不能声称所有存储写入只会发送一次。
- unknown是应用执行状态，不是业务失败。已经发出的请求或后台同步线程仍可能稍后提交；停止后续调用不等于撤销原请求。
- 原服务False缺少阶段信息，故保守停止；审核拒绝或执行前权限变化等明确未写场景也可能被归为未知，增加人工核验。后续需要执行器返回明确的未发送/已拒绝/部分完成/未知合同，不能靠错误文本猜测。
- SDK错误从旧返回False变为专用异常，只有已检查的调用链完成分类贯通；其他OpenGate入口仍需独立审计和集成测试。
- 当前run停止不等于跨run持久幂等。实验LegacyWriteGuard和持久操作账本仍未接入真实存储合同；用户开新run仍可能再次请求写入。
- 下一步把读取时的原始版本保留为可信上下文并作为写入前置条件，而不是重试时刷新版本；然后在隔离真实数据库上验证并发、权限变化、网络故障和原生事务行为。更完整恢复仍需要业务端原子操作账本与权威查询。
