# 首轮基线记录

2026-10-20，后端 develop `e67c05a81d753d0d06c43c96872d28854e1af60a`。

初次运行 78 项：77 通过，1 失败。失败为
`Phase2ToolValidationTests.test_default_registry_contains_only_platform_tools`。
最新 registry 已加入四个 company 项目表格工具，测试中两处枚举仍只列旧工具。
本实验仓库补齐了精确预期；未改生产工具列表或权限逻辑。

运行命令：`python lab/run_baseline.py --repeat 3`。
修正过期测试后，三轮均为 78/78 通过（共 234 次）；每轮约 3.3–3.6 秒。
机器可读报告在忽略提交的 `lab/results/` 下。离线回归结果不是模型能力指标。
