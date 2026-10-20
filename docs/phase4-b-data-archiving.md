# Phase 4-B：OpenClaw 历史数据归档与只读切换

## 目标与边界

本阶段只负责旧 OpenClaw 数据面的停写、历史数据归档、OpenViking 回填完整性复核和最终完整性报告。以下五张表保留且可查询，不执行物理删除：

- `runtime_openclawinstance`
- `runtime_openclawprovisiontask`
- `runtime_openclawruntimesnapshot`
- `runtime_openclawconfigversion`
- `runtime_companyruntimebinding`

依赖边界：

- `TODO(P4-A)`：生产统一运行时达到 100% 后，完成观察窗口并给出停写批准。
- `TODO(P4-C)`：停止 OpenClaw 网桥、容器、定时任务和清理旧基础设施。本分支不实现这些动作。
- 归档文件包含敏感运行时历史，只生成到运维指定的加密受限目录；异地传输和保留周期由运维流程处理。

## 冻结契约

新增环境变量 `OPENCLAW_LEGACY_WRITE_MODE`，仅允许以下值：

| 值 | 行为 |
| --- | --- |
| `read_write` | 默认值；保持现有行为，用于切流前和已批准的回滚。 |
| `read_only` | 五个旧模型允许读取，所有 ORM 写入被拒绝；旧运行时变更 API 返回 HTTP 409。 |

只读模式下，公司升级、Prompt/Skill 同步等公司侧流程不再创建旧实例、任务和配置版本，而返回兼容结果：

```json
{
  "skipped": true,
  "reason": "openclaw_legacy_read_only",
  "message": "Unified runtime is active; legacy OpenClaw synchronization is disabled."
}
```

公司升级返回结构保持既有字段，`instance` 为 `null`，`provision` 和 `bootstrap` 使用上述跳过结果。Phase 0～3 已冻结的字段、运行请求接口和 OpenViking 接口均未修改。

以下旧运行时变更接口在只读模式返回 HTTP 409；详情、列表、审计、统一工作区和 Phase 4-A 治理接口仍可使用：

- `/api/runtime/instance/{provision,retry-provision,bootstrap,deploy-bridge,health-check}`
- `/api/runtime/config/{publish,publish-and-verify,readback}`
- `/api/runtime/snapshots/{backup,restore}`
- `/api/runtime/cron/{jobs/create,remove,run}`
- `/api/runtime/agent-tools/workspace`

## 上线和归档顺序

1. 保持 `OPENCLAW_LEGACY_WRITE_MODE=read_write`，等待 P4-A 的 100% 切流观察结论。
2. 在切换前完成数据库备份，并记录五张旧表的基线：

   ```bash
   python manage.py phase4_archive_legacy_runtime --report-only
   ```

3. P4-A 批准后，将所有 Web 和异步 Worker 的变量统一改为 `read_only` 并滚动重启；确认不存在使用旧环境变量的进程。
4. 在受限且加密的文件系统创建全量归档，目录必须是可信的非符号链接路径：

   ```bash
   umask 077
   python manage.py phase4_archive_legacy_runtime \
     --output-dir /secure/archive/openclaw-legacy-20260814
   ```

5. 保存命令输出和 `manifest.json`。每个表对应一份按主键排序的 JSONL，清单记录行数、SHA-256、Phase 3/OpenViking 完整性结果和 `schemaVersion: 1`。
6. 复制或交接归档后再次验证；验证同时确认文件校验和、数据库行数/摘要和 OpenViking 回填状态：

   ```bash
   python manage.py phase4_archive_legacy_runtime \
     --verify-manifest /secure/archive/openclaw-legacy-20260814/manifest.json
   ```

7. 只有验证结果 `valid=true` 才可交给 P4-C 执行基础设施下线。任何失败都不得删除旧表或旧记录。

若只验收单家公司，可在创建时传 `--cid <company-cid>`；验证会从清单自动恢复相同范围。`--force` 会覆盖已有归档文件，只允许在明确废弃该批次且保留了旧副本时使用。

## 手工验收标准

1. **切换前兼容性**：变量为 `read_write` 时，现有旧实例创建流程行为不变。
2. **数据库只读**：变量改为 `read_only` 并重启后，五张表可执行 `SELECT`；通过 Django ORM 创建、更新或删除均抛出只读错误，表行数不变。
3. **API 只读**：调用旧实例创建、Bootstrap、配置发布、快照备份等接口得到 HTTP 409；实例详情、配置版本列表和审计查询仍正常。
4. **公司流程兼容**：升级透明公司或编辑 Prompt/Skill 时请求成功，响应标记 `openclaw_legacy_read_only`，五张旧表行数不增加。
5. **归档安全**：归档目录权限为 `0700`，JSONL 和清单权限为 `0600`；清单不包含数据库密码、Token 或云密钥。
6. **归档完整性**：`--verify-manifest` 返回 `archiveFilesValid=true`、`databaseUnchanged=true`、`phase3Integrity.summary.valid=true` 和最终 `valid=true`。
7. **不可变校验**：归档后若旧表被意外修改，验证必须返回 `databaseUnchanged=false` 并以非零状态退出。
8. **非破坏性**：归档前后五张表行数一致，数据库中仍保留全部历史数据，没有 Drop/Delete Migration。

## 回滚

只有在 P4-A 批准统一运行时回滚后，才可将全部 Web/Worker 同步改回 `read_write`。已生成的归档保持不可变，不因回滚覆盖或删除。恢复旧写入不等于授权 P4-C 重建或销毁基础设施。
