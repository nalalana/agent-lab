# Phase 3-B 知识索引和数据迁移

任务编号：`P3-B`。本实现不修改 Phase 0–2 冻结的 Runtime、MemoryGateway、Tool 或 Sandbox Contract。

## 数据和状态约定

- 原始文件继续使用 `runtime_workspacefile`，COS 是事实源。
- 索引任务继续使用冻结表 `agent_runtime_memory_sync_job`，`job_type=knowledge_index`。
- `source_ref` 固定为 `workspace_file:<id>`。
- `idempotency_key` 由公司主键、文件主键、逻辑路径摘要和内容校验摘要构成。
- 知识文件索引状态为 `pending/indexing/completed/failed`；`not_applicable` 仅用于非知识文件。
- 历史 `submitted` 不能证明向量索引完成，Migration `0014` 将其重置为 `pending`。
- OpenViking Account 和目标 URI 只根据数据库中的公司上下文计算，API 不接受调用方传入 Namespace、COS Key 或签名 URL。
- `KnowledgeRetriever` 先按 Agent 的 `knowledge.allow/deny` 过滤已完成索引的文件，再逐一限定 `target_uri` 搜索；OpenViking 越权返回会被拒绝。

## 管理 API

所有 API 都要求登录且通过公司管理员权限校验。`cid` 必须放在 Query String 中。

| 方法 | 路径 | 输入 | 输出 `data` |
|---|---|---|---|
| GET | `/api/agent-runtime/runs` | `cid,status?,agent_id?,limit?,offset?` | Run 分页列表 |
| GET | `/api/agent-runtime/runs/<uuid>` | `cid` | Run、事件、脱敏 Tool Call 和安全 Sandbox 字段 |
| GET | `/api/agent-runtime/memory/jobs` | `cid,status?,job_type?,agent_id?,limit?,offset?` | MemorySyncJob 分页列表 |
| POST | `/api/agent-runtime/memory/jobs/<uuid>/retry` | `cid` | 重新入队后的任务 |
| GET | `/api/agent-runtime/knowledge/files` | `cid,status?,limit?,offset?` | 知识文件及索引状态 |
| POST | `/api/agent-runtime/knowledge/files/<id>/reindex` | `cid` | 索引任务和文件状态 |
| GET | `/api/agent-runtime/migration/integrity` | `cid` | 公司迁移完整性报告 |

统一响应：

```json
{"result": 1, "message": "查询成功", "data": {}}
```

失败响应不会返回堆栈、OpenViking 原始响应、密钥或 Sandbox `external_id`。

## 数据回填和完整性报告

先预览，不改数据：

```bash
python manage.py agent_runtime_phase3_migrate --cid <CID>
```

修复 `TransparentCompanyAgentVikingSpace`、创建幂等知识任务并输出报告：

```bash
python manage.py agent_runtime_phase3_migrate --cid <CID> --apply --output /tmp/phase3-report.json
```

确认 Celery Worker 和 OpenViking 可用后才派发：

```bash
python manage.py agent_runtime_phase3_migrate --cid <CID> --apply --dispatch
```

Migration 只创建持久任务，不在数据库迁移事务中发起网络请求。

## 与其他工程师的集成点

- `TODO(P3-A)`：Phase 3-A 合并时，把 `enqueue_pending_knowledge_files` 加入 Celery Beat，并在统一 COS 上传完成入口调用同一个 `KnowledgeIndexService.enqueue()`。
- `TODO(P3-A)`：在统一 Graph 的配置解析后调用 `KnowledgeRetriever.recall()`，把返回内容按既有 Prompt Builder 上下文格式装配；Phase 3-B 不修改 A 所有的 LangGraph。
- `TODO(P3-A)`：Memory `delete` 类型任务等待 Phase 3-A 审计删除入口后接入；Phase 3-B API 不猜测其接口。
- Phase 3-C 只消费本文件定义的管理 API，不需要读取 OpenViking 管理密钥。

## 手工验收

1. 上传 `knowledge/phase3-unique.md`，运行 pending scanner，状态依次为 `pending/indexing/completed`。
2. 在 OpenViking 临时不可用时执行索引，确认任务重试且最终为 `failed`；恢复后调用 retry API 成功。
3. 对同一文件重复 reindex，确认只有一个相同幂等键的任务；修改 checksum 后生成新任务。
4. 公司 A、B 各上传唯一内容，确认列表和 Run/Memory API 不能跨公司读取。
5. 普通成员请求任一管理 API，确认返回 HTTP 403；公司管理员返回 HTTP 200。
6. 运行 `agent_runtime_phase3_migrate --apply` 两次，确认 Viking Space 和 MemorySyncJob 数量第二次不增长。
7. 运行完整性报告，确认 Prompt、Skill、Memory Policy、权限映射和 Viking Space 没有错误。
