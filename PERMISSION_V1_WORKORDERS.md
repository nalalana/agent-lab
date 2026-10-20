# Permission V1 Single-Step Work Orders

这份文档把权限系统拆成 6 个可以单独丢给 `Codex/Cursor` 的单步工单。

每个工单都包含：
- 目标
- 边界
- 可直接执行的提示词
- 代码化验收条件

适用仓库：
- Backend: `C:\Users\dawei\touming-remote-edit\repos\touming-backend`
- Frontend: `C:\Users\dawei\touming-remote-edit\repos\touming-frontend`

## Shared Context

当前确认过的事实：

- 权限模型在 `backend/apps/company/models.py`，已经有：
  - `skillsInclude`
  - `skillsExclude`
  - `toolsAllow`
  - `toolsDeny`
  - `knowledgeRead`
  - `knowledgeDeny`
- 管理员鉴权可以复用 company / runtime 现有 IAM 校验方式。
- Frontend 权限页当前仍主要使用 `Textarea` 手工输入。
- runtime 已有 workspace tree 能力，后端可复用。
- 线上开发服务器的 tenant 实际 workspace 文件确实位于 `/data/tsds/tenants/<tenant>/workspace/...`。
- 但产品侧权限系统必须统一使用 `logical path`，例如：
  - `knowledge/1775028071209-workspace.zip`
- 当前 agent runtime 目录中确实存在：
  - `agent.json`
  - `skills.json`
  - `permissions.json`
  - `knowledge.json`
- 当前开发环境里 `skills.json` 实际上还是空列表，说明 skills 链路还没有证明已被 OpenClaw 真正消费。

## Global Rules For Every Work Order

下面这些约束对所有工单都生效：

1. 只使用 `logical path` 作为业务路径。
   - 不允许把 `/data/...`、`/srv/openclaw-home/...` 作为前端展示或 API 返回主路径。
2. 所有管理接口必须经过现有后台 IAM / admin 鉴权。
3. 每一步都必须补自动化测试。
4. 每一步都必须输出：
   - 修改了哪些文件
   - 新增/修改了哪些测试
   - 如何运行测试
5. 如果当前仓库无法闭环实现，必须明确写出 blocker，不允许假装完成。

---

## Step 1: Backend Knowledge Workspace Tree API

### Goal

新增一个给权限配置页面使用的“知识目录树”接口，管理员可以看到 tenant workspace 的 `logical path` 树，并用于后续选择 `knowledgeRead / knowledgeDeny`。

### Scope

- Backend only
- 优先修改：
  - `backend/apps/company/views.py`
  - `backend/apps/company/urls.py`
  - `backend/apps/company/services.py`
  - `backend/apps/company/serializers.py`
  - 如有必要：`backend/apps/runtime/services.py`

### Work Order Prompt

```text
请在当前代码库中实现一个“权限配置专用的 workspace tree 浏览接口”，用于权限页选择 knowledge 可读/拒绝范围。

目标：
- 管理员可以浏览当前公司 tenant 的 workspace 逻辑目录树
- 返回 logical path 树
- 只做目录/文件枚举，不做内容读取

实现要求：
1. 新增一个 admin-only 接口，建议放在 company app 下，命名与现有 permission 接口风格一致。
2. 接口入参至少包含：
   - cid
   - path（可选，logical path）
   - recursive（可选，默认 false）
3. 接口内部必须复用现有 admin/IAM 校验逻辑，不允许匿名访问。
4. 数据源必须优先复用 runtime 的 `list_workspace_tree` 能力。
5. 返回值中每个 item 至少包含：
   - name
   - logical_path
   - kind（file/directory）
   - runtime_visible（可选）
   - sync_status（若能补充则补充）
6. 明确过滤掉宿主机绝对路径、容器路径、stderr 等内部信息。
7. 接口仅用于权限配置，不要顺手做文件读取或删除。

建议修改文件：
- backend/apps/company/serializers.py
- backend/apps/company/views.py
- backend/apps/company/urls.py
- backend/apps/company/services.py
- 如需补 runtime helper，可改 backend/apps/runtime/services.py

必须完成的代码化验收条件：
1. 新增接口可以被 Django 测试直接调用。
2. admin 访问成功时，返回 JSON 中不出现：
   - `/data/`
   - `/srv/openclaw-home/`
   - `stderr`
3. 非 admin 访问返回 401 或 403。
4. 当 workspace 中存在 `knowledge/1775028071209-workspace.zip` 时，接口返回中能看到该 logical path。
5. 至少新增以下自动化测试：
   - admin 可访问
   - 非 admin 拒绝
   - 返回不泄露宿主机路径
   - recursive=false
   - recursive=true
6. 输出测试命令与测试结果。
```

