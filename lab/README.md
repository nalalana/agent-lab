# Agent 架构研究与评测

基线：`https://github.com/kuibu/touming.git` 的 `develop`，2026-10-20 获取，提交
`e67c05a81d753d0d06c43c96872d28854e1af60a`。保留当前后端代码与业务依赖，实验主分支为 `main`。
上游历史含密钥，被 GitHub 推送保护拒绝；独立仓库使用不继承历史的代码快照，并排除本地 `backend/properties.py` 配置。
`origin` 为 [nalalana/agent-lab](https://github.com/nalalana/agent-lab)；`upstream` 仅用于同步原后端，不向 upstream 推送实验。

## 当前能运行的内容

从仓库根目录执行（使用已安装后端依赖的 Python）：

```powershell
python lab/run_baseline.py --repeat 3
python lab/run_baseline.py --suite apps.agent_runtime.tests.test_response_verification
```

入口直接测试生产执行图、工具校验、权限、响应核验，不启动 Web、队列、数据库服务器或沙箱服务。
只用最小 Django 设置和现有测试依赖；新环境先在独立虚拟环境安装 `backend/requirements.txt`。
结果写入 `lab/results/baseline.json`，包括源码版本、工作区状态、逐条用时、失败详情。
这属于脚本化模型的程序回归，**不能作为真实模型的幻觉率或公开基准成绩**。

## 两条工作线

1. [论文与业界实践](research.md)：记录来源、阅读深度、可检验假设、实验和结论。先留基线，再一次改变一个变量。
2. [数据集与批量测试](datasets.md)：原始数据版本锁定，优先原生评测器，业务测试独立报告。

## 后续实现顺序

1. 运行现有离线基线，记录失败，不预先更改生产语义。
2. 接入 `Phase2AgentRuntimeFacade` 的真实模型适配器和固定工具环境；显式提供模型配置、上下文、工具 schema、权限、classifier/verifier，缺失配置直接失败，禁止退回 fake gateway。
3. 用 tau2 原生环境/评分器对接同一执行图。不能把 tau2 数据文件直接当成普通问答。
4. 补透明公司案例：无证据回答、空查询、失败工具、跨公司权限、错误成功声明、多轮更正；验证数据库最终状态。
5. 数据库集成使用独立测试实例与生产同类引擎，每例重置数据。离线 SQLite 设置只用于无需数据库的回归，不用于证明生产 SQL 正确。
6. 比较 baseline 与单变量改动：成功率、无依据事实率、虚假完成率、权限违规、p50/p95 时延、token 和费用。固定任务、模型、预算和种子；随机性案例重复运行，报告分母与不确定性。

保留开发集与冻结保留集；保留集不得参与提示词调整或训练。业务权限违规和虚假完成单列，不能被平均总分掩盖。
尚未执行真实模型评测，也没有证据表明任何候选架构优于当前架构。
