# 显式提交凭证与写入对账

日期：2026-10-23。接续[旧入口收拢](2026-10-23-unified-writes.md)，起点`8fe6272`。综合分支`solution/reconciliation-20261023`，最终功能验收提交`e4ae8c6`；其后只补文档。原后端冲突工作区、业务库及安全策略未改，没有推送部署或真实模型调用。

## 结果与意义

新增显式启用的服务路径，使“远端已经提交，PG仍保留pending”的新协议操作能够凭匹配的提交证明完成对账。最终真实PG/immudb固定合同12家庭、24叶子全部通过，263项离线程序检查通过。旧四种写入入口与默认Agent工具没有切换到新路径，历史无凭证pending仍保持阻断。

| 固定合同 | 旧版对照 | 最终新路径 |
|---|---|---:|
| 正常原子提交，raw/cells/clear/delete | 不支持新API | 4/4 |
| 实际提交后丢ACK再对账，四种操作 | 不支持新API | 4/4 |
| 实际提交后子进程退出，cells/clear | 不支持新API | 2/2 |
| 新实例重复对账、两调用者并发对账 | 不支持新API | 2/2 |
| PG修复事务失败后再次对账 | 不支持新API | 1/1 |
| 无凭证、旧围栏、当前值恰好相同 | 不支持新API | 3/3安全拒绝 |
| 部分计划、请求/operation ID/scope不匹配 | 不支持新API | 4/4安全拒绝 |
| 在途竞争、错误表scope、无管理员权限 | 不支持新API | 3/3 |
| 事务内重复主键，业务与凭证同时回滚 | 不支持新API | 1/1 |

旧版24项明确标记`unsupported`，不能称为“安全0/24”或24次失败事故。对照还实际执行旧cells写入并在确认提交后丢回执，观察到typed unknown、持久pending、业务审计/版本未推进；该反例单列，不计新恢复成功。最终同字节v2判定器分别运行于旧生产代码和最终候选，case IDs一致、工作区clean。

旧路径在整合提交`0bcd8dc`的真实回归为：安全10/10、提交后进程退出2/2、旧入口13/13、raw/delete实际锁竞争2/2、跨入口cell版本3/3。其后只修改新方法和新测试；剔除本轮新节点后，最终services与起点的完整AST一致，旧判定器未改。各运行原有PG锁两项也通过，所有测试schema和immudb数据库已清理。上述分母不相加为办公任务成功率或生产可靠性。

## 最小实现

服务入口在`backend/apps/opengate/services.py`，SDK实现在`backend/common/immudbIO/recoverable.py`，均新增opt-in方法：

```python
submit_recoverable_write(
    user, table, operation, *, expected_table_version,
    values=None, cells=None, row_index=None,
    expected_max_row_index=None, client_ip=None,
)
reconcile_committed_write(user, table, operation_id, *, client_ip=None)
```

支持`raw_upsert/cells_upsert/clear/delete_row/append_row`；表版本必须显式提供。正常返回含服务器operation ID、`status=committed`、业务table/cell版本及`reconciled`。派发后的不明确结果抛携带operation ID的`RecoverableWriteUnknown`，不能据此重新提交业务。

1. 沿用原serializer、审核、写权限、表锁与版本检查，准备完整SDK payload及原业务审计计划。raw保留格式属性；更新保留已有cell_id，缺失坐标使用稳定ID；追加必须提供显式尾行并包含完整新行。
2. 首个独立durable PG事务在表锁下保存意图：operation ID、租户/资源/原actor绑定、请求和完整效果计划摘要、坐标审计计划。PG意图不保存单元格业务正文。无权限、scope不完整或版本冲突在创建远端服务前拒绝。
3. 第二PG事务持同表行锁，再检查唯一pending及版本。全部业务DML与唯一receipt INSERT放入同一显式immudb BEGIN/COMMIT；只派发一次。要求恰一个完整提交ACK，并通过`verifiableSQLGet`读取匹配receipt，其实际SQL entry事务ID还须等于ACK事务ID。
4. 获得正向证明后，刷新gate/User及资源归属、复查写权限，再原子完成原业务action审计、版本与该围栏。异常或PG提交不确定继续pending，不尝试远端回滚或重发。
5. 对账要求当前TABLE ADMIN（遵循现有IAM继承规则），锁表后按表+operation ID定位意图，重验scope、原actor、摘要、action、审计计划及原表版本；只读对应提交证明，不执行业务SQL。RPC返回后再查新快照和管理员权限。完成后重放返回同一历史结果，无新增审计或RPC；重放仍检查当前权限和scope。

凭证固定为8字段：协议版本、operation ID、scope/request/effect摘要、数据库、表、操作。SDK只解析固定两VARCHAR列的row framing，严格校验主键、schema、长度、规范JSON、完整字段和SDK verified状态；不把JSON自报tx_id当证明。receipt表需部署/迁移显式调用`ensure_receipt_table`，业务提交不自动做DDL。必须使用独立已登录client，避免`useDatabase`及root状态共享竞态。

