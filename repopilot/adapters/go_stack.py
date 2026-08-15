"""Go 技术栈。

亮点：`go test -json` 是一等公民。解析器【同时】认文本和 JSON —— 也就是说
用 `--test-cmd "go test -json ./..."` 跑，解析立刻从"近似"变成"精确"，
一行代码都不用改。这就是"解析器和命令解耦"的具体好处。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .base import (
    EnvRequirement,
    RepoAdapter,
    TestReport,
    ValidationStep,
    tail,
)


class GoAdapter(RepoAdapter):
    kind = "go"
    framework = "go test"

    @classmethod
    def detect(cls, root: Path) -> RepoAdapter | None:
        if (root / "go.mod").exists():
            return cls(root, ["发现 go.mod"])
        return None

    def test_command(self) -> str | None:
        # -json 让解析从"近似"变成"精确"：每条用例一个事件，失败名单和
        # 计数都是真的 —— 失败指纹（停机策略）和 improved/no_regression
        # 这些细粒度判定全靠它。testify#1785 实测：纯文本模式下全绿输出
        # 没有任何数字可抠，Verifier 退化成 exit-code-only。
        # 输出量大不是问题：模型侧有落盘句柄，解析侧本来就要全文。
        return "go test -json ./..."

    def parse_test_output(self, output: str, exit_code: int) -> TestReport:
        if report := _parse_go_json(output, exit_code):
            return report
        return _parse_go_text(output, exit_code)

    def validation_steps(self) -> list[ValidationStep]:
        return [
            # gofmt -l 列出没格式化的文件；有输出就算不合格（它自己 exit 0，所以配 test -z）
            ValidationStep("fmt", 'test -z "$(gofmt -l .)"', required=False),
            ValidationStep("vet", "go vet ./...", required=False),
            ValidationStep("test", "go test ./..."),
            ValidationStep("build", "go build ./...", required=False),
        ]

    def env_requirements(self) -> list[EnvRequirement]:
        return [EnvRequirement("go", ["go", "version"], "安装 Go 1.21+")]


def _parse_go_json(output: str, exit_code: int) -> TestReport | None:
    """`go test -json` 每行一个 JSON 事件，Action 字段是 run/pass/fail/skip。"""
    events = []
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith('{"'):
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if not events:
        return None

    passed = failed = skipped = 0
    names: list[str] = []
    for ev in events:
        # 只统计【单个用例】的事件：没有 Test 字段的是包级汇总，算进去会重复计数
        if not ev.get("Test"):
            continue
        action = ev.get("Action")
        if action == "pass":
            passed += 1
        elif action == "fail":
            failed += 1
            names.append(f"{ev.get('Package', '?')}.{ev['Test']}")
        elif action == "skip":
            skipped += 1

    return TestReport(
        exit_code=exit_code, passed=passed, failed=failed, skipped=skipped,
        failed_names=names, tail=tail(output), parsed=True, framework="go test(json)",
    )


def _parse_go_text(output: str, exit_code: int) -> TestReport:
    """文本模式：`--- FAIL: TestLogin (0.00s)` / `--- PASS: ...`。

    只数顶层用例（`--- FAIL:` 前面没有缩进）；子测试是缩进的
    `    --- FAIL: TestX/case_a`，把它们也当顶层数会导致父子重复计数。
    """
    passed = len(re.findall(r"^--- PASS:", output, re.M))
    skipped = len(re.findall(r"^--- SKIP:", output, re.M))
    names = re.findall(r"^--- FAIL:\s+(\S+)", output, re.M)
    # 编译失败：一个用例都没跑，但 exit != 0。必须能识别，否则看起来像"0 失败"。
    build_failed = bool(re.search(r"^(?:# |.*\[build failed\])", output, re.M))
    # 全绿的非 -v 输出没有任何 --- PASS 行，只有包级的 `ok  pkg  0.5s`。
    # 以前这形状 parsed=False，"全绿"和"没看懂"混为一谈（testify#1785 实测）。
    # 包数不是用例数，但 exit=0 + 全是 ok 行足以断言"0 失败"—— 这正是
    # 基线对比需要的那个事实。粒度以包计，summary 里如实标注。
    ok_pkgs = len(re.findall(r"^ok\s+\S+", output, re.M))
    if not (passed or names or skipped or build_failed) and exit_code == 0 and ok_pkgs:
        return TestReport(
            exit_code=exit_code, passed=ok_pkgs, tail=tail(output),
            parsed=True, framework="go test(pkg)",
        )
    return TestReport(
        exit_code=exit_code, passed=passed, failed=len(names),
        errors=1 if build_failed else 0, skipped=skipped,
        failed_names=names, tail=tail(output),
        parsed=bool(passed or names or skipped or build_failed),
        framework="go test",
    )
