"""Repository Adapter 包：整个代码库里【唯一允许认识具体技术栈】的地方。

"核心与技术栈无关"落到实处就是这个包。核心只会问 adapter 这几个问题：

    这是什么项目？            → kind
    用什么命令跑测试？        → test_cmd
    这段输出什么意思？        → parse_test_output()   ← 新增
    还要跑哪些验证？          → validation_steps()    ← 新增
    怎么把应用跑起来？        → services()            ← 新增
    环境齐了吗？              → env_requirements()    ← 新增
    这个文件里有什么符号？    → symbols()             ← 新增（多语言）

核心把 kind 只【记录】不【分叉】，把命令当【不透明字符串】照着跑 ——
所以支持一门新语言 = 加一个 *_stack.py + 在 registry.ADAPTERS 里登记一行，
**核心一行不用改**。

文件分工：
    base.py         契约（RepoAdapter）+ 统一名词（TestReport / ServiceSpec / …）
    registry.py     探测登记处 + RepoProfile + monorepo 扫描
    symbols.py      多语言符号提取（Python 真 AST，其余正则近似）
    *_stack.py      每种技术栈的全部知识，一个文件一门语言

支持矩阵（深度不一，探测都是零成本的标志文件检查）：
    python / node / nestjs / nextjs / nuxt / angular / vite / go / rust /
    java-maven / java-gradle / ruby / make(兜底)
探测不准或不支持时，永远可以用 `--test-cmd` 强制指定，一票覆盖所有探测。
"""

from .base import (
    CustomAdapter,
    EnvRequirement,
    RepoAdapter,
    ServiceSpec,
    Symbol,
    TestReport,
    ValidationStep,
)
from .python_stack import PYTEST_CMD
from .registry import (
    ADAPTERS,
    ProjectUnit,
    RepoProfile,
    RepositoryProfile,
    detect,
    detect_one,
    scan,
)
from .symbols import LANG_BY_SUFFIX, language_of

__all__ = [
    "ADAPTERS", "CustomAdapter", "EnvRequirement", "LANG_BY_SUFFIX", "PYTEST_CMD",
    "ProjectUnit", "RepoAdapter", "RepoProfile", "RepositoryProfile", "ServiceSpec",
    "Symbol", "TestReport", "ValidationStep", "detect", "detect_one", "language_of",
    "scan",
]
