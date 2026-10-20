# P0-C AGSX 验证与运行配置

本文件记录阶段 0 的 AGSX 技术验证边界。它不实现 P2 的真实 Provider、数据库记录、Redis 锁、Celery Reaper 或 COS 任务产物目录。

## 已验证链路

阶段 0 冒烟脚本会在确认参数存在时执行以下动作：

1. 在 `touming-test/` 下写入、读取、哈希校验并删除一个随机 COS 对象。
2. 创建 AGSX 代码解释器沙箱，运行 `python -V`，上传输入文件并运行 Python 转换。
3. 下载转换结果并校验哈希，在 `finally` 中终止沙箱。
4. 验证控制面返回的 2 CPU、2048 MiB、300 秒生命周期和禁止公网访问配置。
5. 可选验证代码执行超时与公网连通性拒绝。

测试桶当前为可删除的临时桶。传入 `--allow-test-bucket-root-list` 是唯一允许其根目录列举的方式，生产配置严禁使用此参数。

## 环境变量

| 变量 | 用途 | 是否敏感 |
|---|---|---|
| `E2B_DOMAIN` | AGSX E2B 兼容域名，只填域名 | 否 |
| `E2B_API_KEY` | AGSX API Key，由密钥系统注入 | 是 |
| `AGSX_TEMPLATE` | 代码解释器模板名称 | 否 |
| `AGSX_DEFAULT_CPU_CORES` | 默认 `2` | 否 |
| `AGSX_DEFAULT_MEMORY_MIB` | 默认 `2048` | 否 |
| `AGSX_DEFAULT_TTL_SECONDS` | 默认 `300` | 否 |
| `AGSX_NETWORK_POLICY` | 默认 `isolated` | 否 |
| `AGSX_ENABLED` | 新能力开关；P0 和 P1 保持 `false` | 否 |
| `AGSX_TEST_COS_BUCKET` 等 `AGSX_TEST_*` | 仅阶段 0 测试 COS 配置 | 视变量而定 |

兼容旧测试配置时，脚本仍接受 `AGS_TEMPLATE` 或 `AGSX_TOOL_NAME`，但新的部署模板应统一使用 `AGSX_TEMPLATE`。

## 密钥托管与最小权限

- 生产部署从腾讯云 Secret Manager、Kubernetes Secret 或等效部署平台密钥注入读取 `E2B_API_KEY` 和 COS 凭据；不得提交到 Git、镜像层或 `.env.example`。
- Django 日志、Run Event、Snapshot、API/SSE 响应与沙箱环境中不得出现这些值。
- P0 的可删除测试桶可以使用临时宽权限；生产 COS 必须使用专用 CAM 子账号，并限制到公司/Run 前缀。该生产策略在 P2 的 COS 产物实现前复核。
- API Key 只在控制台生成时显示一次；创建、轮换或怀疑泄露后应立即替换，并将旧 Key 失效。

## 状态与错误映射

| 条件 | 冻结错误码 | 是否可重试 | 对外消息 |
|---|---|---:|---|
| 创建超时或运行超时 | `SANDBOX_TIMEOUT` | 是 | Sandbox execution timed out |
| 创建鉴权、模板或请求失败 | `SANDBOX_CREATE_FAILED` | 鉴权/模板否，其他是 | Sandbox could not be created |
| 取消或实例被显式终止 | `RUN_CANCELLED` | 否 | Sandbox execution was cancelled |
| 其他执行/清理异常 | `INTERNAL_ERROR` | 是 | Sandbox operation failed |

原始供应商状态、异常文本和 external ID 只能写入受限日志；P0 的测试报告只保存布尔结果、耗时和脱敏错误类别。

## 本地与云端验证

```bash
cd backend
.venv/bin/python -m django check --settings=config.settings.test
.venv/bin/python manage.py test apps.agent_runtime.tests --settings=config.settings.test

set -a; . ~/.config/touming/agsx.env; set +a
.venv/bin/python -m apps.agent_runtime.providers.sandbox.phase0_smoke \
  --confirm-cloud \
  --allow-test-bucket-root-list \
  --verify-specification \
  --verify-isolated-network \
  --verify-execution-timeout
```

云端命令成功后，需在腾讯云控制台确认无运行中测试实例，并保存控制台截图与脱敏 JSON 报告。

## 当前真实验证记录

- 创建、`python -V`、文件上传/下载、Python 执行、COS 临时对象删除、显式沙箱终止：通过。
- 禁公网控制面设置、沙箱内出站 TCP 拒绝、1 秒代码执行超时：通过。
- `touming-p0-code` 当前控制面返回 2 CPU、1000 MiB；内存未达到冻结要求的 2048 MiB，因此完整 2C2G 验收未通过。
- 在控制台将该工具的内存调整为 2 GiB 后，必须重跑本文件中的完整云端命令；测试脚本会再次验证并自动清理。

## 依赖交接

已验证的组合是 `e2b-code-interpreter==2.9.0` 与 `e2b==2.37.1`。按照团队所有权规则，`backend/requirements.txt` 由工程师 A 在阶段集成时汇总该变更。
