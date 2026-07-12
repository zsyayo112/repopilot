"""状态机的安全路径测试。

重点守住那条最容易被忽视、且改坏后果最严重的路径：executor 改动的文件数
超过 MAX_MODIFIED_FILES（疑似跑偏）时——
  1) 状态机不能"哑巴退出"，必须经过 REPORT 把情况讲清楚；
  2) 必须打印回滚命令，把撤不撤的决定留给人（不自动 reset）；
  3) 必须【跳过】Reviewer（对一堆乱改跑审查既费钱又无意义）。

这条路径不走真实 LLM：把 planner / executor / verifier / reviewer 全部 monkeypatch 掉，
只测状态机自己的编排逻辑。
"""

import subprocess

import pytest

from repopilot import orchestrator
from repopilot.verifier import TestReport


@pytest.fixture
def git_repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "init"], check=True)
    return tmp_path


def test_abort_on_too_many_files_reports_and_skips_review(
        git_repo, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(orchestrator, "MAX_MODIFIED_FILES", 2)
    monkeypatch.setattr(orchestrator, "RUNS_DIR", tmp_path / "runs")

    # 把所有会调模型/跑测试的环节替换成确定性桩
    monkeypatch.setattr(orchestrator, "make_plan", lambda *a, **k: {"root_cause_hypothesis": "x"})
    fake = TestReport(exit_code=1, passed=0, failed=1, errors=0, failed_names=["t::x"], tail="")
    monkeypatch.setattr(orchestrator, "run_tests", lambda *a, **k: fake)
    monkeypatch.setattr(orchestrator, "compare", lambda b, a: {
        "status": "no_change", "new_failures": [], "baseline": "b", "after": "a"})

    # executor 造 3 个新文件 → 超过上限 2
    def fake_exec(toolkit, perms, messages, trace):
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
