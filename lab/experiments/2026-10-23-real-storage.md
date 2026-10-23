# 真实双库对照与受管写入故障隔离

同日后续：[旧写入口收拢与跨入口版本保护](2026-10-23-unified-writes.md)已覆盖两条旧数据入口并修正cell版本汇总；本文保留前一阶段范围。环境中的verify-full指独立连接探针；诊断runner实际为sslmode=require，详见后续口径说明。

日期：2026-10-23。接续[昨日准备报告](2026-10-22-local-databases.md)，按[本轮协议](2026-10-23-real-storage-protocol.md)实施。综合分支为 `solution/real-storage-20261023`，最终功能验收提交 `164f7bb`；之后仅补本文和入口说明。原后端工作区、业务数据库及系统安全策略未修改，没有推送或部署。

## 结论

PostgreSQL/psql 18.6 与 immudb 1.11.2 已在本地实际连接。原方案真实复现了第二单元格参数错误导致部分提交、远端提交但 PG 审计回滚、进程退出后新 run 再次改写等问题。采用“整批预检 + 派发前持久围栏”后，固定安全场景从 1/10 提升至 10/10，另列的真实进程退出场景从 0/2 至 2/2，最终 204 项离线检查通过。

这些是开发故障场景的合同结果，不是生产事故率、办公任务完成率或模型能力分数。本轮模型调用为 0。改动将受管路径推进到可验证的故障隔离，尚不能直接宣称透明数据空间全平台写入安全或无人值守可用。

## 环境和方法

- PG：本机专用 `agent_lab_test` 角色/库，`127.0.0.1:55432`，TLS verify-full；immudb：`127.0.0.1:53322`，认证开启，隔离新数据目录，其他协议入口关闭。
- 用户手动启动文件后，复核官方服务器文件 SHA-256 仍为 `dd41048e1f62b7fd65a01acb10be8213e7d16fcf79a59a0d59ea51c00ae1c41c`。保留其默认启动生成的数据目录，改用专用配置和新目录；未解除或绕过应用控制。
- 实际 SDK 为 immudb-py 1.4.1，PG 驱动 psycopg2-binary 2.9.12。使用源文件抽取的 OpenGate、ImmudbService、toumingClient 和原 Agent 适配器。IAM/审核为允许夹具，未启动完整平台或读取生产配置。
- 每次生成独立 PG schema、`test_<UUID>` immudb 数据库和 UUID 表；所有已完成真实运行的临时资源均清理成功。各组运行时 Git 工作区干净，保存提交、源码和判定器摘要。
- 基线先运行 82 项离线检查，并完成原六项真实双库诊断。后者的 6/6 表示成功观察预期风险，不表示六项安全成功。旧报告及失败结果保留，没有修改原断言来美化方案。

新安全判定器在共同基础设施分支冻结，四种方案使用相同文件 SHA 和 10 个 case ID：正常写入/清空、第二单元格非法时零写入，以及 write/clear 丢确认 × 原版本/重新读取 × 重试 write/clear 的八种组合。每次重试使用新 writer 和新 ToolContext，不使用 SQLite unknown guard；同时检查无新增远端变更、保留已提交值、pending 表不能取得可信读取版本、其他表仍可正常写入。另保留两种顺序的真实 PG 锁竞争。

补充进程退出判定器与上述 10 例分开：真实 SDK 确认提交后，子进程分别 `os._exit(71/72)`，父进程核对退出码、实际远端值和业务 PG 审计回滚，再检查新 run 的 write/clear 拦截。它只覆盖两个精确故障点，未覆盖任意网络分区、数据库宕机或所有提交模糊窗口。

## 独立分支对照

| 方案 | 固定安全场景 | 另列 PG 锁 | 实际评测提交 | 结果目录 |
|---|---:|---:|---|---|
| 原方案 | 1/10 | 2/2 | `8850854` | `baseline-safety` |
| 整批预检 | 2/10 | 2/2 | `8eb06e7` | `preflight-safety` |
| 持久围栏 | 9/10 | 2/2 | `1c0b56d` | `fence-safety` |
| 两者组合 | 10/10 | 2/2 | `64a0757` | `combined-safety` |
| 综合入口验收 | 10/10 | 2/2 | `164f7bb` | `final-safety` |

预检分支为 `exp/realdb-preflight`；围栏分支为 `exp/realdb-durable-fence`，评测使用冻结 worktree，避免后续调用协议修改干扰；组合分支为 `exp/realdb-combined`。综合入口是在组合后加入明确的调用协议并复跑。重复验收不算新增独立场景。