### Acceptance Checklist

- 新接口已接入 URL。
- 测试能直接断言返回结构。
- 响应只返回 `logical_path`，无宿主机路径。
- 至少一条测试断言 `knowledge/1775028071209-workspace.zip` 可见。

---

## Step 2: Frontend Knowledge Path Picker

### Goal

把权限页面里的“知识可读 / 知识拒绝”从 `Textarea` 改成目录树选择器，保存时仍提交 `knowledge_read / knowledge_deny` 数组。

### Scope

- Frontend only
- 优先修改：
  - `app/dashboard/companies/[id]/permissions/page.tsx`
  - `lib/api.ts`
  - 新增可复用组件，例如 `components/company/KnowledgePathPicker.tsx`

### Work Order Prompt

```text
请改造权限页面，把“知识可读 / 知识拒绝”从自由文本 Textarea 改成可视化目录树选择器。

目标：
- 模板弹窗和 Agent 绑定弹窗都能选择目录/文件
- 最终保存的仍是 `knowledge_read` / `knowledge_deny` 数组
- 默认不让用户手填逗号分隔路径

实现要求：
1. 基于现有权限页实现一个可复用的 `KnowledgePathPicker` 组件。
2. 组件通过后端 workspace tree 接口加载 logical path 树。
3. 支持勾选目录和文件：
   - 目录表示递归授权
   - 文件表示单文件授权
4. 组件必须支持：
   - 已选项展示
   - 删除已选项
   - 在模板弹窗和 Agent 绑定弹窗中复用
5. 保留“高级模式”开关，只有打开后才允许手工编辑原始 logical path 列表。
6. 保存时前端仍调用现有 upsert API，只是把 picker 的结果序列化成 `knowledge_read` / `knowledge_deny`。
7. 前端页面中默认不再展示知识权限的纯文本 Textarea。

建议修改文件：
- touming-frontend/app/dashboard/companies/[id]/permissions/page.tsx
- touming-frontend/lib/api.ts
- 新增组件文件，例如 `components/company/KnowledgePathPicker.tsx`

必须完成的代码化验收条件：
1. 模板弹窗和 Agent 绑定弹窗都能看到目录树选择器。
2. 默认界面不再直接显示知识权限原始 Textarea。
3. 已选择路径保存后，刷新页面可正确回显。
4. 前端提交给后端的 payload 中：
   - `knowledge_read` 为数组
   - `knowledge_deny` 为数组
   - 元素为 logical path
5. 页面中不出现 `/data/...` 宿主机路径。
6. 如果仓库已有前端测试体系，则补组件测试；如果没有，则至少抽出序列化 helper 并补对应测试。
7. 运行 `pnpm build` 或项目现有前端构建命令必须通过。
```

### Acceptance Checklist

- 两个弹窗都完成替换。
- 保存和回显都走数组。
- 默认 UI 不暴露原始文本框。
- 前端构建通过。

---

## Step 3: Enforce Knowledge Read / Deny At Runtime

### Goal

确保 `knowledgeRead / knowledgeDeny` 真正控制目录列举、文件定位、文件读取，而不是只停留在配置层。

### Scope

- Backend / runtime
- 优先修改：
  - `backend/apps/runtime/policy.py`
  - `backend/apps/runtime/services.py`
  - `backend/apps/company/tests/test_agent_test_security.py`

### Work Order Prompt

```text
请确保 `knowledgeRead / knowledgeDeny` 真正参与目录列举、文件定位、文件读取判定，而不只是配置项。

目标：
- list / locate / read 都必须受 effective policy 控制
- deny 优先级高于 read
- logical path 是唯一业务路径

实现要求：
1. 在现有 runtime policy 和 AgentWorkspaceAccessService 基础上继续收紧。
2. 目录授权语义：
   - 目录在 read 列表中，表示该目录递归可访问
   - 文件在 read 列表中，表示仅该文件可访问
3. deny 覆盖 read。
4. 未授权时返回统一、安全错误信息。
5. 不允许因为 runtime tree 中存在文件就绕过 knowledge policy。
6. 保持现有测试通过，并新增精确测试。

建议修改文件：
- backend/apps/runtime/policy.py
- backend/apps/runtime/services.py
- backend/apps/company/tests/test_agent_test_security.py

必须完成的代码化验收条件：
1. 授权 `knowledge/demo.txt` 后，只能读取 `knowledge/demo.txt`，不能读取 `knowledge/private.txt`。
2. 授权 `knowledge/reports` 后，可以列举和定位 `knowledge/reports/...` 下内容。
3. 若同时 `read=knowledge`、`deny=knowledge/private`，则 `knowledge/private` 下内容不可见。
4. 返回内容中不出现宿主机绝对路径。
5. 至少新增以下自动化测试：
   - file allow
   - directory allow
   - deny override
   - list 受控
   - locate 受控
   - read 受控
6. 测试全部通过，并输出测试命令与结果。
```

