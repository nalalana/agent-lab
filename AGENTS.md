# Agent Lab

本仓库用于透明公司 Agent 架构实验和批量评测；入口说明在 lab/README.md。
复用 backend/apps/agent_runtime 的执行图、工具校验与权限逻辑，避免另写一套循环作为生产架构的替代评测。
原后端 develop 是 upstream；没有明确指示不要向 upstream 推送实验。
改架构前运行 python lab/run_baseline.py 并保留报告。fake 模型测试不得称为真实模型评测。
论文记录来源和阅读深度；未经评测的优化只能称为假设。
数据、密钥、业务快照和运行轨迹不提交。真实模型必须显式配置，禁止缺配置时静默退回假模型。
