"""Ruby 技术栈。Ruby 的测试命令在各项目之间差异极大（rspec / minitest / rake
自定义任务），所以这个 adapter 老实承认"我给的是最可能的猜测"，并在 notes 里
提示用 --test-cmd 覆盖。**说不准比猜错更有价值。**
"""

from __future__ import annotations

import re
from pathlib import Path

from .base import (
    EnvRequirement,
    RepoAdapter,
    ServiceSpec,
    TestReport,
    tail,
)


class RubyAdapter(RepoAdapter):
    kind = "ruby"
    framework = "rspec"

    def __init__(self, root: Path, notes: list[str] | None = None, cmd: str = "bundle exec rspec"):
        super().__init__(root, notes)
        self.cmd = cmd

    @classmethod
    def detect(cls, root: Path) -> RepoAdapter | None:
        hint = "Ruby 测试命令差异大，必要时用 --test-cmd 覆盖"
        if (root / ".rspec").exists() or (root / "spec").is_dir():
            return cls(root, [f"发现 .rspec/spec，{hint}"], "bundle exec rspec")
        if (root / "Gemfile").exists():
            return cls(root, [f"发现 Gemfile，{hint}"], "bundle exec rake test")
        return None

    def test_command(self) -> str | None:
        return self.cmd

    def parse_test_output(self, output: str, exit_code: int) -> TestReport:
        """rspec：`12 examples, 2 failures, 1 pending`
        minitest：`12 runs, 30 assertions, 2 failures, 0 errors, 1 skips`"""
        m = re.search(r"(?P<total>\d+) examples?, (?P<failed>\d+) failures?"
                      r"(?:, (?P<pending>\d+) pending)?", output)
        if m:
            total, failed = int(m.group("total")), int(m.group("failed"))
            pending = int(m.group("pending") or 0)
            names = re.findall(r"^rspec\s+(\S+)", output, re.M)
            return TestReport(exit_code=exit_code, passed=max(total - failed - pending, 0),
                              failed=failed, skipped=pending, failed_names=names,
                              tail=tail(output), parsed=True, framework="rspec")
        m = re.search(r"(?P<runs>\d+) runs?, \d+ assertions?, (?P<failed>\d+) failures?,"
                      r" (?P<errors>\d+) errors?, (?P<skips>\d+) skips?", output)
        if m:
            runs, failed = int(m.group("runs")), int(m.group("failed"))
            errors, skips = int(m.group("errors")), int(m.group("skips"))
            return TestReport(exit_code=exit_code,
                              passed=max(runs - failed - errors - skips, 0),
                              failed=failed, errors=errors, skipped=skips,
                              tail=tail(output), parsed=True, framework="minitest")
        return super().parse_test_output(output, exit_code)

    def services(self) -> list[ServiceSpec]:
        if (self.root / "config.ru").exists() or (self.root / "bin" / "rails").exists():
            return [ServiceSpec(name="web", command="bundle exec rails server -p 3000",
                                port=3000, ready_pattern=r"Listening on")]
        return []

    def env_requirements(self) -> list[EnvRequirement]:
        return [EnvRequirement("ruby", ["ruby", "--version"], "安装 Ruby 3.x"),
                EnvRequirement("bundler", ["bundle", "--version"], "gem install bundler")]
