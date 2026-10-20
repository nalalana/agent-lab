# Phase 0-B 数据基线与统一运行时 Schema 设计

> 任务编号：P0-B
> 基线分支：`feature/phase0-B-data_baseline`
> 本阶段只冻结 Schema、建立数据盘点/验收/备份工具，不创建统一运行时业务表。

## 1. 本阶段交付物

- 数据盘点命令：`agent_runtime_data_baseline`。
- 固定公司 A/B 验收数据：`agent_runtime_seed_acceptance_data`。
- OpenViking Namespace 和脱敏记忆样例导出。
- PostgreSQL备份、恢复验证和数量校验脚本。
- 统一运行时 8 张新表的 Django Schema 与 Migration 设计。
- Phase 0-B自动化测试。

## 2. 当前 Migration 审查结果

以 `develop@e24e9a2` 为基线：

| App | 当前迁移链 | 结论 |
|---|---|---|
| `company` | `0001` → `0020_agent_viking_memory_space` | 单一线性叶节点 |
| `runtime` | `0001` → `0013_workspacefile_openviking_resource` | 单一线性叶节点 |
| `agent_runtime` | 暂无 Migration | Phase 0只有接口契约和测试 |

上线或合并前必须运行：

```bash
cd backend
python manage.py makemigrations --check --dry-run
python manage.py showmigrations company runtime agent_runtime
python manage.py agent_runtime_data_baseline --fail-on-pending-migrations
```

`agent_runtime_data_baseline` 同时输出：

- 磁盘上的 Migration。
- 数据库已应用 Migration。
- 叶节点。
- 待应用 Migration。
- Migration冲突。

如果 `pending` 非空或 `conflicts` 非空，不允许进入阶段 1。

## 3. 数据盘点范围

| 基线键 | Django Model | 数据表 |
|---|---|---|
| `companies` | `company.Company` | `company_company` |
| `transparentCompanies` | `company.TransparentCompany` | `company_transparentcompany` |
| `agents` | `company.TransparentCompanyAgentDefinition` | `company_transparentcompanyagentdefinition` |
| `agentPromptVersions` | `company.TransparentCompanyAgentPromptVersion` | `company_transparentcompanyagentpromptversion` |
| `skills` | `company.TransparentCompanySkill` | `company_transparentcompanyskill` |
| `sessions` | `company.TransparentCompanyAgentTestSession` | `company_transparentcompanyagenttestsession` |
| `messages` | `company.TransparentCompanyAgentTestMessage` | `company_transparentcompanyagenttestmessage` |
| `workspaceFiles` | `runtime.WorkspaceFile` | `runtime_workspacefile` |
| `vikingSpaces` | `company.TransparentCompanyAgentVikingSpace` | `company_transparentcompanyagentvikingspace` |

默认报告不包含：

- 模型、COS、OpenViking或AGSX凭证。
- 消息正文。
- 原始记忆内容。
- OpenViking `lastError`。

记忆样例默认只输出内容长度和 SHA-256。只有显式指定公司并加上
`--include-memory-content` 时才输出原文：

```bash
python manage.py agent_runtime_data_baseline \
  --memory-company-cid phase0-company-alpha \
  --include-memory-content
```

禁止对生产公司批量使用该选项。

## 4. 固定公司 A/B 验收数据

Fixture：

```text
backend/apps/agent_runtime/fixtures/data_baseline_acceptance.json
```

数据边界：

```text
公司 A：phase0-company-alpha
  Alpha A1：公开，允许 Python，允许记忆
  Alpha A2：内部，禁止 Python，禁止记忆
  唯一记忆：ALPHA-ONLY-731

公司 B：phase0-company-beta
  Beta B1：公开，允许 Python，允许记忆
  Beta B2：内部，禁止 Python，禁止记忆
  唯一记忆：BETA-ONLY-942
```

Seeder 特性：

