# Phase 4-C 基础设施全量切换、退役与回滚 Runbook

## 1. 适用范围与当前边界

本 Runbook 适用于“旧项目暂无用户、跳过灰度、一次性全量切换”的 Phase 4-C 发布。开发仓库固定为 `~/touming`；完成合并后，才在 `/home/ubuntu/project/touming/touming-backend` 做生产形态的重建、全量测试和运行验证。

本阶段负责：

- AGSX 公司级与平台级并发保护、成本估算、失败和遗留沙箱告警。
- Shared Host Bridge 与每公司 OpenClaw Compose 项目的可审计停用。
- 旧镜像、租户目录、Bridge 部署包和恢复命令保留一个发布周期。
- Phase 4-B 已合并；Phase 4-C 将 Lighthouse Provision、Bootstrap、Deploy Bridge 和旧健康检查入口固定为退役 tombstone，并让同名历史 Celery 消息永久 no-op。
- 在线 Agent 请求链路已硬切 unified；即使存在 `legacy`、缺失策略或 `canary` 未命中，也只返回配置错误，不再调用 OpenClaw Adapter。

最终收尾已与 Phase 4-A/B 合并结果对齐：在线请求硬切 unified，旧模型只读/归档逻辑仍由 Phase 4-B 拥有，基础设施退役由本 Runbook 执行。

## 2. 硬性前置条件

以下条件任一不满足都必须停止发布：

1. Phase 4-B 已合入，`OPENCLAW_LEGACY_WRITE_MODE=read_only`，最终归档 manifest 已验证，旧 Provision/Bootstrap/Deploy Bridge 不再创建任务或记录。
2. 所有公司均存在 `CompanyAgentRuntimePolicy(mode='unified')`；不得存在 `legacy`、`canary` 或缺失策略。
3. Phase 3 知识召回完整链路已由对应工程师验收；公司 A/B 的知识和记忆隔离测试通过。
4. Phase 4-A 的异步 Django ORM 回归已修复，`apps.agent_runtime.tests` 全量通过且测试数据库能够正常销毁。
5. AGSX 生产域名、凭证、2C2G 模板、全局并发上限、成本单价和两级成本预算已配置。
6. Celery Worker、Celery Beat、Reaper 和运行告警任务均在线。
7. 旧 Bridge/租户数据备份完成，校验和通过，并已记录恢复负责人和一个发布周期的到期时间。

## 3. Phase 4-C 新增配置

在受保护的 `.prod` 中配置以下值。示例文件中的 `0` 仅表示尚未配置；启用 AGSX 前必须替换为真实值。

|变量|含义|生产要求|
|---|---|---|
|`AGSX_GLOBAL_MAX_CONCURRENT_SANDBOXES`|平台同时活动的沙箱硬上限|正整数，初始建议 `25`|
|`AGSX_FAIL_CLOSED_ON_QUOTA_BACKEND_ERROR`|Redis 配额后端异常时是否拒绝创建|必须为 `true`|
|`AGSX_ESTIMATED_COST_MICRO_UNITS_PER_SECOND_2C2G`|2C2G 每秒估算成本|按供应商合同填写正整数|
|`AGSX_DEFAULT_COMPANY_DAILY_COST_BUDGET_MICRO_UNITS`|每公司默认日成本预算|正整数|
|`AGSX_GLOBAL_DAILY_COST_BUDGET_MICRO_UNITS`|平台日成本预算|正整数|
|`AGSX_COST_CURRENCY`|成本币种|三位大写代码，例如 `CNY`|
|`AGSX_ALERT_FAILURE_WINDOW_MINUTES`|沙箱失败率窗口|建议 `15`|
|`AGSX_ALERT_FAILURE_RATE`|失败率告警阈值|建议 `0.2`|
|`AGSX_ALERT_FAILURE_MIN_CREATES`|计算失败率的最少沙箱数|建议 `5`|
|`OPENCLAW_LEGACY_WRITE_MODE`|Phase 4-B 五张旧表写保护|必须为 `read_only`|

微单位换算：币种的 `1` 个主单位等于 `1,000,000` 微单位。成本单价必须来自当前供应商合同，禁止把测试估值直接带入生产。

## 4. 开发分支验证（`~/touming`）

### 4.1 确认分支和差异

