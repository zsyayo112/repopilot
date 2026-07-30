"""兜底 adapter：没有任何语言标志，但 Makefile 里有 test 目标。

它必须排在探测链的【最后】—— 很多 Python/Go 项目也带 Makefile，但那时候
我们更想用语言专属的知识（能解析输出、能起服务），而不是笼统的 make test。
"""

from __future__ import annotations

import re
from pathlib import Path

from .base import RepoAdapter, ValidationStep


class MakeAdapter(RepoAdapter):
    kind = "make"
    framework = "make"

    def __init__(self, root: Path, notes: list[str] | None = None,
                 targets: set[str] | None = None):
        super().__init__(root, notes)
        self.targets = targets or set()

    @classmethod
    def detect(cls, root: Path) -> RepoAdapter | None:
        mk = root / "Makefile"
        if not mk.exists():
            return None
        try:
            text = mk.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return None
        targets = set(re.findall(r"(?m)^([A-Za-z][\w./-]*):", text))
        if "test" not in targets:
            return None
        return cls(root, ["发现 Makefile 里的 test 目标"], targets)

    def test_command(self) -> str | None:
        return "make test"

    def validation_steps(self) -> list[ValidationStep]:
        """Makefile 项目的验证流水线 = 它自己声明了哪些常见目标。
        不猜、不硬编码 —— 只用 Makefile 里真实存在的目标。"""
        steps = []
        for name in ("lint", "fmt", "typecheck", "check"):
            if name in self.targets:
                steps.append(ValidationStep(name, f"make {name}", required=False))
        steps.append(ValidationStep("test", "make test"))
        if "build" in self.targets:
            steps.append(ValidationStep("build", "make build", required=False))
        return steps
