> **⚠️ 已作废，仅作历史记录保留。**
>
> 这份结果由 v1 评测脚本（`eval/swebench_eval.py`，已删除）产出，它有一个
> **已确认的信息泄露**：agent 的测试命令是从官方 `test_patch` 里抠出的测试
> 文件路径拼出来的，等于把"官方隐藏测试藏在哪个文件"直接告诉了 agent。
> 因此下面的 resolved 数字**不可用于任何对外表述**。
>
> 另外两个问题：实例是从 Lite 里按"看起来能跑"人工挑的（选择偏差），
> 且没有冻结的 dev/test 划分（在同一批 8 条上反复调参 = 人工过拟合）。
>
> 替代方案见 `eval/harness/`：推理与判分进程级隔离、SWE-bench Verified
> 事前冻结划分、不可变 manifest、统一预算、成对消融。

# SWE-bench Lite 迷你评测结果

- 尝试：8  |  gold 校准可判定：3  |  环境伪影（gold 也过不了）：5  |  环境闸门跳过：2
- **可判定实例上 resolved = 3/3 = 100%**；保守口径（未判定全算失败）= 3/8

| 实例 | 结果 | 工具调用 | 上下文峰值 | 耗时(s) | 备注 |
|---|---|---|---|---|---|
| mwaskom__seaborn-3010 | ✅ resolved | 8 | 5017 | 28 |  |
| mwaskom__seaborn-3190 | ⚠ env_artifact | 54 | 24649 | 124 | tests/_core/test_scales.py::TestContinuous::test_tick_minor→ |
| mwaskom__seaborn-3407 | ⚠ env_artifact | 70 | 42608 | 531 | tests/test_axisgrid.py::TestPairGrid::test_pairplot_column_m |
| pallets__flask-4992 | ⚠ env_artifact | 110 | 13791 | 595 | tests/test_config.py::test_config_from_file_toml→FAILED | te |
| pallets__flask-5063 | ⚠ env_artifact | 74 | 34116 | 901 | tests/test_cli.py::TestRoutes::test_subdomain→FAILED | tests |
| pylint-dev__pylint-7228 | ⏭ env_skip | 0 | 0 |  | pip install:  File "/tmp/pip-build-env-_kyxxxm1/overlay/lib/ |
| pylint-dev__pylint-7993 | ⏭ env_skip | 0 | 0 |  | pip install:  File "/tmp/pip-build-env-5jqjzwra/overlay/lib/ |
| pytest-dev__pytest-11143 | ✅ resolved | 15 | 7027 | 56 |  |
| pytest-dev__pytest-11148 | ⚠ env_artifact | 120 | 0 | 378 | testing/acceptance_test.py::TestInvocationVariants::test_cmd |
| sphinx-doc__sphinx-11445 | ✅ resolved | 25 | 12617 | 64 |  |
