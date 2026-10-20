# Phase 4-A 灰度路由、指标和回滚步骤

## 范围

本阶段覆盖在线 Agent 请求从 OpenClaw legacy 路径切换到统一运行时的灰度能力。灰度期曾通过 `CompanyAgentRuntimePolicy` 选择 `legacy`、`canary` 或 `unified`；Phase 4-C 最终切换后，在线 Agent 请求只允许 unified，legacy/canary 未命中的请求会被后端阻断，不再调用 OpenClaw Adapter。控制面、归档、历史查询和基础设施退役能力按 Phase 4-B/C 文档保留。

## 路由策略

- `legacy`：灰度期用于小流量回滚；Phase 4-C 最终切换后，在线请求不再执行该分支，而是返回需要 unified 策略的错误。
- `canary`：灰度期使用 `company_id + session_id` 稳定 Hash，命中 `canary_percentage` 的会话走 unified；Phase 4-C 最终切换后，未命中 unified 的请求同样被阻断，不再回落到 OpenClaw。
- `unified`：在线请求只走统一运行时，不查询 OpenClaw Adapter。

Hash 结果对同一公司和同一会话稳定，因此同一会话不会在灰度比例不变时来回切换引擎。

## 灰度节奏

1. 内部灰度：目标公司设置 `mode=canary, canary_percentage=100`，仅内部公司进入 unified。
2. 10% 灰度：目标生产公司设置 `mode=canary, canary_percentage=10`，观察至少 1 个工作日。
3. 30% 灰度：目标生产公司设置 `mode=canary, canary_percentage=30`，观察至少 2 个工作日。
4. 100% 灰度：设置 `mode=canary, canary_percentage=100`，继续观察至累计至少 5 个工作日。
5. 全量切换：稳定后设置 `mode=unified`。此状态下在线请求不再调用 OpenClaw Adapter。

## 关键指标

通过 Agent Runtime metrics API 观察：

- 首 Token 延迟：`firstTokenMs.avg/p50/p95`
- 总耗时：`totalDurationMs.avg/p50/p95`
- 模型错误率：`modelErrorRate`
- 工具错误率：`toolErrorRate`
- 引擎分布：`routeCounts.openclaw/unified`

建议每次比例提升前比较最近 24 小时与前一阶段窗口，若模型错误率、工具错误率或 p95 延迟显著恶化，应暂停提升。

## 回滚步骤

配置级回滚：

```sql
UPDATE agent_runtime_company_runtime_policy
SET mode = 'legacy', canary_percentage = 0, updated_at = NOW()
WHERE company_id = <company_id>;
```

小流量回退：

```sql
UPDATE agent_runtime_company_runtime_policy
SET mode = 'canary', canary_percentage = 10, updated_at = NOW()
WHERE company_id = <company_id>;
```

代码级回滚：Phase 4-C 最终切换版本已删除在线请求中的 OpenClaw Adapter 调用。若 unified 出现系统性故障，只能回滚到上一发布版本，并按该版本的回滚说明将策略切回 `legacy` 或低比例 `canary`；当前版本不得通过在线请求重新触达 OpenClaw Adapter。

## 去 OpenClaw 验收点

- `mode=unified` 时，agent test 和 public client 在线请求不得调用 `OpenClawRuntimeService.get_company_instance` 或 provider adapter。
- `mode=canary, canary_percentage=100` 时，在线请求同样不得调用 OpenClaw Adapter。
- `mode=legacy`、缺失策略或 `mode=canary` 未命中 unified 时，在线请求必须返回明确错误，不得调用 OpenClaw Adapter。
- OpenClaw 控制面 API、归档、历史查询和退役 runbook 保留到 Phase 4-B/C 路径处理。