这解决的是新协议操作的正向提交认领。凭证缺失、不可读、部分或不匹配一律unknown；没有“未执行”的反向推断、超时解锁、自动重试或顺带清除其他围栏。当前值相同不能认领历史写入。完成结果中的版本是该操作当时的提交版本，后续写入仍需重新读取当前可信版本，不能把重放结果当最新表快照。

## 隔离分支、冻结与复核

| 角色 | 分支 / 冻结提交 |
|---|---|
| 修改前协议 | `study/reconciliation-20261023` / `8b5f5fd` |
| 原子SDK与严格凭证 | `exp/atomic-operation-receipts` / `506ed4d` |
| PG新提交与正向对账 | `exp/recoverable-table-writes` / `87b85e5` |
| RPC返回后权限/归属复查 | 同分支 / `d6b681c` |
| 独立共同夹具与24项判定器 | `study/recovery-evaluator` / `ba8a5c0`、`8fd5be7` |
| User抽取依赖、v2回滚证据加强 | 同分支 / `c1cd8c8`、`37548e0` |
| 最终综合实现与验收 | `solution/reconciliation-20261023` / `e4ae8c6` |

修改前82项原图基线通过，随后冻结[12家庭/24叶协议](2026-10-23-reconciliation-protocol.md)。开发探针依次确认同事务业务+marker、重复marker回滚、丢ACK保留marker，以及SDK proof/实际事务ID；实际服务额外探针覆盖五种effect各正常/丢ACK共10项，包含append。它们为开发诊断，不冒充独立冻结24项，也不是随机任务准确率。

首轮v1的24项已全过；独立审阅发现回滚叶子只检查“注入标记+无ACK+空数据”不足以排除根本未执行SQL。因此保留首轮，v2在相同24 IDs上加强：实际SDK调用恰一次，且得到经本机探针和对应服务器源码核实的duplicate-PK RPC错误，才计为真实原子回滚。单元测试排除了本地TypeError、连接失败及其他UNKNOWN。最终重新运行旧版和候选，不降低原断言。

权限审阅还发现IAM Resource/Grant不受表锁约束。本轮新增2项离线测试（8个subcase）先复现RPC后旧快照漏洞，再补新快照检查，覆盖写权限、tenant、gate策略、原actor及管理员身份变化。夹具策略状态独立于被回滚的业务事务；这不是完整真实IAM并发实验。它缩小RPC期间的竞态窗口，不能保证权限变更与最终PG提交线性化。

并发场景使用独立SDK client和实际PG连接，通过pg_stat_activity确认等待行锁；等待中的恢复零RPC/零服务构造，释放后只完成一次业务审计。子进程仅传本地配置路径、测试schema、PK和操作，在真实完整提交ACK后退出码73；父进程新实例读取凭证完成恢复。修复PG失败时业务审计、版本、fence一起回滚，再核验成功但不重发。重复/并发叶子保存的最终状态另经汇总脚本复查：pending为0、版本为正且仅一条成功业务审计，不增列为新场景。

结果保留于`lab/results/reconciliation-20261023/`：正式对照`baseline-recovery-02`（unsupported）与`candidate-recovery-final`（24/24）、`acceptance-final/final-local-checks.json`（263/263）、`regression-*`，汇总`comparison.json`。原开发探针、v1及首次253项离线结果均保留。v1因Git CRLF转换原始SHA不同，不能作为严格字节对照；最终复制冻结文件同字节、刷新同blob索引确认无暂存差异后再运行v2，两侧SHA严格一致。没有覆盖旧结果。

## 一手资料与阅读深度

以本机实际服务器`1.11.2-bfdce03649f52d575be46f74425fd18eaf4fa69c`、Python SDK1.4.1为准，不按最新文档推断旧二进制能力。资料支持实现选择，实际能力另由本机测试确认。

