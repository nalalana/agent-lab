# 本地数据库准备、真实锁验证与写前拒绝修正

后续更新：2026-10-23用户手动启动immudb后，已完成真实双库对照与综合修正，见[后续报告](2026-10-23-real-storage.md)。下文保留10月22日当时的阻塞状态和验证范围。

日期：2026-10-22。起点d970683；当前分支solution/local-storage-20261022，功能提交582827e。按[本轮协议](2026-10-22-local-databases-protocol.md)运行。用户已授权下载、启动独立本地数据库并继续测试。未修改原后端工作区、推送、部署、连接云数据库或调用收费模型。

## 本轮结果

PostgreSQL与psql已安装并实际运行，两个顺序的真实行锁竞争均通过；合并后的默认入口复跑同样通过。immudb官方文件已下载并校验，但Windows应用程序控制策略阻止执行，尚未启动，真实双库测试不可用。未关闭、解除或绕过系统安全策略。

另修正8处明确发生在派发前的cells参数拒绝：原来被保守提升为未知变更，现在通过类型化异常准确声明not_dispatched/not_attempted。服务内部发生同类异常仍保留unknown硬停。该修复不涉及提示、核验阈值或数据库原子性。

| 验证层次 | 结果 | 范围 |
|---|---|---|
| 改动前原执行图基线 | 82/82通过 | 离线脚本化模型与程序合同 |
| 原版本真实PG锁 | 2/2通过 | PG真实、远端SQLite替身、IAM/审核允许夹具 |
| 合并后真实PG锁 | 同2场景再次2/2通过 | 验证入口扩展保持原行为，不当作4种独立场景 |
| 写前拒绝候选验收 | 101/101通过 | 82基线+19项相关检查，含新4项 |
| 合并后完整离线检查 | 179/179通过 | 含原图、unknown、存储、版本、恢复和新入口合同 |
| 真实PG+immudb | unavailable，0项通过 | immudb未启动，未回退替身 |

本轮没有新增真实模型能力成绩。179项程序检查不等于179个真实办公任务；PG锁通过也不等于跨库写入安全已经验证。

## 下载与本地运行

使用E:/program/touming/.local-db中的独立portable目录，未注册Windows服务或修改全局PATH。下载包、数据、证书私钥、凭据和日志均在Git外；连接配置与密码文件限制为当前用户访问，密码未输出。

| 组件 | 固定版本 | 当前状态 |
|---|---|---|
| PostgreSQL server、psql、initdb、pg_ctl | EDB PostgreSQL 18.6-5，程序报告18.6 | 已运行，仅127.0.0.1:55432 |
| immudb、immuclient、immuadmin | 官方v1.11.2 Windows amd64 | 文件校验完成；服务端执行被应用控制阻止 |
| Python客户端 | psycopg2-binary 2.9.12、immudb-py 1.4.1 | 使用现有环境，没有另引入数据库框架 |