```bash
ssh lxc@43.129.235.38
cd ~/touming
pwd
git status --short --branch
git diff --check
```

### 4.2 使用隔离 PostgreSQL 15

```bash
docker run --rm -d \
  --name phase4c-test-postgres \
  -e POSTGRES_USER=postgres \
  -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=touming_phase4c \
  -p 127.0.0.1:55432:5432 \
  postgres:15-alpine
```

### 4.3 定向测试、全量测试与 Migration 检查

```bash
cd ~/touming/backend
export POSTGRES_HOST=127.0.0.1
export POSTGRES_PORT=55432
export POSTGRES_USER=postgres
export POSTGRES_PASSWORD=postgres
export POSTGRES_DB=touming_phase4c
export SECRET_KEY='replace-with-test-secret-longer-than-32-characters'
export DJANGO_SETTINGS_MODULE=config.settings.test

.venv/bin/python manage.py test \
  apps.agent_runtime.tests.test_phase2_sandbox_provider \
  apps.agent_runtime.tests.test_phase3_operations \
  apps.agent_runtime.tests.test_phase4c_preflight \
  apps.runtime.tests.test_phase4_data_archiving \
  apps.runtime.tests.test_phase4c_runtime_retirement \
  --verbosity 1

cd ~/touming
python3 scripts/ops/test_phase4c_retirement_tools.py

cd ~/touming/backend
.venv/bin/python manage.py test apps.agent_runtime.tests --verbosity 1
.venv/bin/python manage.py test --verbosity 1
.venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/python manage.py check --deploy
```

测试结束后只移除本次创建的临时容器：

```bash
docker stop phase4c-test-postgres
```

## 5. 部署目录的全量重建与测试

以下步骤只在代码合并并锁定发布提交后执行：

```bash
cd /home/ubuntu/project/touming/touming-backend
pwd
git status --short --branch
git fetch origin
git switch develop
git pull --ff-only origin develop
git rev-parse HEAD
```

记录发布提交为 `PHASE4C_RELEASE_COMMIT`。不要在运行中的容器内临时 `pip install`；必须从锁定提交重建镜像：

```bash
docker compose -f config/docker/compose.prod.yml build --pull \
  backend celery-worker celery-beat
docker compose -f config/docker/compose.prod.yml up -d \
  backend celery-worker celery-beat
docker compose -f config/docker/compose.prod.yml ps
```

执行生产配置检查：

```bash
docker compose -f config/docker/compose.prod.yml exec -T backend \
  python manage.py check --deploy
docker compose -f config/docker/compose.prod.yml exec -T backend \
  python manage.py showmigrations --plan
```

在同一部署主机上按第 4 节再次使用隔离 PostgreSQL 跑后端全量测试。不得让自动化测试连接生产数据库。

## 6. 无灰度全量验收

旧项目无用户，因此不执行 10%/30% 灰度和观察日。替代顺序固定为：

1. 空库 Migration。
2. 后端全量自动化测试。
3. 固定验收数据测试：公开聊天、Agent Test、定时任务、取消、普通问答、代码工具、超时、失败、Reaper。
4. 公司 A/B 租户隔离测试。
5. `mode=unified` 全量部署。
6. 证明验收期间没有 Bridge 请求。
7. 备份旧基础设施。
8. dry-run 停用计划。
9. 全量停止 Bridge 和每公司 Compose。
10. 再跑一次完整 smoke 与告警检查。

固定验收数据可通过现有命令创建；不得在生产业务库反复执行：

```bash
python manage.py agent_runtime_seed_acceptance_data --help
```

## 7. 生成后端预检报告

先保持 Bridge 在线作为回滚能力。使用 Phase 4-B 已验证的最终归档 manifest，在生产容器内生成第一份报告：

```bash
cd /home/ubuntu/project/touming/touming-backend
mkdir -p logs/phase4c
docker compose -f config/docker/compose.prod.yml exec -T backend \
  python manage.py agent_runtime_phase4c_preflight \
  --require-legacy-archive \
  --legacy-archive-manifest /secure/archive/openclaw-legacy/manifest.json \
  --output /logs/phase4c/preflight-before-stop.json \
  --fail-on-blockers
```

此时 `legacy_bridge_configuration` 可以是 `warning`，但 `legacy_runtime_read_only` 和 `legacy_archive_integrity` 必须为 `pass`，且 `summary.ready` 必须为 `true`。报告只记录旧变量名称，不输出值。

