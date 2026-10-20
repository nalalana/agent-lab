# 公司表格写入路径的跨库边界诊断（2026-10-20）

## 结论

不能把上轮受控SQLite的原子幂等方案直接接到现有公司表格工具：单元格写入immudb，审计与版本位于Django数据库；两者不是同一个事务。底层存储客户端还内置异常重试。Agent外层门闩只能阻止外层重发，不能保证调用内部不会再次写入。

本轮把风险落为6组可执行隔离检查，并实现实验用持久门闩。6/6表示所列预期行为被复现，包括不安全行为，并不表示真实表格系统已修复。没有修改后端执行路径或部署，没有调用模型。

## 代码证据

- `backend/apps/agent_runtime/tools/project_tables.py`：公司表格工具调用OpenGateService；write schema只接收tid及r/c/value，没有操作ID或原始版本条件。写入结果将服务返回的版本信息转成message字符串。
- `backend/apps/opengate/services.py:1108`：upsert_table_cells在Django transaction.atomic内持有表记录锁，但通过ImmudbService逐个写入远端单元格；发生异常时本地事务回滚，远端已提交内容不会因此回滚。循环中返回False则可能保留部分成功写入及相应审计。
- `backend/common/immudbIO/services.py:145`：upsert_data具有坐标稳定主键，但没有本次逻辑操作的幂等键和状态查询合同。相同坐标的再次赋值不等于并发环境下可以安全重试。
- `backend/common/immudbIO/client.py:169`：upsertToTable捕获异常后继续sqlExec，RETRY_MAX_COUNT=10。重试发生在Agent拿到结果之前，外层门闩对此不可见。

这些是当前实验仓库继承后端代码的静态事实及隔离复现，不是线上事故调查。

## 测试方法及替身边界

`lab/run_table_boundary.py` 从当前源码AST提取未改写的upsert_table_cells、值转换、版本和审计辅助函数；另外提取原始upsertToTable方法与重试常量。manifest记录文件hash和Git版本。

使用真实Django模型、schema和SQLite事务保存表元数据及审计。远端immudb替换为另一份独立SQLite，每次写入独立提交；IAM和内容审核使用允许夹具。SDK测试替换SQL网络端点与sleep，在重试等待期间插入另一个写入。没有导入生产配置、连接真实immudb、验证PostgreSQL行锁或完整平台IAM。

这是源码方法级合同测试，不是完整DjangoWorkspaceBackendToolExecutor端到端集成。预期注入的异常日志保存在各输出目录的expected_faults.log。

## 六组结果

| 检查 | 实际观察 | 含义 |
|---|---|---|
| 远端提交后丢响应 | 服务返回False，远端值100，本地审计0条；另一写入改为200后，盲重试覆盖回100 | 本地事务不提供跨库原子性，失败不能推断无副作用 |
| 多单元格部分失败 | 首格变为10、第二格未写，整体False；审计为success/failed | 需要部分完成状态，不能把整个请求当作未执行 |
| 持久unknown门闩 | 新对象及新Python进程均阻止旧请求重发；同任务换新intent也不能绕过；中间写入200保留 | 验证外层停止规则，不证明存储层幂等 |
| 已确认历史回执重放 | 同intent只调用远端一次；权限回调撤销或payload变化则拒绝 | 重放也需要当前授权；回执只描述历史操作 |
| 原版本前置条件 | 中间写入后，携带原table_version的重试在调用远端前被拒绝 | 现有服务有可复用的版本保护，Agent接口尚未传递该条件 |
| 隐藏SDK重试 | 外层guard仅调用执行器一次，但SDK sqlExec两次；等待期间200被覆盖回100，外层最终收到成功并记录acknowledged | 外层门闩挡不住调用内部的重试；不能宣称端到端不重复写入 |

第六组的重要性：即使外层没有观察到unknown，内部也可能已经执行过多次。真实服务还有其他调用路径；本轮没有统计线上发生频率，也没有证明每次网络异常都会产生覆盖。

原版本检查能保护本地审计可见的中间更新，但不解决远端已变更、本地审计却未落盘的全部窗口，不能替代业务端操作账本。

## 实验包装器

`lab/legacy_write_guard.py` 在首次外层执行前，将可信scope/intent和payload hash持久化为unknown；只有完整成功回执才能转为acknowledged。False、异常、部分成功或进程中断都不触发自动重试。已有acknowledged结果只作为historical_receipt重放。

同一可信任务存在未解决unknown时，新intent也会被阻止。作用域和逻辑intent必须由可信运行时稳定绑定，不是模型参数。授权回调在执行、重放及状态查询前运行；本轮只验证回调被重新执行，实际IAM实现使用夹具，不能据此声称权限链已验证。

包装器没有自动解除unknown的接口，也没有伪造权威业务对账。它未接入生产Executor，只用于说明旧接口缺少幂等合同情况下可做的保守保护。现有底层SDK重试仍使它不具备端到端防重保障。

## 验证记录

正式完整6组运行代码 `89df3ba`，启动工作区干净；原始记录、ORM数据库、远端替身、门闩数据库及日志位于忽略目录 `lab/results/table-write-boundary-source-6/`。之前smoke和5组运行同样保留；第六组来自沿调用链新增的诊断，没有为了择优修改原案例。

本轮修改前的82项既有图回归通过，新增6组合同检查全部通过。模型调用0，未运行新的模型能力基准或RAGTruth保留集。

```powershell
E:/program/touming/.venv/Scripts/python.exe lab/run_table_boundary.py --output lab/results/<新的目录>
```

## 下一步顺序

1. 为Agent写入路径明确存储客户端重试策略：无业务幂等保证的变更，不能把超时自动当作可重复发送的错误；保留未知与部分完成语义，读查询与写入重试策略分开。
2. 将读取时取得的版本作为可信执行上下文保留，并传入写入前置条件，保留结构化回执；不让模型自行刷新版本来绕过冲突。
3. 在immudb一侧设计可原子提交的操作记录和状态查询，再验证审计对账与崩溃恢复。只有本地Django写一条operation记录不能补齐跨库合同。
4. 使用隔离的真实immudb与PostgreSQL补做网络故障、权限撤销和并发测试，再考虑后端回迁；当前源码级替身检查不替代这一步。
