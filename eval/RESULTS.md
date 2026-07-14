# SWE-bench Lite 迷你评测结果

- 尝试：8  |  resolved：2  |  环境闸门跳过：2
- **resolved 率 = 2/8 = 25%**

| 实例 | 结果 | 工具调用 | 上下文峰值 | 耗时(s) | 备注 |
|---|---|---|---|---|---|
| mwaskom__seaborn-3010 | ✅ resolved | 8 | 5017 | 28 |  |
| mwaskom__seaborn-3190 | ❌ scored | 54 | 24649 | 124 | 75, 1.  ])
E        DESIRED: array([0.25, 0.5 , 0.75, 1.  ]) |
| mwaskom__seaborn-3407 | ❌ scored | 70 | 42608 | 531 | rror: No such keys(s): 'mode.use_inf_as_na'

../venv/lib/pyt |
| pallets__flask-4992 | ❌ scored | 110 | 13791 | 595 | precated and will be removed in Python 3.14; use value inste |
| pallets__flask-5063 | ❌ scored | 74 | 34116 | 901 | 
tests/test_cli.py:504: AssertionError
===================== |
| pylint-dev__pylint-7228 | ⏭ env_skip | 0 | 0 |  | pip install:  File "/tmp/pip-build-env-_kyxxxm1/overlay/lib/ |
| pylint-dev__pylint-7993 | ⏭ env_skip | 0 | 0 |  | pip install:  File "/tmp/pip-build-env-5jqjzwra/overlay/lib/ |
| pytest-dev__pytest-11143 | ❌ scored | 15 | 7027 | 56 | al/work/pytest-dev__pytest-11143/repo/testing/test_assertrew |
| pytest-dev__pytest-11148 | ❌ scored | 120 | 0 | 378 | -------------------
ERROR: module or package not found: ns_p |
| sphinx-doc__sphinx-11445 | ✅ resolved | 25 | 12617 | 64 |  |
