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
    # 三轮全是 no_change：improved 现在是通过态，会让循环正确地停在 B ——
    # 但这个测试守的是"C 改坏后回滚到 B"的路径，得让三轮都不达标才走得到 C。
    monkeypatch.setattr(orchestrator, "compare", lambda b, a: {
        "status": "no_change", "new_failures": [],
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


def test_improved_over_red_baseline_is_done_not_another_round(
        git_repo, tmp_path, monkeypatch):
    """红基线 7 个失败、修完剩 6 个（全是基线预置的）→ 已达标，必须停。

    真实案例（tenacity#233）：这条路径曾因"没全绿"又烧了一整轮预算去修
    与 issue 无关的环境失败，直到 TOKEN_LIMIT 才靠止损交出第 1 轮的正确补丁。
    与基线打平的 no_regression 都算过，严格更好的 improved 没有理由不算。
    """
    _stub_common(monkeypatch, tmp_path)
    base = TestReport(exit_code=1, failed=7, parsed=True,
                      failed_names=[f"t::e{i}" for i in range(7)])
    after = TestReport(exit_code=1, failed=6, parsed=True,
                       failed_names=[f"t::e{i}" for i in range(6)])
    monkeypatch.setattr(orchestrator, "run_tests", lambda *a, **k: base)
    monkeypatch.setattr(orchestrator, "run_tests_in", lambda *a, **k: after)
    monkeypatch.setattr(orchestrator, "compare", lambda b, a: {
        "status": "improved", "new_failures": [], "confidence": "high",
        "baseline": "7 failed", "after": "6 failed"})
    monkeypatch.setattr(orchestrator, "run_validation",
                        lambda *a, **k: type("V", (), {"ok": True, "steps": [],
                                                       "render": lambda self: ""})())

    rounds = []

    def fake_exec(toolkit, perms, messages, trace, **kw):
        rounds.append(1)
        (git_repo / "a.py").write_text("x = 1\n")
        return ("done", 0)
    monkeypatch.setattr(orchestrator, "run_executor", fake_exec)

    out = tmp_path / "r.json"
    code = orchestrator.solve(str(git_repo), "issue", test_cmd="echo", yes=True,
                              budget=Budget(max_fix_attempts=3, allow_reviewer=False),
                              result_path=str(out))

    assert code == 0
    assert len(rounds) == 1, "improved 已达标，不该再烧一轮去修基线预置的失败"
    res = json.loads(out.read_text())
    assert res["ok"] is True
    assert res["test_status"] == "improved"


def test_stop_reports_the_delivered_patch_not_the_last_round(
        git_repo, tmp_path, monkeypatch):
    """TOKEN_LIMIT 止损交出第 1 轮补丁时，result.json 说的必须是第 1 轮的成绩。

    曾经的行为：工作区里是回滚后的最佳补丁，报告里却是最后一轮的 regressed
    和它的回归名单 —— 成功交付被自己的报告说成失败（rollback 分支有
    _rebuild_comparison，stop 分支漏了）。
    """
    _stub_common(monkeypatch, tmp_path)
    seq = [TestReport(exit_code=1, failed=2, parsed=True, failed_names=["a", "b"]),
           TestReport(exit_code=1, failed=4, parsed=True,
                      failed_names=["a", "b", "c", "d"])]
    calls = {"n": 0}
    monkeypatch.setattr(orchestrator, "run_tests", lambda *a, **k: seq[0])
    monkeypatch.setattr(orchestrator, "run_tests_in",
                        lambda *a, **k: seq[min(calls["n"], len(seq)) - 1])
    monkeypatch.setattr(orchestrator, "compare", lambda b, a: {
        "status": "no_change" if a.failed == 2 else "regressed",
        "new_failures": [] if a.failed == 2 else ["t::c", "t::d"],
        "confidence": "high", "baseline": "2 failed", "after": f"{a.failed} failed"})

    def fake_exec(toolkit, perms, messages, trace, *, ledger=None, **kw):
        calls["n"] += 1
        (git_repo / "a.py").write_text(f"x = {calls['n']}\n")
        if calls["n"] == 2:
            ledger.prompt_tokens = ledger.budget.token_budget + 1
        return ("done", 0)
    monkeypatch.setattr(orchestrator, "run_executor", fake_exec)

    out = tmp_path / "r.json"
    orchestrator.solve(str(git_repo), "issue", test_cmd="echo", yes=True,
                       budget=Budget(max_fix_attempts=3, allow_reviewer=False),
                       result_path=str(out))

    res = json.loads(out.read_text())
    assert res["halt_code"] == "TOKEN_LIMIT"
    assert (git_repo / "a.py").read_text() == "x = 1\n", "交出的应是第 1 轮最佳补丁"
    assert res["test_status"] == "no_change", "结论必须是交付补丁的，不是最后一轮的"
    assert res["new_failures"] == [], "回滚后不该再挂着最后一轮的回归名单"
    assert res["halting"]["best_attempt"] == 1


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
                              budget=Budget(max_fix_attempts=4, max_review_rounds=3,
                                            allow_reviewer=True),
                              result_path=str(result_file))

    assert code == 1                            # 审查没通过 = 判负
    assert len(execs) == 2, "驳回应触发且只触发一次重修（第二次是同一个要求）"
    res = json.loads(result_file.read_text())
    assert res["reviewer"]["rejections"] == 2
    assert res["halt_code"] == "REVIEWER_REPEAT"


def _green_pipeline(monkeypatch, report):
    """基线绿、验证绿、lint/build 全过 —— 只留预算这一个变量。"""
    monkeypatch.setattr(orchestrator, "run_tests", lambda *a, **k: report)
    monkeypatch.setattr(orchestrator, "compare", lambda b, a: {
        "status": "still_green", "new_failures": [], "confidence": "high",
        "baseline": "g", "after": "g"})
    monkeypatch.setattr(orchestrator, "run_validation",
                        lambda *a, **k: type("V", (), {"ok": True, "steps": [],
                                                       "render": lambda self: ""})())


def test_wall_clock_gate_stops_before_verify_and_review(git_repo, tmp_path, monkeypatch):
    """墙钟超了就不许再进 VERIFY / Reviewer —— 但仍然要走完 REPORT 交卷。

    这条守的是一次真实事故：executor 自己在 1757 秒（预算 1800）停了，
    随后 VERIFY 又跑了 15 分钟、Reviewer 又卡在网络退避里 30 分钟，
    一条 30 分钟预算的实例跑成 65 分钟。超支的部分全在 executor 之外，
    而那时没有任何一处代码在看表。
    """
    _stub_common(monkeypatch, tmp_path)
    green = TestReport(exit_code=0, passed=3, parsed=True)
    _green_pipeline(monkeypatch, green)

    verifies = []
    monkeypatch.setattr(orchestrator, "run_tests_in",
                        lambda *a, **k: (verifies.append(1), green)[1])

    def fake_exec(toolkit, perms, messages, trace, *, ledger=None, **kw):
        (git_repo / "a.py").write_text("x = 1\n")
        ledger.started_at -= 10_000        # 把表拨到 1800 秒预算之外
        return ("done", 0)
    monkeypatch.setattr(orchestrator, "run_executor", fake_exec)

    def boom_review(*a, **k):
        raise AssertionError("预算耗尽后不该再开一次模型调用")
    monkeypatch.setattr(orchestrator, "review", boom_review)

    out = tmp_path / "r.json"
    code = orchestrator.solve(str(git_repo), "issue", test_cmd="echo", yes=True,
                              budget=Budget(instance_timeout=1800, allow_reviewer=True),
                              result_path=str(out))

    assert code == 1
    assert verifies == [], "墙钟已经超了，不该再花十几分钟跑一次全量验证"
    res = json.loads(out.read_text())
    assert res["halt_code"] == "TIMEOUT"
    # 没验证过就【不许】写一个像样的状态："no_change" 会被下游当成真跑过测试
    assert res["test_status"] == "unverified"
    assert (git_repo / "a.py").read_text() == "x = 1\n", "补丁该照常交出来"


def test_token_limit_still_verifies_but_skips_reviewer(git_repo, tmp_path, monkeypatch):
    """token 用光 ≠ 时间用光：验证不花 token，该跑还得跑；Reviewer 花，跳。

    一刀切地"超支就什么都不做"会白白丢掉验证信息 —— 而这次评测真正被耗尽的
    资源是时间，不是 token。
    """
    _stub_common(monkeypatch, tmp_path)
    green = TestReport(exit_code=0, passed=3, parsed=True)
    _green_pipeline(monkeypatch, green)

    verifies = []
    monkeypatch.setattr(orchestrator, "run_tests_in",
                        lambda *a, **k: (verifies.append(1), green)[1])

    def fake_exec(toolkit, perms, messages, trace, *, ledger=None, **kw):
        (git_repo / "a.py").write_text("x = 1\n")
        ledger.prompt_tokens = ledger.budget.token_budget + 1
        return ("done", 0)
    monkeypatch.setattr(orchestrator, "run_executor", fake_exec)

    def boom_review(*a, **k):
        raise AssertionError("token 已耗尽，不该再开一次模型调用")
    monkeypatch.setattr(orchestrator, "review", boom_review)

    out = tmp_path / "r.json"
    orchestrator.solve(str(git_repo), "issue", test_cmd="echo", yes=True,
                       budget=Budget(instance_timeout=1800, allow_reviewer=True),
                       result_path=str(out))

    assert verifies, "TOKEN_LIMIT 不该把验证也拦掉 —— 跑一次测试一个 token 都不花"
    res = json.loads(out.read_text())
    assert res["halt_code"] == "TOKEN_LIMIT"
    assert res["test_status"] == "still_green"
    # "想要审查但没钱了" 和 "这次实验故意关掉审查" 是两件事，报告里不能混
    assert res["reviewer"]["enabled"] is True
    assert res["reviewer"]["decision"] is None


def test_empty_diff_is_not_a_success(git_repo, tmp_path, monkeypatch):
    """agent 一行没改就收工 → 不许判成功。

    真实案例（testify#1785）：executor 空转到放弃、零改动，工作区原样当然
    still_green、验证当然全过，报告却给了 ok=true 退出码 0 ——
    "没交卷"被说成"考了满分"。still_green 只有在有补丁的前提下才是成绩。
    """
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
                        lambda *a, **k: ("我放弃了", 0))   # 不写任何文件

    out = tmp_path / "r.json"
    code = orchestrator.solve(str(git_repo), "issue", test_cmd="echo", yes=True,
                              budget=Budget(max_fix_attempts=1, allow_reviewer=False),
                              result_path=str(out))

    assert code == 1
    res = json.loads(out.read_text())
    assert res["ok"] is False
    assert res["halt_code"] == "NO_PATCH"
    assert res["test_status"] == "still_green"   # 事实照记，但它救不了 ok


