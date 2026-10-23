# 旧写入口围栏覆盖协议

日期：2026-10-23。起点 `3dc5e43`（综合功能 `164f7bb`）；用户要求继续。修改前82项离线基线通过，报告保留于 `lab/results/unified-writes-20261023/baseline-local.json`。本轮仅修改 agent-lab 快照，不改原后端冲突工作区、业务数据、系统安全策略，不推送部署或调用收费模型。

目标是将 `upsert_table_data_full`（实际为单个单元格完整属性 payload）及 `delete_table_row` 纳入现有持久围栏/版本协议。保留 raw payload 与原序列化/SDK语义，不转成普通 value 丢失样式。权限、本地参数拒绝必须在远端派发前；无法独立持久化时拒绝；派发后失败仍按未知处理，不自动重试、清围栏或补偿。两个服务增加可选版本参数，HTTP 传递并明确映射预检、冲突、事务拒绝及 typed unknown。既有受管 upsert/clear 判定器不改。

实现、共同判定器、HTTP 调用兼容各在独立 worktree/分支：`exp/unified-legacy-writes`、`study/legacy-write-evaluator`、`exp/legacy-write-callers`。共同判定器在候选真实运行之前冻结，同一文件用于旧版与候选；失败完整保留，不修改 gold 或隐藏未执行项。调用链审计区分 Python 同名函数覆盖后的可达入口与死定义，不将两条旧路径覆盖称为整个系统生命周期已安全。

固定13个真实PG+immudb开发诊断：

1. `raw_normal_metadata`：完整属性正常写入、真实格式字段保留、业务版本推进。
2. `delete_normal_version`：正常删行、实际远端状态及业务版本推进。
3. `pending_write_blocks_raw`。
4. `pending_write_blocks_delete`。
5. `pending_clear_blocks_raw`。
6. `pending_clear_blocks_delete`。
7. `raw_ack_lost_blocks_all_mutations`。
8. `delete_ack_lost_blocks_all_mutations`。
9. `raw_invalid_no_dispatch`。
10. `raw_stale_version_no_dispatch`。
11. `delete_stale_version_no_dispatch`。
12. `raw_outer_atomic_no_dispatch`。
13. `delete_outer_atomic_no_dispatch`。

丢确认在实际SDK返回已提交事务后注入；第7/8项对 raw/cells/clear/delete 四种后续操作分别使用独立故障表。阻断必须有可信类型或明确 False，并同时满足零新增远端变更调用、值不变；任意异常/TypeError不算安全通过。原方法缺少版本形参时按原签名调用并记录，不用不兼容 TypeError 冒充拒绝。pending 读取不签发可信版本，其他表应保持可写。每项分别记录事实/检查/失败，固定13项与子项、另列两项PG锁不混计。

复用真实PG schema/test_UUID immudb隔离库及清理入口，无SQLite unknown guard、无数据库替身回退；IAM/审核仍为允许夹具，未验证完整生产平台。最终候选还须通过既有10项安全场景、2项进程退出场景和相关离线回归。旧库不在运行时仅启动已有专用配置，不打印凭据；不可用则保留unavailable，先完成独立离线工作。

权威对账/解除、完整平台联调和模型端到端复测为后续阶段，本轮不以局部安全场景代替这些验收。
