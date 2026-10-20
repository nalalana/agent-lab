# Feature: 平台风控与滥用防护

## 当前代码状态

- 仓库：`https://github.com/kuibu/touming.git`
- 基础分支：`merge/release2.1-auth-security`
- 当前工作分支：`feature/risk-control`
- 当前代码已同步到 `cb9afa1 Merge text moderation workflow`。
- 当前基线已经包含：
  - 图片审核工作流。
  - 文本审核工作流。
  - `backend/apps/moderation` 统一审核 app。
  - 生产 Redis / cache 配置方向。
  - OpenClaw runtime bridge 配置方向。
  - immudb / OpenClaw 备份脚本方向。

## 协作边界

这是风控能力的独立工作区。后续实现应在本目录继续，不要直接在主后端工作区改复杂逻辑。

本轮目标是准备代码基线和功能说明；真正实现时应按本文档逐步开发、补测试、验证，再进入 review / merge 流程。

## 背景

平台已经具备企业公开数据、项目表格、外部 API 写入、Agent / OpenClaw 调用、图片审核和文本审核能力。正式上线前仍需要补齐统一风控，避免以下风险：

- 注册、登录、验证码接口被刷。
- 登录接口被撞库或暴力尝试。
- API Key 被滥用，导致外部写入、读取或资源消耗失控。
- OpenClaw / Agent 调用被高频触发，消耗运行时资源。
- 公开客服、Agent 测试消息、表格写入等入口被批量灌入垃圾内容。
- 审核接口、人工复核接口或运营接口被异常调用。
- 单一 IP、手机号、用户、公司、API Key 或设备特征持续制造异常流量。

风控能力应与现有内容安全互补：内容审核负责判断文本/图片是否安全，风控负责判断请求行为、频率、主体信誉和资源预算是否安全。

## 目标

- 建立统一后端风控服务，支持多维度限流、封禁、白名单和日志。
- 对注册、登录、短信验证码、API Key、外部 API、OpenClaw / Agent 调用和审核相关接口建立基础规则。
- 通过 Redis / Django cache 承载高频计数和短期状态。
- 通过数据库记录风控命中日志、人工处置和长期黑白名单。
- 为运营支持能力预留查询、解封、加白和规则调整接口。
- 保证正常用户体验不被明显误伤，并让被拦截请求可追踪、可解释。

## 建议实现范围

### 1. 风控 app 或模块

建议新增后端 app：

```text
backend/apps/risk_control/
```

建议包含：

```text
apps.py
models.py
services.py
serializers.py
views.py
urls.py
tests/
```

如果实现者认为复用现有 app 更合适，也可以把高频校验 service 放在公共模块中，但数据库模型、接口和测试仍应保持清晰边界。

### 2. 核心模型

建议模型：

- `RiskRule`：规则定义，例如短信、登录、API Key、OpenClaw 调用的阈值和窗口。
- `RiskDecisionLog`：每次命中或放行的决策日志。
- `RiskBlockListEntry`：黑名单，支持 IP、手机号、用户、公司、API Key、UA hash 等主体。
- `RiskAllowListEntry`：白名单或更高额度主体。
- `RiskManualAction`：运营人工处置记录，例如解封、加白、调整额度。

日志中不要长期保存完整敏感原文、token 或 secret。手机号、API Key、IP 等字段如需展示，应按业务需要脱敏或保存 hash。

### 3. 核心服务

建议提供统一 service：

```python
RiskControlService.evaluate(...)
RiskControlService.check_rate_limit(...)
RiskControlService.record_decision(...)
RiskControlService.is_blocked(...)
RiskControlService.apply_manual_action(...)
```

建议请求上下文至少包含：

- `scope`：业务范围，例如 `auth.login`、`sms.send`、`external_api.upsert`、`runtime.openclaw.call`。
- `actor_user_id`
- `company_id`
- `api_key_id`
- `phone_hash`
- `ip`
- `user_agent_hash`
- `request_path`
- `request_method`
- `resource_id`

### 4. Redis / cache 计数

当前基线已有 Redis cache 配置，风控高频计数应优先使用 Django cache / Redis。

建议 key 设计包含：