### Acceptance Checklist

- 权限粒度包含目录和文件。
- `deny` 覆盖 `allow`。
- list / locate / read 全部受控。
- 不返回宿主机绝对路径。

---

## Step 4: Tool Registry And Tool Permission UI

### Goal

把工具权限从自由文本升级成“工具注册表 + allow/deny 多选”，并让后端 policy 真正消费它。

### Scope

- Backend + Frontend
- 优先修改：
  - `backend/apps/runtime/tool_registry.py`（建议新建）
  - `backend/apps/company/views.py`
  - `backend/apps/company/urls.py`
  - `backend/apps/company/services.py`
  - `backend/apps/runtime/policy.py`
  - `touming-frontend/app/dashboard/companies/[id]/permissions/page.tsx`
  - `touming-frontend/lib/api.ts`

### Work Order Prompt

```text
请设计并实现 v1 工具权限系统，核心是“工具注册表 + allow/deny 选择器”，不要继续依赖自由文本随便填 tool id。

目标：
- B 端用户能看懂工具权限
- 后端能强制执行
- deny 高于 allow
- subagent 默认 deny-all

实现要求：
1. 新增平台侧 tool registry，建议为代码常量或独立模块，不必一开始做数据库表。
2. registry 中每个工具至少包含：
   - tool_id
   - name
   - group
   - risk_level
   - description
   - enabled_for_biz
3. v1 至少纳入：
   - workspace.list
   - workspace.locate
   - workspace.read
   - workspace.rename
   - workspace.delete
   - web.search
   - web.fetch
   - subagent.call
4. 高风险内部工具先不要暴露给普通 B 端页面。
5. 后端新增 catalog 接口给前端读取。
6. 前端权限页改成按组多选工具，而不是 Textarea。
7. policy 继续消费 allow/deny，但来源改成 registry 选择。

建议修改文件：
- backend/apps/runtime/tool_registry.py（新建）
- backend/apps/company/views.py
- backend/apps/company/urls.py
- backend/apps/company/services.py
- backend/apps/runtime/policy.py
- touming-frontend/app/dashboard/companies/[id]/permissions/page.tsx
- touming-frontend/lib/api.ts

必须完成的代码化验收条件：
1. 新增 catalog 接口，前端可获取工具分组和描述。
2. 模板页和 Agent 绑定页都可以多选工具 allow/deny。
3. 未在 allow 中的 subagent 无法执行对应工具。
4. deny 覆盖 allow。
5. 高风险内部工具不出现在普通权限页里。
6. 至少新增以下自动化测试：
   - registry 输出结构
   - allow 生效
   - deny 覆盖
   - subagent 默认 deny-all
7. 前端构建与后端测试全部通过。
```

### Acceptance Checklist

- 工具列表来自 registry，不再是自由文本。
- allow/deny 可视化可选。
- 后端 policy 实际生效。
- 高风险工具不对 B 端暴露。

---

## Step 5: Skills Runtime Audit

### Goal

先把 `skills` 的控制面和运行面关系查清楚，并做成一个可重复验证的 inspection 能力。不要先改 UI，不要先假设 OpenClaw 已经消费 `skills.json`。

### Scope

- Backend + Docs
- 优先修改：
  - `backend/apps/runtime/services.py`
  - `backend/apps/company/views.py` 或 `backend/apps/runtime/views.py`
  - `backend/apps/company/urls.py` 或 `backend/apps/runtime/urls.py`
  - 新增 `docs/runtime-skills-audit.md` 或同等文档

### Work Order Prompt

