# 旧写入口收拢与跨入口版本保护

日期：2026-10-23。接续[真实双库报告](2026-10-23-real-storage.md)，用户要求继续。综合分支 `solution/unified-writes-20261023`，最终功能验收提交 `7c74a8d`；此后仅补文档。原后端冲突工作区、业务库及安全策略未改，没有推送部署或真实模型调用。

## 结果

此前两条绕过围栏的活入口已纳入协议：raw 完整单元格属性写入和删行。同时修复了审阅中新发现的跨入口单元格版本缺口：raw/clear/delete 成功后，旧 cell expected_version 现在会失效。最终231项离线检查通过，真实PG/immudb结果如下。

| 固定开发合同 | 修改前 | 最终 | 最终结果目录 |
|---|---:|---:|---|
| 旧入口正常/格式、pending、丢ACK、旧表版本、outer atomic | 2/13 | 13/13 | `final-v2-legacy` |
| raw/delete 两种顺序实际PG行锁竞争 | 0/2 | 2/2 | `final-v2-legacy-locks` |
| raw/clear/delete 后仅带旧cell版本写入 | 0/3 | 3/3 | `final-v2-cell-versions` |
| 既有受管安全场景回归 | 前轮已10/10 | 10/10 | `final-v2-safety` |
| 既有提交确认后进程退出回归 | 前轮已2/2 | 2/2 | `final-v2-crash` |

原有cells/clear两种顺序PG锁场景在各真实运行中均2/2。重复运行不计作新的独立场景，上表也不合并为随机可靠性或生产事故率。231项程序检查不等于231个真实办公任务。

## 采用的实现

- `upsert_table_data_full` 名称虽含 full，实际接收单个单元格完整属性 dict；继续使用原 `WriteDataSerializer` 和原SDK，保留ctfa/ctt/cts、格式、样式和原坐标主键规则，不转成普通value。预检新增负坐标、越界列与非法版本拒绝；数值字符串坐标按原序列化规则接受，审计使用规范化整数。
- raw/delete 均保留服务层权限检查，新增可选 `expected_table_version`。在独立 durable PG事务中锁表、检查pending/版本并持久化围栏；第二事务同锁复核，远端操作成功后将业务审计和围栏一起完成。失败、异常及事务提交失败保留pending，派发后的False不当成未执行。
- 两条活HTTP路由为 `/api/opengate/tables/upsert`、`/api/opengate/tables/data/rows/delete`。它们传递版本参数，保持raw正常成功响应及delete的deleted_cells；typed预检返回400/未派发，冲突与事务拒绝返回409，unknown返回409并明确可能已生效、勿重复提交。没有类型的服务False也保守归unknown；原有cells接口预检响应保持不变。
- `_cell_versions` 原来只汇总成功cell.upsert。本轮纳入成功data.upsert、cell.clear；成功row.delete按行聚合最大审计ID，使该行所有请求列的旧版本失效，包括尚无单元格记录的列。取坐标事件与删行记录的最大值，pending/failed/fence不推进版本，不影响其他行。表级CAS保持原逻辑。

## 分支、冻结与对照过程

| 角色 | 独立分支 / 冻结提交 |
|---|---|
| 两旧服务围栏 | `exp/unified-legacy-writes` / 功能`16d8c9d`，共同基础设施加入后真实评测`33b2b69` |
| HTTP兼容 | `exp/legacy-write-callers` / `de32df9` |
| 13项共同判定器 | `study/legacy-write-evaluator` / loader `da27e72`、判定器 `2d8892c` |
| 跨入口cell版本 | `exp/cell-version-mutations` / `53a45ad` |
| 根工作区旧实现对照 | `21f19c1`，无本轮生产修正 |
| 首次整合 | `83c25da`，226项离线与13项合同通过；随后审阅发现cell版本缺口 |
| cell反例冻结 | `b2d9bff`，真实补充0/3 |
| 最终整合 | `7c74a8d`，231项离线及上表全部真实验收 |

修改前先留存82项原图基线，按[协议](2026-10-23-unified-writes-protocol.md)固定13项。实际legacy并发两项在首次真实运行前补充；cell版本三项是在首次整合结果之后因独立审阅发现，另行登记、冻结并先复现失败，不能冒充原始预注册。原13项、安全10项及崩溃2项判定正文均未改。

13项中，raw/delete两种丢ACK分别对raw/cells/clear/delete四种后续操作使用独立故障表，避免前一次重试造成后一次陈旧而假通过；加上已有cells/clear到旧入口的四项，共每组12次实际SDK确认提交后丢回执。后续服务实例必须抛真实typed unknown、零新增变更服务调用、保留已提交状态，pending读不签发可信表版本，另一表仍可正常写入。这里是同进程新服务globals，不冒充legacy专属进程崩溃测试。