- 使用 `update_or_create`，可重复执行。
- 生产环境永久拒绝执行。
- 非测试数据库必须显式使用 `--allow-non-test-database`。
- OpenViking `accountId/userId/rootUri` 使用与现有 Migration 0020 一致的确定性算法。
- 不直接向真实 OpenViking写入内容，只建立数据库 Namespace和合成记忆样例。

测试数据库执行：

```bash
cd backend
python manage.py agent_runtime_seed_acceptance_data
```

隔离的开发数据库执行：

```bash
python manage.py agent_runtime_seed_acceptance_data --allow-non-test-database
```

不得在共享生产数据库执行。

## 5. 统一运行时 Schema 总则

- 新运行时模型放在 `apps.agent_runtime`。
- 公司 Prompt 版本模型放在 `apps.company`，与现有 Agent Prompt版本保持同一领域。
- 新表统一使用 `DateTimeField`，不再新增时间字符串字段。
- Run、Snapshot、Tool Call、Sandbox、Memory Job使用 UUID主键。
- 所有公司级查询必须以 `company_id` 过滤。
- JSONField只保存脱敏、可序列化数据。
- 凭证明文不能进入任何新表。
- 事件、工具和沙箱通过 `AgentRun` 级联删除；消息、公司、Agent使用保护或明确的业务删除策略。
- OpenViking仍为语义数据层，PostgreSQL只保存 Namespace映射、同步任务和审计状态。

## 6. CompanyAgentRuntimePolicy

建议模型：`apps.agent_runtime.CompanyAgentRuntimePolicy`。

| 字段 | Django类型 | 约束/默认值 |
|---|---|---|
| `id` | `BigAutoField` | 主键 |
| `company` | `OneToOneField(Company)` | `CASCADE` |
| `mode` | `CharField(16)` | `legacy/canary/unified`，默认 `legacy` |
| `canary_percentage` | `PositiveSmallIntegerField` | 默认 0，Check 0–100 |
| `sandbox_template` | `CharField(128)` | 默认空字符串 |
| `network_policy` | `CharField(16)` | 默认 `isolated` |
| `max_concurrent_runs` | `PositiveIntegerField` | 默认 5 |
| `max_sandbox_seconds` | `PositiveIntegerField` | 默认 300 |
| `daily_token_budget` | `PositiveBigIntegerField(null=True)` | 可空 |
| `daily_sandbox_seconds_budget` | `PositiveBigIntegerField(null=True)` | 可空 |
| `config_version` | `PositiveIntegerField` | 默认 1 |
| `created_at` | `DateTimeField(auto_now_add=True)` | UTC |
| `updated_at` | `DateTimeField(auto_now=True)` | UTC |

约束：

- `canary_percentage BETWEEN 0 AND 100`。
- `mode=legacy` 时默认不调用新运行时。
- Canary必须使用 `company_id + session_id` 稳定 Hash。

## 7. AgentConfigSnapshot

建议模型：`apps.agent_runtime.AgentConfigSnapshot`。

| 字段 | Django类型 | 约束/说明 |
|---|---|---|
| `id` | `UUIDField` | 主键，`uuid4` |
| `company` | `ForeignKey(Company)` | `PROTECT` |
| `agent` | `ForeignKey(AgentDefinition)` | `PROTECT` |
| `company_prompt_version` | `ForeignKey(CompanyPromptVersion, null=True)` | `SET_NULL` |
| `agent_prompt_version` | `ForeignKey(AgentPromptVersion, null=True)` | `SET_NULL` |
| `skill_versions` | `JSONField` | 默认 `{}` |
| `model_config` | `JSONField` | 不含密钥 |
| `tool_policy` | `JSONField` | 工具清单和授权快照 |
| `memory_policy` | `JSONField` | 记忆范围和限制 |
| `compiled_prompt_encrypted` | `TextField(blank=True)` | 加密或留空 |
| `checksum` | `CharField(64)` | SHA-256，索引 |
| `created_at` | `DateTimeField(auto_now_add=True)` | UTC |