```text
请先不要改 skills UI，先做一次“OpenClaw runtime skills 消费链审计”，并把结论固化成代码和文档。

目标：
- 明确当前 runtime 到底是否真的消费 `skills.json`
- 明确 `TransparentCompanySkill + AgentSkillBinding` 到 runtime 的哪一层已经打通，哪一层还没打通
- 产出可重复验证的检查工具

实现要求：
1. 新增一个 admin-only 的“agent runtime exports inspection”能力，建议是接口或管理命令。
2. 对每个 agent 至少输出：
   - agent.json 摘要
   - skills.json 摘要
   - permissions.json 摘要
   - knowledge.json 摘要
3. 不直接输出敏感内容，只输出结构摘要和关键字段。
4. 新增一份文档，写清楚：
   - 平台 skill catalog
   - agent binding
   - runtime 导出
   - native OpenClaw 实际消费点
5. 如果当前仓库里无法证明 OpenClaw 真正消费 `skills.json`，必须明确写出“当前仅证明写出，不证明消费”。

建议修改文件：
- backend/apps/runtime/services.py
- backend/apps/company/views.py 或 backend/apps/runtime/views.py
- backend/apps/company/urls.py 或 backend/apps/runtime/urls.py
- docs/runtime-skills-audit.md（新建）

必须完成的代码化验收条件：
1. 开发者可通过接口或命令看到每个 agent 的 runtime export 摘要。
2. 返回中至少能看到：
   - agent_id
   - skills.json 的 items 数量
   - permissions.json 是否存在
   - knowledge.json 是否存在
3. 接口或命令必须经过 admin-only 控制，非 admin 不可访问。
4. 文档必须明确区分：
   - 平台管理模型
   - runtime 导出文件
   - OpenClaw 实际消费点
5. 如果消费点无法在当前仓库中证明，文档中必须明确写成 blocker。
6. 新增测试至少覆盖 inspection 能力的返回结构和鉴权。
```

### Acceptance Checklist

- 能检查 runtime exports。
- 文档明确说明“已证明/未证明”的边界。
- 不再凭猜测讨论 skills 生效链。

---

## Step 6: Skills Runtime Integration

### Goal

仅在 Step 5 已经确认 OpenClaw skills 实际消费点后，才把 skills 真正接入 runtime 生效链。

### Scope

- Backend / runtime
- 优先修改：
  - `backend/apps/runtime/services.py`
  - `scripts/runtime/shared_host_bridge.py`
  - `backend/apps/runtime/policy.py`
  - `backend/apps/company/tests/test_agent_test_security.py`

### Work Order Prompt

```text
基于 Step 5 的审计结论，实现 skills 的 runtime 真正生效链。不要猜测 OpenClaw 的消费方式，必须以已确认的消费点为准。

目标：
- 选中的 skill 真正进入 OpenClaw runtime 使用链
- disabled skill / disabled binding 不生效
- permission 里的 skillsInclude / skillsExclude 只做过滤器，不替代真实绑定

实现要求：
1. 如果已确认 OpenClaw 消费的是 native config 中的某字段，就把绑定结果发布到该字段。
2. 如果已确认 OpenClaw 消费的是 agentDir 下某个文件，就按它真实消费的格式 materialize。
3. 如果当前仓库无法证明消费点，请停止实现并明确 blocker，不要假装完成。
4. 实现后保留 inspection 能力，以便验证发布结果。
5. 只让 enabled 的 company skill + enabled 的 agent binding 进入 runtime。
6. `skillsInclude / skillsExclude` 继续作为 policy 过滤器参与收口。

建议修改文件：
- backend/apps/runtime/services.py
- scripts/runtime/shared_host_bridge.py
- backend/apps/runtime/policy.py
- backend/apps/company/tests/test_agent_test_security.py

必须完成的代码化验收条件：
1. 至少有一个 enabled skill 绑定到某 subagent 后，发布结果中能在“已确认的 runtime 消费点”看到该 skill。
2. disabled skill 不进入 runtime。
3. disabled binding 不进入 runtime。
4. `skillsInclude / skillsExclude` 会影响最终进入 runtime 的 skills 集合。
5. 如果消费点可证明，必须有自动化测试验证“绑定 skill 后 runtime 产物发生变化”。
6. 如果消费点不可证明，本步骤不得标记为完成，输出中必须明确 blocker。
```

### Acceptance Checklist

- 只有在消费点明确后才允许实施。
- 运行时产物变化可观测。
- disabled skill / binding 必须失效。
- 无法证明消费点时，必须显式阻断。

---

## Suggested Execution Order

1. Step 1: Backend Knowledge Workspace Tree API
2. Step 2: Frontend Knowledge Path Picker
3. Step 3: Enforce Knowledge Read / Deny At Runtime
4. Step 4: Tool Registry And Tool Permission UI
5. Step 5: Skills Runtime Audit
6. Step 6: Skills Runtime Integration

## Notes For Codex / Cursor Operators

- 每次只执行一个 Step。
- 不要把多个 Step 混在一轮修改里。
- 如果某一步涉及前后端联动，先保证后端接口和测试落地，再做前端。
- Step 5 之前，不要把 skills 当成“已经真实 runtime 生效”的功能对外承诺。