- [官方SQL事务文档](https://immudb.io/docs/develop/sql/transactions/)：阅读Python完整示例及事务说明，使用显式BEGIN/COMMIT包裹多项DML。本机普通client没有newTx接口，因此采用原有sqlExec能力，不引入会话事务框架。
- [对应服务器SQLExec](https://github.com/codenotary/immudb/blob/bfdce03649f52d575be46f74425fd18eaf4fa69c/pkg/server/sql.go#L55)、[SQL引擎](https://github.com/codenotary/immudb/blob/bfdce03649f52d575be46f74425fd18eaf4fa69c/embedded/sql/engine.go#L712)：读取相关完整函数，核对未闭合事务及执行错误路径；RPC异常本身不能证明未提交。
- [对应VerifiableSQLGet](https://github.com/codenotary/immudb/blob/bfdce03649f52d575be46f74425fd18eaf4fa69c/pkg/database/sql.go#L27)、[协议字段](https://github.com/codenotary/immudb/blob/bfdce03649f52d575be46f74425fd18eaf4fa69c/pkg/api/schema/schema.proto#L1195)、[row编码](https://github.com/codenotary/immudb/blob/bfdce03649f52d575be46f74425fd18eaf4fa69c/embedded/sql/catalog.go#L1605)：读取相关完整函数/消息及编码分支，核对实际entry事务ID和固定row framing；不是完整数据库源码审计。atTx选择精确写版本，不当作通用as-of snapshot。
- [重复PK分支](https://github.com/codenotary/immudb/blob/bfdce03649f52d575be46f74425fd18eaf4fa69c/embedded/sql/stmt.go#L1437)、[错误定义](https://github.com/codenotary/immudb/blob/bfdce03649f52d575be46f74425fd18eaf4fa69c/embedded/store/immustore.go#L80)、[服务端映射](https://github.com/codenotary/immudb/blob/bfdce03649f52d575be46f74425fd18eaf4fa69c/pkg/server/errors.go#L57)、[实际grpc-go依赖转换](https://github.com/grpc/grpc-go/blob/v1.82.1/status/status.go#L87)：完整阅读相关分支/函数，解释本机duplicate-PK为RpcError/UNKNOWN/固定key-already-exists文本；只用于测试分类，不变成业务自动解锁依据。
- 本地SDK1.4.1的sqlexec、verifiedSQLGet handler及RootService相关完整函数已读，官方仓库master作交叉核对；未读到可用的1.4.1 tag，不能声称对该tag审计。[官方查询说明](https://immudb.io/docs/develop/sql/querying/)的相关段落作语义核对，行修订号不当作operation提交凭证。

## 环境、边界与下一步

本机专用PG18.6/immudb1.11.2，loopback端口55432/53322，每次独立schema/test_UUID库，最终清理均完成。诊断PG使用sslmode=require；独立readiness曾通过verify-full，不混称本轮均做主机名验证。IAM写入/admin及审核为允许夹具，真实Tenant/Resource绑定和存储引擎保留。构造函数替换为独立已登录client；SDK调用计数不含夹具登录/建receipt表，另计服务构造次数，不能解释为包含认证成本的RPC总量。263项程序检查和24项固定故障合同均不等于真实模型成绩或主流办公软件对照。

- 仅新协议意图可对账。旧无凭证围栏、派发前崩溃、原actor删除/uid变化、tenant或资源重新绑定、列数变化等情况继续fail closed，可能长期阻断；没有历史迁移或管理员强制清除入口。
- 凭证可信性依赖受信后端一次写入完整事务、受保护的意图与receipt写权限。部分计划诊断同时改变effect摘要，验证不匹配拒绝；不证明能识别恶意写者执行部分业务后伪造完全匹配的marker。当前SDK默认root缓存为内存状态，未建立跨重启的持久可信根；不证明能抵御恶意数据库管理员、receipt任意改写或服务端身份替换。接入前需制定专用权限、持久root及数据库升级兼容方案。
- 不提供跨PG/immudb原子回滚、分布式exactly-once、全故障窗口证明或自动恢复调度。固定schema decoder及错误签名随服务器/SDK升级必须重新验证。
- 默认HTTP/Agent/runtime/SSE/recorder/UI仍走原协议。不能在现有Agent unknown门禁内直接自动调用新提交或重试；接入应先传递operation ID、呈现待核验状态，由授权管理员调用对账，完成后重新读取可信版本。
- 生命周期create/delete_db、结构/权限变更、直接SDK和其他存证域未纳入统一围栏。完整生产IAM/审核、HTTP管理员对账入口、前端状态展示、平台隔离联调、吞吐及真实模型端到端仍待完成。

当前可进入透明数据空间的隔离联调阶段，尚不足以开放无人值守生产写入。优先补齐管理员对账入口和客户端状态合同，再跑完整授权/撤权、runtime事件流与前端交互；默认切换应以这些结果为依据。

## 复现

沿用已有专用测试配置，输出必须新目录；不打印/提交配置，连接失败不回退替身。旧版缺API返回unsupported并非零退出，不能据退出码解释为已支持恢复但安全失败。

```powershell
../.venv/Scripts/python.exe lab/run_baseline.py --output lab/results/<新基线目录>/baseline.json
../.venv/Scripts/python.exe lab/test_atomic_operation_receipts.py
../.venv/Scripts/python.exe lab/test_recoverable_writes.py
../.venv/Scripts/python.exe lab/test_realdb_recovery.py
../.venv/Scripts/python.exe lab/run_realdb_recovery.py --config ../.local-db/realdb.json --output lab/results/<新恢复目录>
```