Snapshot不可更新，只能新增。召回的原始记忆不能写入 Snapshot。

## 8. AgentRun

建议模型：`apps.agent_runtime.AgentRun`。

| 字段 | Django类型 | 约束/说明 |
|---|---|---|
| `id` | `UUIDField` | 主键 |
| `request_id` | `CharField(128)` | 公司内幂等 |
| `company` | `ForeignKey(Company)` | `PROTECT` |
| `agent` | `ForeignKey(AgentDefinition)` | `PROTECT` |
| `user` | `ForeignKey(User, null=True)` | `SET_NULL` |
| `session` | `ForeignKey(AgentTestSession)` | `PROTECT` |
| `source` | `CharField(24)` | test/public/cron |
| `engine` | `CharField(16)` | openclaw/unified |
| `status` | `CharField(24)` | 状态机值 |
| `config_snapshot` | `ForeignKey(AgentConfigSnapshot, null=True)` | `PROTECT` |
| `input_message` | `ForeignKey(AgentTestMessage)` | `PROTECT` |
| `output_message` | `ForeignKey(AgentTestMessage, null=True)` | `SET_NULL` |
| `model_provider` | `CharField(64)` | 脱敏名称 |
| `model_name` | `CharField(128)` | 模型名称 |
| `input_tokens` | `PositiveIntegerField` | 默认 0 |
| `output_tokens` | `PositiveIntegerField` | 默认 0 |
| `sandbox_seconds` | `PositiveIntegerField` | 默认 0 |
| `cancel_requested_at` | `DateTimeField(null=True)` | UTC |
| `started_at` | `DateTimeField(null=True)` | UTC |
| `first_token_at` | `DateTimeField(null=True)` | UTC |
| `completed_at` | `DateTimeField(null=True)` | UTC |
| `error_code` | `CharField(64, blank=True)` | 安全错误码 |
| `error_message` | `TextField(blank=True)` | 脱敏错误 |
| `metadata` | `JSONField` | 默认 `{}` |
| `created_at` | `DateTimeField(auto_now_add=True)` | UTC |

约束和索引：

```text
UNIQUE(company, request_id)
INDEX(company, created_at DESC)
INDEX(company, status)
INDEX(session, created_at)
```

状态：

```text
PENDING → RUNNING → WAITING_TOOL → RUNNING → COMPLETED
                    ├→ FAILED
                    └→ CANCELLING → CANCELLED
```

## 9. AgentRunEvent

| 字段 | Django类型 | 约束/说明 |
|---|---|---|
| `id` | `BigAutoField` | 主键 |
| `run` | `ForeignKey(AgentRun)` | `CASCADE` |
| `sequence` | `PositiveIntegerField` | Run内递增 |
| `event_type` | `CharField(64)` | 冻结事件枚举 |
| `payload` | `JSONField` | 脱敏 |
| `created_at` | `DateTimeField(auto_now_add=True)` | UTC |

约束：`UNIQUE(run, sequence)`。

Token Delta不逐条长期保存，只保存状态、告警、工具和终态事件。

## 10. AgentToolCall

| 字段 | Django类型 | 约束/说明 |
|---|---|---|
| `id` | `UUIDField` | 主键 |
| `run` | `ForeignKey(AgentRun)` | `CASCADE` |
| `model_call_id` | `CharField(128)` | Run内唯一 |
| `tool_name` | `CharField(128)` | 注册工具名 |
| `executor` | `CharField(16)` | backend/sandbox |
| `status` | `CharField(24)` | pending/running/completed/failed |
| `arguments_redacted` | `JSONField` | 脱敏参数 |
| `arguments_hash` | `CharField(64)` | 幂等辅助 |
| `result_ref` | `CharField(1024, blank=True)` | COS或内部引用 |
| `duration_ms` | `PositiveIntegerField(null=True)` | 耗时 |
| `error_code` | `CharField(64, blank=True)` | 安全错误码 |
| `started_at` | `DateTimeField(null=True)` | UTC |
| `completed_at` | `DateTimeField(null=True)` | UTC |