停止条件：

- `summary.blockers > 0`。
- 仍有活动 AgentRun、活动 AGSX 沙箱或 Reaper 清理候选。
- 存在旧 Provision/Bootstrap/Deploy Bridge 待执行/执行中任务。
- 任一公司不是 `unified` 或缺失策略。
- AGSX 生产配置不完整。
- `OPENCLAW_LEGACY_WRITE_MODE` 不是 `read_only`。
- Phase 4-B 归档文件、数据库不可变性、Phase 3 完整性或 OpenViking 回填校验失败。

## 8. 证明在线链路不再调用 Bridge

记录全量验收开始时间，并在所有验收请求完成后检查 Bridge 日志：

```bash
PHASE4C_CUTOVER_START='2026-08-17 00:00:00 UTC'
sudo journalctl \
  -u tsds-shared-host-bridge.service \
  --since "${PHASE4C_CUTOVER_START}" \
  --no-pager
```

逐条核对验收请求时间段内没有 `/runtime/<namespace>/chat`、provision、bootstrap、deploy-bridge 或 workspace 写请求。`AgentRun.routeCounts` 不能单独作为证据，因为旧 legacy 路径不保证创建 `AgentRun`。

## 9. 备份和库存清单

在 Shared Host 上执行现有备份脚本。`RETENTION_DAYS` 必须至少覆盖一个完整发布周期：

```bash
sudo env \
  BACKUP_ROOT=/var/backups/touming/openclaw \
  RETENTION_DAYS=30 \
  TENANTS_DIR=/data/tsds/tenants \
  BRIDGE_DIR=/opt/tsds/bridge \
  BRIDGE_ENV_FILE=/etc/tsds/shared-host-bridge.env \
  ENCRYPTION_PASSPHRASE_FILE=/root/secure/openclaw-backup-passphrase \
  bash scripts/ops/backup_232_openclaw.sh
```

验证最新备份目录中的 `SHA256SUMS`，并把归档路径、校验和、发布提交和恢复负责人写入发布记录。

安装只读库存和停用工具：

```bash
sudo install -d -m 0750 /opt/tsds/ops /var/lib/tsds/phase4c
sudo install -m 0750 scripts/ops/phase4c_openclaw_inventory.py /opt/tsds/ops/
sudo install -m 0750 scripts/ops/phase4c_stop_openclaw.py /opt/tsds/ops/
```

生成库存清单；`--runtime-root` 必须与该主机真实 `TSDS_RUNTIME_ROOT` 一致：

```bash
sudo python3 /opt/tsds/ops/phase4c_openclaw_inventory.py \
  --runtime-root /data/tsds/tenants \
  --service-name tsds-shared-host-bridge.service \
  --bridge-port 19080 \
  --output /var/lib/tsds/phase4c/openclaw-inventory.json
```

清单必须包含：Bridge systemd 状态、租户 Compose 路径、Compose 项目、镜像引用/ID、发布提交和 `preserveForOneRelease=true`。它不会读取环境变量值、租户配置内容或 secret 文件。

把 `preflight-before-stop.json` 安全复制到同一 Shared Host 的 `/var/lib/tsds/phase4c/`。

## 10. Dry-run 与正式停用

先生成 dry-run 结果：

```bash
sudo python3 /opt/tsds/ops/phase4c_stop_openclaw.py \
  --inventory /var/lib/tsds/phase4c/openclaw-inventory.json \
  --preflight-report /var/lib/tsds/phase4c/preflight-before-stop.json \
  --output /var/lib/tsds/phase4c/stop-dry-run.json
```

人工检查 `stopCommands`：只能出现 `systemctl disable --now` 和 `docker compose ... stop`，不能出现 `down`、`rm`、`rmi`、卷删除或目录删除。

确认维护窗口内没有新请求后执行：

```bash
sudo python3 /opt/tsds/ops/phase4c_stop_openclaw.py \
  --inventory /var/lib/tsds/phase4c/openclaw-inventory.json \
  --preflight-report /var/lib/tsds/phase4c/preflight-before-stop.json \
  --apply \
  --confirm STOP_OPENCLAW_PHASE4C \
  --output /var/lib/tsds/phase4c/stop-applied.json
```

