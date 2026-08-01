"""状态机的安全路径测试。

重点守住那条最容易被忽视、且改坏后果最严重的路径：executor 改动的文件数
超过 MAX_MODIFIED_FILES（疑似跑偏）时——
  1) 状态机不能"哑巴退出"，必须经过 REPORT 把情况讲清楚；
  2) 必须打印回滚命令，把撤不撤的决定留给人（不自动 reset）；
  3) 必须【跳过】Reviewer（对一堆乱改跑审查既费钱又无意义）。

第二组测试守的是新加的停机策略与 Reviewer 环：
  4) 连续两轮失败完全相同 → 必须停，不许继续烧额度；
  5) 交卷时交的是【历史最佳补丁】，不是最后一版；
  6) Reviewer 驳回 → 回到 EXECUTE 再修；同一个要求提第二次 → 停。

这些路径都不走真实 LLM：把 planner / executor / verifier / reviewer 全部
monkeypatch 掉，只测状态机自己的编排逻辑。
"""

import json
import subprocess

import pytest

from repopilot import orchestrator
from repopilot.budget import Budget
from repopilot.verifier import TestReport


@pytest.fixture
def git_repo(tmp_path):
    """目标仓库放在 tmp_path/repo 下，【不能】直接用 tmp_path。

    因为轨迹目录被打到 tmp_path/runs，而回滚会在目标仓库里跑 `git clean -fd`——
    两者同级才不会互相删。这不只是测试洁癖：真实运行里 RUNS_DIR 属于
    RepoPilot 自己的项目根、目标仓库是另一个目录，本来就是分开的。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "a.py").write_text("x = 0\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "init"], check=True)
    return repo


def _stub_common(monkeypatch, tmp_path):
    """把所有会调模型的环节换成确定性桩。"""
    monkeypatch.setattr(orchestrator, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(orchestrator, "make_plan",
                        lambda *a, **k: {"root_cause_hypothesis": "x"})


def test_abort_on_too_many_files_reports_and_skips_review(
        git_repo, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(orchestrator, "MAX_MODIFIED_FILES", 2)
    _stub_common(monkeypatch, tmp_path)

    fake = TestReport(exit_code=1, passed=0, failed=1, errors=0, failed_names=["t::x"], tail="")
    monkeypatch.setattr(orchestrator, "run_tests", lambda *a, **k: fake)
    monkeypatch.setattr(orchestrator, "run_tests_in", lambda *a, **k: fake)
    monkeypatch.setattr(orchestrator, "compare", lambda b, a: {
        "status": "no_change", "new_failures": [], "baseline": "b", "after": "a"})

    # executor 造 3 个新文件 → 超过上限 2
    def fake_exec(toolkit, perms, messages, trace, **kw):
        for i in range(3):
            (git_repo / f"junk{i}.py").write_text("boom\n")
        return ("done", 0)
    monkeypatch.setattr(orchestrator, "run_executor", fake_exec)

    # Reviewer 一旦被调用就是 bug
    def boom_review(*a, **k):
        raise AssertionError("跑偏中止时不应调用 Reviewer")
    monkeypatch.setattr(orchestrator, "review", boom_review)

    code = orchestrator.solve(str(git_repo), "some issue", test_cmd="echo", yes=True)

    out = capsys.readouterr().out
    assert code == 1                      # 判负
    assert "中止" in out                  # 报告里显式说明了中止原因
    assert "git checkout" in out          # 打印了回滚命令
    assert (git_repo / "junk0.py").exists()  # 没有自动回滚——决定权留给人


def test_identical_failures_twice_halts_early(git_repo, tmp_path, monkeypatch):
    """连续两轮失败完全相同 = 这一轮什么都没改变，必须停在第 2 轮。

    额度给了 5 轮。没有停机策略的话它会老老实实烧完 5 轮 —— 那正是评测里
    120 次工具调用的来源。
    """
    _stub_common(monkeypatch, tmp_path)
    same = TestReport(exit_code=1, failed=2, parsed=True,
                      failed_names=["t::a", "t::b"], tail="boom")
    monkeypatch.setattr(orchestrator, "run_tests", lambda *a, **k: same)
    monkeypatch.setattr(orchestrator, "run_tests_in", lambda *a, **k: same)
    monkeypatch.setattr(orchestrator, "compare", lambda b, a: {
        "status": "no_change", "new_failures": [], "confidence": "high",
        "baseline": "b", "after": "2 failed"})

    rounds = []

    def fake_exec(toolkit, perms, messages, trace, **kw):
        rounds.append(1)
        (git_repo / "a.py").write_text(f"x = {len(rounds)}\n")
        return ("done", 0)
    monkeypatch.setattr(orchestrator, "run_executor", fake_exec)
    monkeypatch.setattr(orchestrator, "review",
                        lambda *a, **k: {"decision": "revise", "requested_actions": [],
                                         "comments": "no", "unrelated_changes": []})

    result_file = tmp_path / "result.json"
    orchestrator.solve(str(git_repo), "issue", test_cmd="echo", yes=True,
                       budget=Budget(max_fix_attempts=5, allow_reviewer=False),
                       result_path=str(result_file))

    assert len(rounds) == 2, "失败指纹连续两轮相同后应立刻停止，而不是烧完 5 轮"
    res = json.loads(result_file.read_text())
    assert res["halt_code"] == "SAME_FAILURE_TWICE"


def test_keeps_best_patch_not_latest(git_repo, tmp_path, monkeypatch):
    """补丁 A 失败 3 → 补丁 B 失败 1 → 补丁 C 失败 5：交卷时工作区里必须是 B。

    这就是 halting.py 文件头那个 A/B/C 例子的可执行版本。默认行为（在最新
    修改上继续）会交出 C —— 把已经拿到手的成绩丢掉。
    """
    _stub_common(monkeypatch, tmp_path)
    seq = [TestReport(exit_code=1, failed=3, parsed=True, failed_names=["a", "b", "c"]),
           TestReport(exit_code=1, failed=1, parsed=True, failed_names=["a"]),
           TestReport(exit_code=1, failed=5, parsed=True,
                      failed_names=["a", "b", "c", "d", "e"])]
    calls = {"n": 0}

    monkeypatch.setattr(orchestrator, "run_tests", lambda *a, **k: seq[0])
    monkeypatch.setattr(orchestrator, "run_tests_in",
                        lambda *a, **k: seq[min(calls["n"], len(seq)) - 1])
    monkeypatch.setattr(orchestrator, "compare", lambda b, a: {
        "status": "improved" if a.failed < 3 else "no_change", "new_failures": [],
        "confidence": "high", "baseline": "3 failed", "after": f"{a.failed} failed"})

    def fake_exec(toolkit, perms, messages, trace, **kw):
        calls["n"] += 1
        (git_repo / "a.py").write_text(f"x = {calls['n']}\n")
        return ("done", 0)
    monkeypatch.setattr(orchestrator, "run_executor", fake_exec)

    orchestrator.solve(str(git_repo), "issue", test_cmd="echo", yes=True,
                       budget=Budget(max_fix_attempts=3, allow_reviewer=False))

    # 第 2 轮（失败 1 个）是最佳；第 3 轮把它改坏了，必须被回滚掉
    assert (git_repo / "a.py").read_text() == "x = 2\n"


def test_reviewer_rejection_triggers_another_round(git_repo, tmp_path, monkeypatch):
    """Reviewer 驳回 → 回到 EXECUTE；同一个要求提第二次 → 停止重修。"""
    _stub_common(monkeypatch, tmp_path)
    green = TestReport(exit_code=0, passed=5, parsed=True)
    monkeypatch.setattr(orchestrator, "run_tests", lambda *a, **k: green)
    monkeypatch.setattr(orchestrator, "run_tests_in", lambda *a, **k: green)
    monkeypatch.setattr(orchestrator, "compare", lambda b, a: {
        "status": "still_green", "new_failures": [], "confidence": "high",
        "baseline": "green", "after": "green"})
    monkeypatch.setattr(orchestrator, "run_validation",
                        lambda *a, **k: type("V", (), {"ok": True, "steps": [],
                                                       "render": lambda self: ""})())

    execs = []

    def fake_exec(toolkit, perms, messages, trace, **kw):
        execs.append(len(messages))
        (git_repo / "a.py").write_text(f"x = {len(execs)}\n")
        return ("done", 0)
    monkeypatch.setattr(orchestrator, "run_executor", fake_exec)

    # 每次都驳回、而且每次要求一模一样 —— 第二次就该被停机策略拦下
    monkeypatch.setattr(orchestrator, "review", lambda *a, **k: {
        "decision": "revise", "requested_actions": ["加一个空输入的测试"],
        "comments": "不够", "unrelated_changes": []})

    result_file = tmp_path / "r.json"
    code = orchestrator.solve(str(git_repo), "issue", test_cmd="echo", yes=True,
                              budget=Budget(max_fix_attempts=4, max_review_rounds=3),
                              result_path=str(result_file))

    assert code == 1                            # 审查没通过 = 判负
    assert len(execs) == 2, "驳回应触发且只触发一次重修（第二次是同一个要求）"
    res = json.loads(result_file.read_text())
    assert res["reviewer"]["rejections"] == 2
    assert res["halt_code"] == "REVIEWER_REPEAT"


def test_result_json_carries_budget_and_ledger(git_repo, tmp_path, monkeypatch):
    """result.json 是评测线束唯一的判据入口，关键字段必须齐。"""
    _stub_common(monkeypatch, tmp_path)
    green = TestReport(exit_code=0, passed=3, parsed=True)
    monkeypatch.setattr(orchestrator, "run_tests", lambda *a, **k: green)
    monkeypatch.setattr(orchestrator, "run_tests_in", lambda *a, **k: green)
    monkeypatch.setattr(orchestrator, "compare", lambda b, a: {
        "status": "still_green", "new_failures": [], "confidence": "high",
        "baseline": "g", "after": "g"})
    monkeypatch.setattr(orchestrator, "run_validation",
                        lambda *a, **k: type("V", (), {"ok": True, "steps": [],
                                                       "render": lambda self: ""})())
    monkeypatch.setattr(orchestrator, "run_executor",
                        lambda *a, **k: ("done", 0))
    monkeypatch.setattr(orchestrator, "review", lambda *a, **k: {
        "decision": "accept", "requested_actions": [], "comments": "ok",
        "unrelated_changes": []})

    out = tmp_path / "res.json"
    budget = Budget(max_fix_attempts=2)
    code = orchestrator.solve(str(git_repo), "issue", test_cmd="echo", yes=True,
                              budget=budget, result_path=str(out))

    assert code == 0
    res = json.loads(out.read_text())
    assert res["ok"] is True
    assert res["budget_fingerprint"] == budget.fingerprint()
    for key in ("ledger", "patch_audit", "halting", "reviewer", "attempts"):
        assert key in res