def test_empty_diff_retries_instead_of_declaring_done(git_repo, tmp_path, monkeypatch):
    """一轮零改动 + 还有重试额度 → 喂回"零交付"纠偏再来一轮，不许收工。

    sqlfluff#8230 实战：executor 40 轮诊断到一半被截停、零改动，
    still_green 让循环直接交卷 —— 明明还有两轮额度没用。
    """
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

    rounds = []

    def fake_exec(toolkit, perms, messages, trace, **kw):
        rounds.append(len(messages))
        if len(rounds) == 2:                      # 第 1 轮空手而归，第 2 轮交付
            (git_repo / "a.py").write_text("x = 1\n")
        return ("done", 0)
    monkeypatch.setattr(orchestrator, "run_executor", fake_exec)

    out = tmp_path / "r.json"
    code = orchestrator.solve(str(git_repo), "issue", test_cmd="echo", yes=True,
                              budget=Budget(max_fix_attempts=3, allow_reviewer=False),
                              result_path=str(out))

    assert len(rounds) == 2, "零交付应触发回炉，而不是 still_green 收工"
    assert code == 0
    res = json.loads(out.read_text())
    assert res["ok"] is True and res["attempts"] == 2


def test_test_only_patch_is_not_a_success(git_repo, tmp_path, monkeypatch):
    """只交付复现测试 = 没修任何东西，不许判成功。

    cobra#1777 重跑实测：一个只含 47 行复现测试的补丁，靠 still_green +
    "有交付物"拿到了 exit 0。修复必须落在源码上（与官方判分口径一致：
    测试文件会被还原、拿零分）。
    """
    _stub_common(monkeypatch, tmp_path)
    green = TestReport(exit_code=0, passed=3, parsed=True)
    monkeypatch.setattr(orchestrator, "run_tests", lambda *a, **k: green)
    monkeypatch.setattr(orchestrator, "run_tests_in", lambda *a, **k: green)
    monkeypatch.setattr(orchestrator, "compare", lambda b, a: {
        "status": "still_green", "new_failures": [], "confidence": "high",
        "baseline": "g", "after": "g"})

    def fake_exec(toolkit, perms, messages, trace, **kw):
        (git_repo / "tests").mkdir(exist_ok=True)
        (git_repo / "tests" / "test_repro.py").write_text(
            "def test_repro():\n    assert False\n")
        return ("写了复现测试", 0)
    monkeypatch.setattr(orchestrator, "run_executor", fake_exec)

    out = tmp_path / "r.json"
    code = orchestrator.solve(str(git_repo), "issue", test_cmd="echo", yes=True,
                              budget=Budget(max_fix_attempts=1, allow_reviewer=False),
                              result_path=str(out))

    assert code == 1
    res = json.loads(out.read_text())
    assert res["ok"] is False
    assert res["halt_code"] == "TEST_ONLY_PATCH"