两项新并发实际观察pg_stat_activity的Lock等待；释放先操作方后，等待方须以TABLE_WRITE_CONFLICT拒绝，远端变更入口总数仅seed+胜者。三个cell补充场景刻意不提供表版本，只带旧单元格版本，要求CELL_WRITE_CONFLICT、零新增调用、值保持；当前单元格版本还须可正常写入。多列删行、未写列、坐标隔离和失败审计行为另由真实SQLite ORM离线检查覆盖。

三项cell诊断的old/current令牌直接取源提取的 `_cell_versions` helper，证明版本计算与CAS门禁；未通过真实HTTP/客户端读取回执获取令牌，不能据此宣称删除后空坐标的客户端重建流程已完成。

所有结果保留于 `lab/results/unified-writes-20261023/`，包含原失败、首次整合、补充反例和最终`final-v2-*`；最终离线报告为 `acceptance-v2/final-local-checks.json`。`comparison.json`核对同组判定器原始SHA/case IDs一致、最终生产源摘要与当前文件对应、无SQLite unknown guard、模型调用0及资源清理完成。

身份例外如实保留：首轮`candidate-legacy-locks`仅因Git CRLF换行转换导致原始SHA不同；同代码同字节的第一次复跑`candidate-legacy-locks-exact`因索引换行状态记为dirty。核对无语义差异、刷新同blob索引且无暂存差异后，`candidate-legacy-locks-exact-clean`再次2/2，clean且同原始SHA，作为正式候选对照。前两跑未覆盖或删除。所有正式表内对照和最终验收均clean。

## 环境与指标口径

本地专用PG18.6、immudb1.11.2重新启动并认证，仍为127.0.0.1:55432/53322。每次真实运行独立schema、test_UUID数据库和UUID表，最终全部清理。IAM/审核仍为允许夹具，运行源提取服务与实际SDK，不连接生产properties/业务库。没有收费模型调用、数据或凭据提交。

连接口径精确化：独立readiness探针通过TLS verify-full；当前诊断runner的PG配置实际使用sslmode=require。此前环境说明中的verify-full指独立连接准备验证，不表示所有诊断都做了主机名核验。

计数是变更服务入口次数，包含原SDK封装内的本地序列化拒绝，不等于SQL RPC次数。尤其旧版raw非法颜色payload已由原SDK序列化器拒绝；该项失败证明预检/明确未派发合同缺失，不能称为实际数据部分写入。远端值、SQL格式字段与实际提交丢ACK另有事实核对。

## 尚未完成的接回条件

- 四种数据变更服务现在共享pending隔离和版本审计，但项目/表创建、项目删除delete_db、PG结构/权限更新、直接SDK和其他存证域不在这轮围栏内。代码中前一个同名app_upsert_table_data定义被后定义覆盖；实际App路由走受管cells，并非另一条活raw路由，本轮未改死定义。
- HTTP表版本仍可省略；未提供任何expected版本时仍是兼容的无条件编辑，不能宣称所有HTTP调用都强制读后CAS。现有Agent路径仍使用可信读取版本。历史缺坐标的审计没有回填，也未证明迁移前所有旧客户端版本都能自动失效。
- 围栏不提供跨库原子回滚、exactly-once或自动恢复。缺行等不明确False也会保守留下pending，甚至尚未派发的崩溃也可能阻断表；需要权威操作凭证、对账与有审计的解除，不能超时清除或猜测重发。新增raw/delete的任意进程/数据库故障窗口尚未穷尽。
- HTTP映射、权限拒绝和serializer为离线原handler检查；完整生产IAM、审核、风险控制、runtime/SSE/recorder、前端409展示、吞吐与真实模型端到端尚未联调。

本轮完成了旧数据入口覆盖与一个实际跨入口陈旧写入缺口；下一阶段优先权威对账和恢复，再进行完整平台隔离联调，仍不开放无人值守生产写入。

## 复现

使用已有专用测试配置，output必须是新目录，配置不打印、不提交；连接失败不会回退替身。

```powershell
../.venv/Scripts/python.exe lab/run_realdb_legacy.py --config ../.local-db/realdb.json --output lab/results/<新旧入口目录>
../.venv/Scripts/python.exe lab/run_realdb_legacy_locks.py --config ../.local-db/realdb.json --output lab/results/<新并发目录>
../.venv/Scripts/python.exe lab/run_realdb_cell_versions.py --config ../.local-db/realdb.json --output lab/results/<新版本目录>
```