工具会校验：报告版本、报告时效、主机名、运行根目录、Compose 路径边界、systemd 服务名和 `summary.ready`。任何校验失败都不会执行停用命令。

## 11. 停用后验证

在 Shared Host 验证：

```bash
sudo systemctl is-active tsds-shared-host-bridge.service
sudo systemctl is-enabled tsds-shared-host-bridge.service
docker ps --filter label=com.docker.compose.project
ss -ltnp
```

预期：Bridge 为 `inactive/disabled`，19080 不再监听，旧租户容器不运行；镜像、已停止容器、卷、Compose 文件和租户目录仍存在。

在后端重新执行固定验收集、公开聊天 smoke、Agent Test smoke、代码工具 smoke、取消/超时 smoke，并检查：

- 新请求全部产生 `AgentRun(engine='unified')`。
- 普通问答不创建沙箱。
- 工具请求在公司/全局限额内创建 AGSX，并在结束后删除。
- `sandbox.quota.rejected.total`、成本、失败、清理和 orphan 指标/告警可见。
- Bridge 停止后所有新链路仍成功。

## 12. 清空旧环境变量与最终预检

Phase 4-C 代码和示例配置已不再加载这些变量。只有 Phase 4-B 和停用后 smoke 均通过，才从真实活动 `.prod`、容器环境和密钥管理系统删除或清空：

- `RUNTIME_DOCKER_SHARED_HOST_API_BASE`
- `RUNTIME_DOCKER_SHARED_HOST_API_TOKEN`
- `RUNTIME_DOCKER_SHARED_HOST_TIMEOUT`
- `RUNTIME_DOCKER_SHARED_HOST_BOOTSTRAP_TIMEOUT`
- `TENCENT_LIGHTHOUSE_*`
- 仅供 Bridge/OpenClaw 的 `TSDS_*` 部署变量

重新构建并发布 backend、Celery Worker、Celery Beat，然后执行严格预检：

```bash
docker compose -f config/docker/compose.prod.yml exec -T backend \
  python manage.py agent_runtime_phase4c_preflight \
  --require-bridge-disabled \
  --require-legacy-archive \
  --legacy-archive-manifest /secure/archive/openclaw-legacy/manifest.json \
  --output /logs/phase4c/preflight-final.json \
  --fail-on-blockers
```

## 13. 退役接口与历史队列消息

以下旧路径保留一个发布周期的稳定 tombstone，直接返回 HTTP `410`，不会解析租户参数、创建旧记录或调用云 API：

- `/api/runtime/lighthouse/{catalog,purchase-dry-run}`
- `/api/runtime/instance/{provision,retry-provision,bootstrap,deploy-bridge,health-check}`

历史 Celery 队列中的 Provision、Bootstrap、Deploy Bridge 和 OpenClaw Health 消息仍保留任务注册名，但只返回 `retired=true` 的无敏感信息结果，不读取数据库、不执行 Provider、不启动子进程。旧部署脚本只作为恢复包保留，不属于正常部署步骤。

## 14. 回滚

触发条件：统一聊天系统性失败、跨租户访问、AGSX 大面积创建/清理失败、成本失控、数据完整性失败。

恢复顺序固定为“租户 Compose → Bridge → 上一发布版本后端”：

1. 从 `stop-applied.json.recoveryCommands` 逐条审核并执行租户 `docker compose ... up -d`。
2. 执行 `systemctl enable --now tsds-shared-host-bridge.service`。
3. 恢复受保护的旧 Bridge 环境变量。
4. 部署库存清单记录的上一发布提交/镜像。
5. 若数据库已发生 Phase 4-B 之后不可逆变化，按 Phase 4-B 恢复说明处理；禁止直接写旧只读表。
6. 重新验证 Bridge health、租户 gateway、workspace、OpenViking 和公开聊天。

停用工具不删除数据，因此正常回滚不需要先恢复备份；只有文件/卷损坏时才使用加密备份。

## 15. 一个发布周期后的清理

下一个发布周期结束且没有回滚后，另开受审 PR 执行最终删除：旧镜像、已停止容器、租户 Compose、Bridge systemd unit、Bridge env、Lighthouse deploy 脚本和旧备份。删除前再次生成库存、核对保留期限并取得发布负责人批准；本 Runbook 和当前停用工具不执行这些删除。