def test_red_repro_test_gets_repro_feedback_not_regression_blame(
        git_repo, tmp_path, monkeypatch):
    """只动了测试 + 新增失败 = 自己写的复现在红：预期中的红，不是回归。

    R1 在 cobra#1777 实测：正确的红复现被 _retry_feedback 判成"你改出了回归、
    优先处理"，第 2 轮被带偏。正确的反馈是"复现就位，修源码让它转绿"。
    """
    _stub_common(monkeypatch, tmp_path)
    seq = [TestReport(exit_code=0, passed=3, parsed=True),                 # 基线
           TestReport(exit_code=1, passed=3, failed=1, parsed=True,
                      failed_names=["tests/test_repro.py::test_repro"]),   # 复现红
           TestReport(exit_code=0, passed=4, parsed=True)]                 # 修复后
    calls = {"n": 0}
    monkeypatch.setattr(orchestrator, "run_tests", lambda *a, **k: seq[0])
    monkeypatch.setattr(orchestrator, "run_tests_in",
                        lambda *a, **k: seq[min(calls["n"], 2)])
    monkeypatch.setattr(orchestrator, "compare", lambda b, a: {
        "status": "regressed" if a.failed else "still_green",
        "new_failures": list(a.failed_names), "confidence": "high",
        "baseline": "g", "after": f"{a.failed} failed"})
    monkeypatch.setattr(orchestrator, "run_validation",
                        lambda *a, **k: type("V", (), {"ok": True, "steps": [],
                                                       "render": lambda self: ""})())

    feedbacks = []

    def fake_exec(toolkit, perms, messages, trace, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            (git_repo / "tests").mkdir(exist_ok=True)
            (git_repo / "tests" / "test_repro.py").write_text(
                "def test_repro():\n    assert False\n")
        else:
            feedbacks.append(messages[-1]["content"])
            (git_repo / "a.py").write_text("x = 1\n")          # 补上源码修复
        return ("done", 0)
    monkeypatch.setattr(orchestrator, "run_executor", fake_exec)

    out = tmp_path / "r.json"
    code = orchestrator.solve(str(git_repo), "issue", test_cmd="echo", yes=True,
                              budget=Budget(max_fix_attempts=3, allow_reviewer=False),
                              result_path=str(out))

    assert code == 0
    assert feedbacks and "预期中的红" in feedbacks[0], \
        "红复现应得到'复现就位'反馈，而不是被当成回归指责"
    assert "你改出来的回归" not in feedbacks[0]


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
    def fake_exec(toolkit, perms, messages, trace, **kw):
        (git_repo / "a.py").write_text("x = 1\n")   # ok=true 要求真的交付了补丁
        return ("done", 0)
    monkeypatch.setattr(orchestrator, "run_executor", fake_exec)
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
