"""停机策略与最佳补丁：让 agent 知道什么时候该停下来。

这些规则是被评测数据逼出来的 —— 失败实例里出现过 120 次工具调用、901 秒，
全程在同一个错误方向上反复微调。模型不会自己承认方向错了，它总能在当前
修改上再想出一个小调整。所以停机必须是外部的、只看可观测量的。
"""

from repopilot.adapters.base import TestFailure, TestReport
from repopilot.budget import Budget, Ledger
from repopilot.halting import HaltingPolicy


def _report(failed=0, names=(), exit_code=None):
    return TestReport(
        exit_code=exit_code if exit_code is not None else (1 if failed else 0),
        failed=failed, parsed=True, failed_names=list(names),
        failures=[TestFailure(test=n, exception="AssertionError") for n in names])


def _cmp(status, after=""):
    return {"status": status, "new_failures": [], "confidence": "high",
            "baseline": "b", "after": after}


def _policy(**budget_kw):
    return HaltingPolicy(budget=Budget(**budget_kw))


def test_signature_ignores_the_noise_that_changes_every_run():
    """失败指纹只取用例 id + 异常类型。

    含耗时、临时路径的整段输出永远不相等，"连续两次一模一样"那条规则
    就永远不会触发 —— 那正是它必须存在的理由。
    """
    a = _report(2, ["t::x", "t::y"])
    b = _report(2, ["t::x", "t::y"])
    a.tail = "3.21s /tmp/pytest-of-syz/pytest-0"
    b.tail = "9.87s /tmp/pytest-of-syz/pytest-42"
    assert a.signature() == b.signature()
    assert a.signature() != _report(2, ["t::x", "t::z"]).signature()


def test_two_identical_failing_rounds_stop_the_run():
    p = _policy()
    led = Ledger(budget=p.budget)
    p.record(1, _cmp("no_change"), _report(2, ["a", "b"]), "diff1", ["src/a.py"])
    assert p.decide(led, ["src/a.py"]).action == "continue"

    p.record(2, _cmp("no_change"), _report(2, ["a", "b"]), "diff2", ["src/a.py"])
    d = p.decide(led, ["src/a.py"])
    assert d.action == "stop" and d.code == "SAME_FAILURE_TWICE"


def test_green_twice_is_not_no_progress():
    """测试连续两轮都绿 → 指纹当然一样。那不是卡住了，那是修好了。

    少了这道判断，Reviewer 驳回后的第二轮会被"连续两次失败相同"误杀。
    """
    p = _policy()
    led = Ledger(budget=p.budget)
    for i in (1, 2, 3):
        p.record(i, _cmp("still_green"), _report(0), f"d{i}", ["src/a.py"])
        assert p.decide(led, ["src/a.py"]).action == "continue"


def test_a_worse_round_rolls_back_to_the_best_checkpoint():
    """补丁 A 失败 3 → 补丁 B 失败 1 → 补丁 C 失败 5：best 必须是 B。"""
    p = _policy()
    led = Ledger(budget=p.budget)
    p.record(1, _cmp("no_change"), _report(3, ["a", "b", "c"]), "A", ["s.py"])
    p.record(2, _cmp("improved"), _report(1, ["a"]), "B", ["s.py"])
    p.record(3, _cmp("no_change"), _report(5, ["a", "b", "c", "d", "e"]), "C", ["s.py"])

    d = p.decide(led, ["s.py"])
    assert d.action == "rollback" and d.code == "REGRESSION_IN_ROUND"
    assert p.best.attempt == 2 and p.best.diff == "B"


def test_growing_diff_without_fewer_failures_is_negative_yield():
    p = _policy()
    led = Ledger(budget=p.budget)
    p.record(1, _cmp("no_change"), _report(2, ["a", "b"]), "x" * 100, ["s.py"])
    p.record(2, _cmp("no_change"), _report(2, ["a", "c"]), "x" * 500, ["s.py"])
    d = p.decide(led, ["s.py"])
    assert d.action == "rollback" and d.code == "DIFF_GROWING"


def test_same_file_three_rounds_without_progress_kills_the_approach():
    """三轮改同一个文件没改善 = 方案不成立，必须 stop。

    这里的 diff 同时在变大，所以 DIFF_GROWING 也成立 —— 断言 stop 而不是
    rollback，守的正是那条优先级：同时成立时要给更强的结论，
    否则"退一步再试"会把这套规则要消灭的打转原样复现出来。
    """
    p = _policy()
    led = Ledger(budget=p.budget)
    for i, names in enumerate([("a", "b"), ("a", "c"), ("a", "d")], start=1):
        p.record(i, _cmp("no_change"), _report(2, names), f"d{i}" * i, ["src/x.py"])
    d = p.decide(led, ["src/x.py"])
    assert d.action == "stop" and d.code == "NO_PROGRESS"


def test_token_pressure_narrows_before_it_stops():
    """用到八成先收缩（关掉探索），不是直接停 —— 留出的两成是给收尾的。"""
    p = _policy(token_budget=10_000)
    led = Ledger(budget=p.budget)
    led.prompt_tokens = 8_500
    p.record(1, _cmp("improved"), _report(1, ["a"]), "d", ["s.py"])
    d = p.decide(led, ["s.py"])
    assert d.action == "narrow" and d.code == "TOKEN_PRESSURE"
    assert p.narrowed
    # 已经收缩过就不再重复触发
    assert p.decide(led, ["s.py"]).action == "continue"


def test_exceeding_the_file_limit_stops_the_run():
    p = _policy(max_modified_files=2)
    led = Ledger(budget=p.budget)
    d = p.decide(led, ["a.py", "b.py", "c.py"])
    assert d.action == "stop" and d.code == "MODIFIED_FILES_EXCEEDED"


def test_reviewer_repeating_itself_is_detected():
    p = _policy()
    v = {"requested_actions": ["加一个空输入的测试"]}
    assert p.reviewer_repeats(v) is False
    assert p.reviewer_repeats(dict(v)) is True
    assert p.reviewer_repeats({"requested_actions": ["换个别的要求"]}) is False


def test_disabled_policy_never_interferes():
    """消融配置：关掉停机策略之后它必须彻底沉默，否则消融就不干净。"""
    p = HaltingPolicy(budget=Budget(), enabled=False)
    led = Ledger(budget=p.budget)
    p.record(1, _cmp("no_change"), _report(2, ["a", "b"]), "d", ["s.py"])
    p.record(2, _cmp("no_change"), _report(2, ["a", "b"]), "d", ["s.py"])
    assert p.decide(led, ["a.py"] * 50).action == "continue"


def test_unparsed_output_never_counts_as_zero_failures():
    """解析不出数字时不能返回 0 —— 那会让跑挂的测试套件看起来像满分，

    最佳补丁就会选中一个把测试搞崩的版本。（TestReport.parsed 存在的同一个理由。）
    """
    p = _policy()
    broken = TestReport(exit_code=2, parsed=False)
    cp = p.record(1, _cmp("no_change"), broken, "d", ["s.py"])
    assert cp.failures > 0