```text
risk:{scope}:ip:{ip}
risk:{scope}:user:{user_id}
risk:{scope}:company:{company_id}
risk:{scope}:api_key:{api_key_id}
risk:{scope}:phone:{phone_hash}
```

窗口策略建议先支持固定窗口或滑动窗口近似实现，不要在第一版引入过重的复杂算法。

### 5. 必须覆盖的入口

第一版建议至少覆盖：

- 短信验证码发送。
- 注册。
- 登录失败次数和登录频率。
- API Key 创建、读取、外部 API 写入。
- OpenGate 表格普通写入和外部 API 写入。
- OpenClaw / Agent runtime 调用。
- 公开客服输入和 Agent 测试消息。
- 文本/图片审核状态查询与人工复核接口。

### 6. 与内容审核的关系

风控不替代 `backend/apps/moderation`。

建议顺序：

```text
请求主体与频率风控
  -> 权限校验
  -> 内容审核
  -> 业务写入或 runtime 调用
  -> 审计 / immudb 存证
```

对高风险入口，如果风控拒绝，应在进入第三方内容审核或 OpenClaw 调用前拦截，减少成本和资源消耗。

### 7. 环境变量

建议新增到 `.dev.example`、`.test.example`、`.prod.example`：

```text
RISK_CONTROL_ENABLED=true
RISK_CONTROL_FAIL_POLICY=allow|deny
RISK_CONTROL_REDIS_PREFIX=touming:risk
RISK_CONTROL_DEFAULT_IP_LIMIT_PER_MINUTE=60
RISK_CONTROL_DEFAULT_USER_LIMIT_PER_MINUTE=120
RISK_CONTROL_SMS_IP_LIMIT_PER_HOUR=20
RISK_CONTROL_SMS_PHONE_LIMIT_PER_DAY=10
RISK_CONTROL_LOGIN_FAILURE_LIMIT_PER_15M=10
RISK_CONTROL_API_KEY_LIMIT_PER_MINUTE=120
RISK_CONTROL_RUNTIME_CALL_LIMIT_PER_MINUTE=30
RISK_CONTROL_LOG_ALLOW_DECISIONS=false
```

生产默认建议：

```text
RISK_CONTROL_ENABLED=true
RISK_CONTROL_FAIL_POLICY=deny
RISK_CONTROL_LOG_ALLOW_DECISIONS=false
```

开发和迁移阶段可临时使用更宽松策略，但必须在文档中写明。

## 需要用户确认

- 短信验证码：单 IP、单手机号、单用户的小时/日限额。
- 登录：失败多少次触发冷却，冷却多久。
- API Key：默认 QPS / 分钟 / 日额度，以及是否按公司分配总额度。
- OpenClaw / Agent：每公司、每用户、每 Agent 的调用预算。
- 是否允许运营人员在后台解封、加白、调整额度。
- 命中风控后前端展示文案：直接提示频率过高，还是进入人工处理。
- 是否需要接入腾讯云 WAF / CDN / 网关侧限流作为后端风控的外层防护。

## 验收标准

- 同 IP 或同手机号不能无限刷验证码。
- 登录失败达到阈值后会被冷却，正常登录成功后状态处理合理。
- API Key 无法无限调用外部写入或读取接口。
- OpenClaw / Agent 调用有频率和预算限制。
- 风控命中有日志，可按用户、公司、IP、API Key、scope 查询。
- 黑名单、白名单和人工解封有审计记录。
- 风控拒绝发生在高成本第三方服务或 runtime 调用之前。
- 测试覆盖短信、登录、API Key、外部写入、runtime 调用和人工处置。
- 生产 secret 不进入 Git，不进入聊天记录，不写死在镜像。

## 明确不做

- 不只依赖前端限制。
- 不把 PostgreSQL 作为高频限流主存储。
- 不在日志里保存完整 token、API Key、验证码、敏感文本或云服务密钥。
- 不绕过已有 `apps.moderation` 内容审核。
- 不在主工作区直接做复杂实现。

## 建议验证命令

```bash
cd backend
python manage.py test apps.user.tests
python manage.py test apps.opengate.tests
python manage.py test apps.runtime.tests
python manage.py test apps.moderation.tests
python manage.py test apps.risk_control.tests
```

如果本地依赖不完整，先按 `CONFIG.md` 配置 Redis、PostgreSQL、immudb 和环境变量。
