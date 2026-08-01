"""Verifier：基线对比的判据，以及一个被本轮修掉的真 bug。

判据是整个项目的灵魂，所以每条出边都要有测试守着 —— 判错了，agent 会
"修好了一个它其实搞坏了的东西"，而且理直气壮。
"""

import subprocess

import pytest

from repopilot.adapters import detect
from repopilot.adapters.base import TestReport
from repopilot.verifier import compare, run_validation
from repopilot.workspace import Workspace


def rep(exit_code, passed=0, failed=0, names=(), parsed=True):
    return TestReport(exit_code=exit_code, passed=passed, failed=failed,
                      failed_names=list(names), parsed=parsed)


def test_fixed():
    assert compare(rep(1, failed=1, names=["t::a"]), rep(0, passed=1))["status"] == "fixed"


def test_still_green():
    assert compare(rep(0, passed=3), rep(0, passed=3))["status"] == "still_green"


def test_regressed_by_new_failure_name():
    out = compare(rep(1, failed=1, names=["t::a"]), rep(1, failed=1, names=["t::b"]))
    assert out["status"] == "regressed"
    assert out["new_failures"] == ["t::b"]


def test_improved():
    out = compare(rep(1, failed=3, names=["a", "b", "c"]), rep(1, failed=1, names=["a"]))
    assert out["status"] == "improved"
    assert out["resolved_failures"] == ["b", "c"]


def test_red_baseline_same_failures_is_no_regression():
    """红基线 + 失败集合原封不动 = no_regression（与 still_green 同级的通过态）。

    官方镜像的环境里基线可能天生带红（实测 flask-5014 容器里 8 个测试因
    DNS 解析失败永远是红的）。那些红不是这次改动造成的 —— 判成 no_change
    会让一份正确的补丁被 NO_PROGRESS 误杀（v0.6 容器化冒烟实测踩到）。
    """
    assert compare(rep(1, failed=1, names=["a"]), rep(1, failed=1, names=["a"]))["status"] \
        == "no_regression"


def test_red_baseline_without_parsed_names_is_still_no_change():
    """解析不出名单（parsed=False）时不许升格成 no_regression ——
    没有失败名单就无法证明"红的还是原来那批红的"，不确定必须显式。"""
    assert compare(rep(1, failed=1, parsed=False), rep(1, failed=1, parsed=False))["status"] \
        == "no_change"


# --------------------------------------------------------------------------
# 本轮修掉的 bug
# --------------------------------------------------------------------------
def test_green_to_red_is_a_regression_even_without_parsed_names():
    """【回归测试：旧版把这个判成 no_change】

    失败用例名单只有 pytest 解析得出来。对 Node/Go 项目，基线全绿、改完全红时：
      new_failures 为空 → 不是 regressed
      base.exit_code == 0 → 不是 fixed
      after.failed(0) < base.failed(0) 不成立 → 不是 improved
    于是落到 no_change ——「把测试搞挂了」被报成「这轮白干」。
    """
    out = compare(rep(0, parsed=False), rep(1, parsed=False))
    assert out["status"] == "regressed"


def test_unparsed_reports_are_marked_low_confidence():
    """看不懂就说看不懂：verifier 必须知道自己什么时候是瞎的。"""
    assert compare(rep(0, parsed=False), rep(0, parsed=False))["confidence"] == "low"
    assert compare(rep(0), rep(0))["confidence"] == "high"


def test_improved_requires_trustworthy_numbers():
    """数字没解析出来时不许下 improved 结论 —— 那个结论完全建立在假数字上。"""
    out = compare(rep(1, failed=5, parsed=False), rep(1, failed=0, parsed=False))
    assert out["status"] == "no_change"


# --------------------------------------------------------------------------
# 验证流水线
# --------------------------------------------------------------------------
@pytest.fixture
def git_repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "a.py").write_text("x = 1\n")
    return tmp_path


def test_validation_runs_steps_in_order_and_stops_at_first_failure(git_repo):
    ws = Workspace(git_repo)
    profile = detect(git_repo, test_cmd_override="true")
    # 手工塞一条会失败的 lint，验证 fail-fast：test 不该被执行
    from repopilot.adapters.base import ValidationStep
    profile.adapter.validation_steps = lambda: [
        ValidationStep("lint", "false"),
        ValidationStep("test", "true"),
    ]
    report = run_validation(ws, profile)
    assert report.ok is False
    assert [s.name for s in report.steps] == ["lint"]   # 早失败早省钱
    assert report.failed_step.name == "lint"


def test_validation_skips_optional_missing_tools(git_repo):
    """项目没装 mypy 时，"mypy 不存在"不该被当成"类型检查失败"。"""
    ws = Workspace(git_repo)
    profile = detect(git_repo, test_cmd_override="true")
    from repopilot.adapters.base import ValidationStep
    profile.adapter.validation_steps = lambda: [
        ValidationStep("typecheck", "definitely-not-a-real-binary-xyz", required=False),
        ValidationStep("test", "true"),
    ]
    report = run_validation(ws, profile)
    assert report.ok is True
    assert report.steps[0].skipped is True
    assert [s.name for s in report.steps] == ["typecheck", "test"]


def test_test_cmd_override_keeps_the_detected_parser(tmp_path):
    """人只是换了跑测试的方式，不是换了一门语言 —— 解析能力必须留着。"""
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    profile = detect(tmp_path, test_cmd_override="make test")
    assert profile.kind == "custom"
    assert profile.test_cmd == "make test"
    report = profile.parse_test_output("===== 3 passed in 0.1s =====", 0)
    assert report.parsed is True and report.passed == 3   # 还认得 pytest 输出


def test_core_never_branches_on_stack_kind(tmp_path):
    """把"核心不认识任何技术栈"这条保证钉成测试，而不是一句口号。

    核心一旦开始拿 kind 做字符串比较，Adapter 这层抽象就出现了第一道裂缝 ——
    而且是那种会被后人模仿的裂缝。所以用 grep 守住它。
    """
    import re
    from pathlib import Path

    core = ["orchestrator.py", "executor.py", "reviewer.py", "planner.py",
            "verifier.py", "permissions.py"]
    root = Path(__file__).resolve().parents[1] / "repopilot"
    offenders = []
    for name in core:
        for lineno, line in enumerate(
                (root / name).read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r'\bkind\s*[=!]=\s*["\']', line):
                offenders.append(f"{name}:{lineno}: {line.strip()}")
    assert not offenders, (
        "核心里出现了对技术栈的字符串分支，Adapter 抽象被破坏了：\n"
        + "\n".join(offenders))


def test_command_overridden_is_a_property_not_a_string_compare(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    assert detect(tmp_path).command_overridden is False
    assert detect(tmp_path, test_cmd_override="make test").command_overridden is True