| 补充进程退出对照 | 通过 | 实际评测提交 | 结果目录 |
|---|---:|---|---|
| 原方案 | 0/2 | `ccf238a` | `baseline-crash` |
| 组合 | 2/2 | `ea51042` | `combined-crash` |
| 综合入口验收 | 2/2 | `164f7bb` | `final-crash` |

全部结果保留于 `lab/results/real-storage-20261023/`，各目录含 manifest、summary、逐项事实和失败检查。`comparison.json` 核对同组判定器摘要和 case ID 一致、清理完成及最终存储源码对应当前版本。原方案失败也完整保留。

## 采用的改动

1. **整批准备与原规则预检**（功能提交 `edcc3f3`）。第一次远端变更前准备所有 payload，用原 `WriteDataSerializer` 检查，不放宽空值、类型或权限。Agent 适配路径只将明确的 `TableCellValidationError` 映射为未派发/未尝试；可能已有远端效果后的 False、ValueError 或异常仍是 unknown。
2. **派发前持久表级围栏**（功能提交 `39c9408`）。复用现有 TableAuditEvent 的 `write.fence` pending 记录，无新增模型或迁移。在独立 PG durable 事务中先持久化，再进入远端写入事务；只有业务审计和整个操作成功才一起结束围栏。回滚、丢确认或进程退出留下 pending，后续受管写入/清空拒绝，受管读取不签发可信版本。围栏元数据不推进业务表版本。
3. **明确调用协议**（功能提交 `6281495`，综合对应 `445e4d3`）。无法独立持久化的外层 atomic/autocommit=false 调用在派发前拒绝。Agent write/clear 准确返回 `TABLE_WRITE_DURABILITY_REQUIRED`、不可重试、未派发/未尝试；两个 HTTP upsert 入口对该拒绝与 typed unknown 返回结构化 409，明确区分“联系管理员”与“可能已生效、勿重复提交”。HTTP 旧服务 False 仍保留旧响应，尚未统一为结构化 unknown。没有额外创建 HTTP clear 路由。

组合时仅合并上述功能，不放宽 unknown 门禁。两处隔离单元夹具先因新增事务/异常命名空间失败，保留诊断后修正夹具，并增加 HTTP 参数错误兼容检查；没有为通过分数修改共同安全判定器。

最终验收：`acceptance/final-local-checks.json`，提交 `164f7bb`，204/204 离线程序检查通过，包括原 82 项基线、既有合同和新增预检、围栏、调用协议及判定器检查。离线子进程中部分仍用 SQLite/Mock，和上方真实数据库场景分列，不当成真实模型任务。

## 接回透明数据空间前的缺口

- **旧入口尚未统一。** `upsert_table_data_full` 和 `delete_table_row` 仍直接修改远端，其 HTTP 路由仍存在，可绕过围栏与行锁。当前只覆盖 `upsert_table_cells`、`clear_table_cells` 和 `get_table_data`。生产接回前必须将旧写路径统一到同一协议或封禁绕行入口。
- **围栏提供隔离，不提供跨库回滚。** 预检避免已知非法后续单元格导致的先写；合法批次中后续网络/业务故障仍可部分提交。pending 表停止后续操作，已发生的远端效果不会被撤回。
- **尚无权威对账和解除流程。** 围栏持久化后、远端尚未派发时崩溃也可能锁住该表。不能靠超时或猜测清除；需要操作标识、远端事务证据、授权核验和有审计的解除/修复流程。当前选择保守阻断，恢复可用性仍需人工核验。
- **事务与完整平台集成待验证。** 外层 atomic 会被拒绝，须确认真实 HTTP/Agent 执行边界；本轮仅验证抽取的实际 handler/adapter/graph 节点，未验证生产 IAM、审核、SSE、recorder、前端 409 展示及完整 runtime。PG 行锁跨远端调用持有，吞吐与超时也未作负载评测。
- **故障范围仍有限。** 未覆盖 PG commit 回执模糊、任意服务宕机、长批次、多节点负载和真实业务脱敏保留集。没有新增办公软件对比或模型端到端成绩。

因此可以将此分支用于下一轮隔离联调；在旧入口收敛、权威对账和全链路授权/故障验收完成前，不开放无人值守业务写入。

## 复现

在 `agent-lab` 工作区使用独立本地测试配置运行；配置含凭据，不提交、不在终端打印。每次 output 使用新目录，缺配置或连接失败不会回退替身。

```powershell
../.venv/Scripts/python.exe lab/run_realdb_safety.py --config ../.local-db/realdb.json --output lab/results/<新安全目录>
../.venv/Scripts/python.exe lab/run_realdb_crash.py --config ../.local-db/realdb.json --output lab/results/<新崩溃目录>
```

原 `run_realdb_integration.py` 六诊断用于保留原风险观察；修正后的验收使用上述安全判定器，不应将旧风险断言直接视为新方案的通过标准。
