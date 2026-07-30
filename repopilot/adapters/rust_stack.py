"""Rust 技术栈。cargo 的输出格式很规整，文本解析足够可靠。"""

from __future__ import annotations

import re
from pathlib import Path

from .base import (
    EnvRequirement,
    RepoAdapter,
    TestReport,
    ValidationStep,
    tail,
)

# cargo 每个 target 都会输出一行：
#   test result: FAILED. 12 passed; 2 failed; 0 ignored; 0 measured; 0 filtered out
_RESULT = re.compile(
    r"test result:\s*(?:ok|FAILED)\.\s*(?P<passed>\d+) passed;\s*(?P<failed>\d+) failed;"
    r"\s*(?P<ignored>\d+) ignored", re.I)


class RustAdapter(RepoAdapter):
    kind = "rust"
    framework = "cargo test"

    @classmethod
    def detect(cls, root: Path) -> RepoAdapter | None:
        if (root / "Cargo.toml").exists():
            return cls(root, ["发现 Cargo.toml"])
        return None

    def test_command(self) -> str | None:
        return "cargo test"

    def parse_test_output(self, output: str, exit_code: int) -> TestReport:
        """注意要把【所有】target 的 test result 行累加：一个 workspace 会输出多行
        （lib、每个 bin、每个 integration test 各一行），只取第一行会严重少算。"""
        rows = list(_RESULT.finditer(output))
        passed = sum(int(m.group("passed")) for m in rows)
        failed = sum(int(m.group("failed")) for m in rows)
        skipped = sum(int(m.group("ignored")) for m in rows)

        # 失败名单在 `failures:` 段落里，每行一个缩进的用例路径
        names: list[str] = []
        for block in re.findall(r"^failures:\n((?:\s{4}\S+\n)+)", output, re.M):
            names += [line.strip() for line in block.splitlines() if line.strip()]
        # 编译失败：cargo 会输出 error[E0xxx]，此时一个用例都没跑
        compile_error = bool(re.search(r"^error(?:\[E\d+\])?:", output, re.M))

        return TestReport(
            exit_code=exit_code, passed=passed, failed=failed,
            errors=1 if compile_error and not rows else 0, skipped=skipped,
            failed_names=sorted(set(names)), tail=tail(output),
            parsed=bool(rows) or compile_error, framework="cargo test",
        )

    def validation_steps(self) -> list[ValidationStep]:
        return [
            ValidationStep("fmt", "cargo fmt --check", required=False),
            ValidationStep("clippy", "cargo clippy -- -D warnings", required=False),
            ValidationStep("test", "cargo test"),
        ]

    def env_requirements(self) -> list[EnvRequirement]:
        return [EnvRequirement("cargo", ["cargo", "--version"],
                               "安装 Rust 工具链（https://rustup.rs）")]