约束：`UNIQUE(run, model_call_id)`。

## 11. SandboxExecution

| 字段 | Django类型 | 约束/说明 |
|---|---|---|
| `id` | `UUIDField` | 主键 |
| `run` | `OneToOneField(AgentRun)` | `CASCADE` |
| `company` | `ForeignKey(Company)` | `PROTECT` |
| `provider` | `CharField(32)` | `tencent_agsx` |
| `external_id` | `CharField(255)` | 不向普通客户端返回 |
| `status` | `CharField(24)` | creating/ready/running/terminated/failed |
| `template` | `CharField(128)` | 镜像模板 |
| `cpu_cores` | `PositiveSmallIntegerField` | 默认 2 |
| `memory_mib` | `PositiveIntegerField` | 默认 2048 |
| `network_policy` | `CharField(16)` | 默认 isolated |
| `expires_at` | `DateTimeField` | 强制过期 |
| `ready_at` | `DateTimeField(null=True)` | UTC |
| `terminated_at` | `DateTimeField(null=True)` | UTC |
| `billed_seconds` | `PositiveIntegerField` | 默认 0 |
| `error_code` | `CharField(64, blank=True)` | 错误码 |
| `created_at` | `DateTimeField(auto_now_add=True)` | UTC |

约束：同一 Run最多一个沙箱；`external_id` 建议唯一索引。

## 12. MemorySyncJob

| 字段 | Django类型 | 约束/说明 |
|---|---|---|
| `id` | `UUIDField` | 主键 |
| `company` | `ForeignKey(Company)` | `PROTECT` |
| `agent` | `ForeignKey(AgentDefinition, null=True)` | `SET_NULL` |
| `user` | `ForeignKey(User, null=True)` | `SET_NULL` |
| `job_type` | `CharField(32)` | knowledge_index/memory_commit/delete |
| `idempotency_key` | `CharField(255)` | 唯一 |
| `source_ref` | `CharField(1024)` | Message/COS引用 |
| `status` | `CharField(24)` | pending/running/completed/failed |
| `attempts` | `PositiveSmallIntegerField` | 默认 0 |
| `next_retry_at` | `DateTimeField(null=True)` | UTC |
| `error_code` | `CharField(64, blank=True)` | 错误码 |
| `created_at` | `DateTimeField(auto_now_add=True)` | UTC |
| `completed_at` | `DateTimeField(null=True)` | UTC |

索引：`(status, next_retry_at)`、`(company, created_at)`。

## 13. TransparentCompanyPromptVersion

该模型放在 `apps.company`。

| 字段 | Django类型 | 约束/说明 |
|---|---|---|
| `id` | `BigAutoField` | 主键 |
| `company` | `ForeignKey(TransparentCompany)` | `CASCADE` |
| `version` | `PositiveIntegerField` | 公司内递增 |
| `content` | `TextField` | Prompt内容 |
| `checksum` | `CharField(64)` | SHA-256 |
| `status` | `CharField(16)` | draft/active/archived |
| `created_by` | `ForeignKey(User, null=True)` | `SET_NULL` |
| `created_at` | `DateTimeField(auto_now_add=True)` | UTC |

约束：

```text
UNIQUE(company, version)
UNIQUE(company) WHERE status = 'active'
```

数据迁移将现有 `TransparentCompany.prompts` 转换为版本 1、状态 `active`，原字段在灰度期继续保留。

## 14. Migration 拆分方案

### company/0021

```text
Create TransparentCompanyPromptVersion
Add conditional unique constraint for active version
RunPython: backfill TransparentCompany.prompts as version 1
```

