# 显式可恢复写入与权威提交对账协议

日期：2026-10-23。起点8fe6272，用户要求继续。修改前82项原图基线通过，保留于lab/results/reconciliation-20261023/baseline-local.json。仅改agent-lab实验快照，不动原后端冲突工作区、生产业务库或安全策略，不推送部署，不调用收费模型。

开发能力探针先确认本机immudb1.11.2：同一显式BEGIN/COMMIT内业务UPSERT与唯一INSERT receipt同时提交；重复receipt主键导致前序业务写入回滚；实际提交后丢ACK，业务与receipt仍同时可读。三项能力及原PG两锁均通过，资源清理完成，结果capability-01保留。它不是生产恢复验收，也不证明receipt缺失等于未提交。

本轮选择新增显式opt-in服务路径，沿用原GatesTable锁、权限、版本和TableAuditEvent围栏，不替换旧四入口、不接管默认Agent工具、不复制Agent循环。新提交服务生成operation ID，在PG派发前保存版本化scope、规范请求摘要与可重建业务审计计划；一个逻辑操作的全部业务DML和唯一receipt INSERT同属一项显式immudb事务。对账只接受绑定该操作、scope、action、摘要和完整计划的可信提交凭证，补齐原业务审计/版本并原子结束该围栏；不得重发业务、按超时解除、靠当前值相同认领或顺带清其他pending。旧无协议凭证的围栏、缺失/部分/篡改/不可读凭证继续unknown。receipt内自报tx_id不能替代远端可信提交归属。

实现按三分支隔离：exp/recoverable-table-writes负责PG服务协议与恢复；exp/atomic-operation-receipts负责实际SDK原子写与凭证读取；study/recovery-evaluator负责独立真实故障判定器。接口先约定，判定器冻结后真实运行；缺少新方法的旧版必须明确unsupported/capability=false，不伪称0分安全失败或已支持恢复。旧安全/崩溃/旧入口判定器与结果保留，不降低断言来适配BEGIN包装。

固定12个主场景家庭、24个叶子，分别记录子项与全部失败，不将叶子/重复运行混称独立真实业务任务：

1. normal_atomic_commit[raw,cells,clear,delete]。
2. commit_ack_lost_reconcile[raw,cells,clear,delete]。
3. commit_process_exit_reconcile[cells,clear]。
4. reconcile_idempotency[new_run_replay,concurrent_two_callers]。
5. pg_repair_rollback。
6. unattributed_evidence[absent_receipt,legacy_fence,matching_current_values]。
7. partial_receipt_rejected。
8. tampered_receipt_rejected[request_digest_mismatch,operation_id_mismatch,scope_mismatch]。
9. inflight_reconcile。
10. scope_rejected_without_rpc。
11. auth_rejected_without_rpc。
12. atomic_batch_rollback。

故障注入专门识别带operation receipt的显式逻辑事务，必须先确认真实SDK提交回执再丢ACK或进程退出；事务中错误必须检查业务和receipt同时回滚。恢复期间业务变更次数为0；无授权/错误scope需全部RPC为0，不能仅以旧RealRemote.calls衡量。修复PG事务失败时审计/版本/fence一起回滚，可再次对账但不能再次写业务。已完成后的重复对账仍先重新授权与scope绑定，审计ID/版本应稳定。在途与恢复竞争需观察真实PG锁，不允许提前猜测无凭证而解除。

真实运行使用独立PG schema/test_UUID immudb库及UUID表；IAM/审核可为显式允许夹具，不能据此声称完整生产授权/HTTP/runtime验证。程序回归、真实存储合同、SDK能力探针及原有模型成绩分列。凭据、业务数据、原始轨迹和本地配置不提交。字段/认证/SDK差异必须记载，额外场景公开作为后补，失败不可覆盖。
