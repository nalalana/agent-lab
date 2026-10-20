# Touming Agent Lab

透明公司 Agent 架构研究与批量评测仓库，基于后端最新 develop（2026-10-20）。

从 [Agent Lab 使用说明](lab/README.md) 开始。包含论文与业界实践初筛、数据集版本锁定，以及无需启动平台服务的生产执行图回归入口。

```powershell
python lab/run_baseline.py --repeat 3
```

完整后端暂保留以复用业务工具与权限，未完成独立内核抽取。上游远程名为 `upstream`；独立仓库为 [nalalana/agent-lab](https://github.com/nalalana/agent-lab)，开发主分支为 `main`。

以下为继承的上游说明。

## 上游原说明：Vue 3 + TypeScript + Vite

This template should help get you started developing with Vue 3 and TypeScript in Vite. The template uses Vue 3 `<script setup>` SFCs, check out the [script setup docs](https://v3.vuejs.org/api/sfc-script-setup.html#sfc-script-setup) to learn more.

Learn more about the recommended Project Setup and IDE Support in the [Vue Docs TypeScript Guide](https://vuejs.org/guide/typescript/overview.html#project-setup).


## 内测邀请码和用户反馈

部署后先执行 `python backend/manage.py migrate`。
内部生成邀请码：`python backend/manage.py generate_company_invites --count 10 --days 30 --uses 1`。
默认每码可创建一家企业，30 天有效；明文仅生成时输出，数据库保存 SHA-256 摘要。可将 remaining_uses 设为 0 撤销。
公司注册请求必填 invitation_code，创建失败不消耗；已有企业的后续认证不受影响。
反馈需登录，最多 5 张图片、每张 5 MB，支持 JPG/PNG/WebP，最多 2000 万像素。
使用已有 COS 配置保存私有对象，图片通过管理员授权接口读取；部署代理需允许至少 26 MB 的 multipart 请求。
GET /api/user/feedback 为 admin/root 专用分页列表；POST 同路径提交问卷；GET /api/user/feedback/{id}/images/{index} 为管理员图片接口。
验证：`python backend/apps/user/tests/check_beta_feedback.py`，使用独立内存 SQLite 和模拟 COS，不连接业务数据库。
