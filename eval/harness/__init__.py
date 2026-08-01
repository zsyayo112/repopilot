"""RepoPilot 评测线束。

    推理侧                      判分侧
    instances.PublicInstance    instances.SecretInstance
    environment                 grader
    inference                   failures
    baselines                   report
    manifest（两侧共用）         dataset（两侧共用，两个入口分开）

约束：inference.py / baselines.py / environment.py 不得 import grader、
不得 import SecretInstance、不得读 FAIL_TO_PASS / PASS_TO_PASS / patch。
这条约束由 tests/test_eval_isolation.py 做 AST 静态检查。
"""

from .manifest import HARNESS_VERSION

__all__ = ["HARNESS_VERSION"]