PostgreSQL来源由[PostgreSQL官方Windows页](https://www.postgresql.org/download/windows/)指向[EDB binaries](https://www.enterprisedb.com/download-postgresql-binaries)，固定[18.6-5 ZIP](https://get.enterprisedb.com/postgresql/postgresql-18.6-5-windows-x64-binaries.zip)并核对官方SHA-256 sidecar与[EDB维护者确认](https://github.com/EnterpriseDB/edb-installers/issues/746)。本机归档校验值为e2246ba91d22345bc3d017586c09ede52d9df180b1eeb480f050445f1cad84e2。程序文件未带Authenticode签名，但Windows允许其正常运行；没有通过修改安全策略启动它们。

immudb来源为[官方v1.11.2发布](https://github.com/codenotary/immudb/releases/tag/v1.11.2)与同版本GitHub Release API资产digest，三份Windows文件均逐一核对SHA-256。服务端文件位于E:/program/touming/.local-db/immudb/immudb-v1.11.2-windows-amd64.exe，SHA-256为dd41048e1f62b7fd65a01acb10be8213e7d16fcf79a59a0d59ea51c00ae1c41c。执行--help即被系统拒绝；已向用户请求由管理员按现有策略批准该文件，等待期间不再尝试执行或绕过。

PG专用role/database均为agent_lab_test，role非超级用户。实际psql及libpq verify-full连接通过：证书/主机名验证、TLSv1.3、TLS_AES_256_GCM_SHA384；非TLS连接和错误主机名均被拒绝。锁runner沿用sslmode=require，确认加密，不将该runner单独声称为verify-full测试。

本地配置路径（含凭据，不提交或输出内容）：

- E:/program/touming/.local-db/postgresql/agent-lab-pg.json：PG入口配置。
- E:/program/touming/.local-db/realdb.json：双库入口配置。
- E:/program/touming/.local-db/immudb/local-test.toml：已准备的loopback配置；53322，认证开启，web/metrics/PG协议端口关闭，尚未运行。

安装及握手证据保留于.local-db/postgresql/setup-report.json、distribution.json、authenticode.json和.local-db/immudb-downloads.json。安装版本与本机测试环境不是对生产版本兼容性的承诺。

## 真实PostgreSQL锁验证

冻结对照使用agent-lab-followup-baseline的edef74e，合并后版本使用582827e。两次均创建独立UUID schema，测试结束清理成功；只连接本机专用库。

两个场景分别由写入或清空先获取GatesTable行锁，在远端替身调用处暂停；另一操作携带相同旧版本进入。通过pg_stat_activity实际观察等待锁，释放先行操作后，竞争方返回TABLE_WRITE_CONFLICT且不触达远端，最终值保留先行操作的结果。

这补上了此前未运行的真实PG锁证据。OpenGate相关方法由源文件原样抽取；IAM和内容审核仍替换为允许，远端存储仍为SQLite，不能据此宣称完整平台或immudb已验证。

结果在lab/results/local-databases-20261022/postgres-baseline及postgres-integrated，分别保留manifest、trials和summary。

## 写前拒绝的最小修复

独立分支exp/prewrite-rejection-contract，冻结6a9a73a，合并提交658b6ad。仅修改tools/project_tables.py的write/clear各4处显式本地cells检查；它们全部位于存储服务调用之前。

| 情况 | 修改前 | 修改后 |
|---|---|---|
| 非法cells数组、缺失字段、越界、非法值/重复坐标等8个分支 | ValueError经广域catch被提升为OPERATION_OUTCOME_UNKNOWN | TOOL_INVALID_ARGUMENTS + not_dispatched/not_attempted；存储调用0次 |
| 服务可能产生效果后抛ValueError | 保守unknown | 仍为unknown，原错误码保留、停止排队写入与模型续写 |

未改workspace广域catch、unknown门禁、参数合法性规则或按错误码整类豁免。原有一个存储测试随明确异常合同更新预期；新增4项检查复用实际源码、生产执行节点与原图。改前复现保留在独立worktree忽略目录lab/results/prewrite-rejection/reproduced-before.json，8个局部分支和1个全图预期失败均记录，未抹去旧结果。

这些检查使用替代授权/服务和脚本化模型，不能宣称真实数据库或模型完成率改善。修复效果限于错误分类准确与避免不必要的未知写入硬停。

## 真实双库入口与尚未关闭的问题

独立分支exp/realdb-integration，冻结765b05e，合并提交582827e。新增run_realdb_integration.py，复用run_postgres_locks流程、原OpenGate/ImmudbService方法、生产toumingClient和现有SDK。额外工厂参数默认不改变旧PG入口，生成SQL安全UUID表及test_UUID数据库，声明IAM/审核和lab-only SQLite unknown门闩。

冻结入口计划六项：两种顺序的真实双库锁竞争、正常写入与清空、真实UPSERT提交后丢确认、真实UPDATE提交后丢确认、第二单元格空字符串被原序列化器拒绝后出现部分提交。故障注入在真实SDK返回后丢弃确认，不用假成功替代远端提交；逐项对照远端内容、PG审计/版本与调用次数。

本次realdb-attempt-01因本地immudb端点不可用，在登录阶段返回unavailable和_InactiveRpcError，real_immudb=false、0项通过，未进入PG schema或替身回退。离线新增4项只证明配置限制、错误不泄露、无静默回退及源方法故障合同，不证明六项真实场景已执行。

源码与离线原方法检查仍提示下列风险，须待真实immudb继续复核：远端提交后PG回滚可能丢掉成功审计/版本；多单元格逐条提交可部分成功；未提交PG事务期间的远端中间状态读取、旧接口绕开表锁、跨run持久操作归因仍未覆盖。实验SQLite guard不是生产持久对账方案。本轮没有为跨库原子性、存储级幂等或无人值守写入作已完成结论。

## 后续运行

PostgreSQL保持在上述loopback端口运行，以便后续测试。可通过专用pg_ctl与data目录启停，无系统服务。immudb需先由管理员按策略允许已校验文件，再使用.local-db/immudb/start-local.ps1启动；运行后先验证进程和实际监听地址，不能仅凭Start-Process返回认定就绪。

从仓库根目录使用新输出目录：

```powershell
../.venv/Scripts/python.exe lab/run_postgres_locks.py --config E:/program/touming/.local-db/postgresql/agent-lab-pg.json --output lab/results/<新PG目录>
../.venv/Scripts/python.exe lab/run_realdb_integration.py --config E:/program/touming/.local-db/realdb.json --output lab/results/<新双库目录>
```

本轮179项离线汇总位于lab/results/local-databases-20261022/acceptance/final-local-checks.json，执行提交582827e，之后仅补报告。全部结果保持本地忽略。immudb策略阻塞解除后应继续六项真实测试，再依据实际失败决定原子操作记录、对账与恢复改动；本轮不把准备完成等同于双库验证完成。