反向迁移只删除生成的版本记录，不修改原 `prompts` 字段。

### agent_runtime/0001_initial

```text
Create CompanyAgentRuntimePolicy
Create AgentConfigSnapshot
Create AgentRun
Create AgentRunEvent
Create AgentToolCall
Create SandboxExecution
Create MemorySyncJob
```

依赖：

```text
company.0021_transparent_company_prompt_version
runtime.0013_workspacefile_openviking_resource
```

### agent_runtime/0002_backfill_runtime_policy

```text
为所有现有公司创建 mode=legacy 的 CompanyAgentRuntimePolicy
```

Backfill必须使用历史模型、`iterator()` 和 `bulk_create(ignore_conflicts=True)`，避免导入当前 Model。

## 15. 数据基线使用方法

生成安全基线：

```bash
cd backend
python manage.py agent_runtime_data_baseline \
  --fail-on-pending-migrations \
  --output ../data/baselines/before-phase1.json
```

恢复后比较：

```bash
python manage.py agent_runtime_data_baseline \
  --compare ../data/baselines/before-phase1.json
```

只导出固定验收公司的脱敏记忆样例：

```bash
python manage.py agent_runtime_data_baseline \
  --memory-company-cid phase0-company-alpha \
  --memory-company-cid phase0-company-beta \
  --output ../data/baselines/phase0-acceptance.json
```

## 16. 备份与恢复验证

备份前必须停止应用写入，否则 pg_dump和计数报告可能来自不同时间点。

```bash
export AGENT_RUNTIME_BACKUP_QUIESCED=1
export POSTGRES_HOST=127.0.0.1
export POSTGRES_PORT=5432
export POSTGRES_DB=touming_dev
export POSTGRES_USER=postgres
export POSTGRES_PASSWORD='***'

./scripts/postgres/agent_runtime_backup.sh
```

脚本默认通过 `touming-backend-dev` 容器执行 Django基线命令；容器名不同时设置
`BACKEND_CONTAINER`。脚本会核对 Django实际连接的数据库与 `POSTGRES_DB`，不一致时停止备份。

恢复只允许进入专用验证数据库，默认要求名称以 `_restore_verify` 结尾：

```bash
export TARGET_POSTGRES_DB=touming_phase0_restore_verify
export RESTORE_CONFIRM_DATABASE=touming_phase0_restore_verify
export DJANGO_ENVIRONMENT=testing

./scripts/postgres/agent_runtime_restore_verify.sh \
  data/backups/agent-runtime/<timestamp>/touming_dev.dump \
  data/backups/agent-runtime/<timestamp>/baseline.json
```

恢复脚本会执行：

1. 校验 Dump SHA-256。
2. 清理并恢复专用验证数据库。
3. 对 9 张基线业务表逐一执行 `COUNT(*)`。
4. 任一数量不一致时返回非零退出码。

数量验证程序通过 `BACKEND_CONTAINER` 中已经安装的 Python/psycopg2运行，不要求宿主机安装 Django或psycopg2。

禁止把 `TARGET_POSTGRES_DB` 指向生产数据库。

## 17. 阶段 0-B 验收标准

- [ ] `makemigrations --check --dry-run` 无未提交变更。
- [ ] Migration无冲突，数据库无待应用 Migration。
- [ ] 基线报告包含 9 类业务模型数量。
- [ ] Namespace数量等于 Viking Space数量。
- [ ] 默认报告不包含原始记忆内容或任何凭证。
- [ ] 公司 A/B Fixture连续执行两次，记录数量不增加。
- [ ] 公司 A/B的 accountId、userId和rootUri互不重叠。
- [ ] Alpha只能拥有 `ALPHA-ONLY-731`，Beta只能拥有 `BETA-ONLY-942`。
- [ ] 备份可恢复到专用验证数据库。
- [ ] 恢复前后 9 类数据数量一致。
- [ ] Phase 0-B相关测试全部通过。
