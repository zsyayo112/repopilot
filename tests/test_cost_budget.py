"""按钱结算的预算：守住"预算单位 = 报告单位"这条等式。

背景是首轮 sweep 的一个测量事实：92% 的计费 token 是缓存命中，价格只有
未命中的 1/4。token 口径把命中按全价记账，于是 11 条实例里 7 条被 600k
上限停下时，实际只花了 6 分钱 —— 预算在一个几乎不要钱的资源上掐流量。
更糟的是它对变体不公平：前缀越稳定的 agent 命中率越高，按 token 计价
恰好把这项真实的效率优势记成了劣势。

这个文件守四件事：
  1) 账本：缓存命中按实价计入成本预算，不再按全价挤占额度；
  2) 指纹：价格表参与成本口径的指纹（价一变、不可比），token 口径不受影响；
  3) 归因：COST_LIMIT 有自己的桶，不混进 TOKEN_LIMIT；
  4) 管道：--cost-budget 必须传进并发子进程 —— 父进程按新口径写 manifest、
     子进程按默认值跑，是那种两边都不报错、数字却不属于任何配置的 bug。
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from repopilot import budget as budget_mod
from repopilot import orchestrator
from repopilot.budget import Budget, Ledger
from repopilot.verifier import TestReport

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))

import run as eval_run  # noqa: E402
from harness import failures  # noqa: E402


# ---------------------------------------------------------------------------
# 1) 账本
# ---------------------------------------------------------------------------
def _ledger(cost_budget, *, prompt=0, cached=0, completion=0, model="deepseek-chat"):
    led = Ledger(budget=Budget(model=model, cost_budget_usd=cost_budget,
                               token_budget=600_000))
    led.prompt_tokens, led.cached_tokens, led.completion_tokens = prompt, cached, completion
    return led


def test_cache_hits_no_longer_exhaust_the_budget_at_full_price():
    """首轮的典型画像：700k 计费 token、93% 命中。

    token 口径下它早就死了（700k > 600k）。按钱算它只花了
    50k×0.27 + 650k×0.07 + 10k×1.10 = $0.070 —— 预算 $0.15 还剩一半。
    """
    led = _ledger(0.15, prompt=700_000, cached=650_000, completion=10_000)
    assert led.exhausted() is None
    assert led.pressure() == pytest.approx(0.070 / 0.15, abs=0.01)


def test_cost_limit_fires_when_the_money_is_actually_gone():
    led = _ledger(0.05, prompt=700_000, cached=650_000, completion=10_000)
    assert led.exhausted() == "COST_LIMIT"


def test_unpriced_model_keeps_token_metering():
    """模型不在价格表里 → 钱的口径根本不生效，token 口径照旧。

    入口（eval/run.py）会直接拒绝这种配置；这里守的是防线的第二层：
    就算有人绕过入口，也绝不能出现"成本算出来是 0、预算永远花不完"。
    """
    led = _ledger(1.0, prompt=700_000, cached=650_000, model="mystery-model")
    assert led.exhausted() == "TOKEN_LIMIT"


def test_token_budget_stops_judging_under_usd_metering():
    """按钱结算时 token_budget 退出裁决 —— 一次运行只有一种货币。"""
    led = _ledger(10.0, prompt=5_000_000, cached=4_900_000)   # token 口径下超了 8 倍
    assert led.exhausted() is None


# ---------------------------------------------------------------------------
# 2) 指纹
# ---------------------------------------------------------------------------
def test_pricing_is_part_of_the_fingerprint_only_when_it_shapes_behavior(monkeypatch):
    cost = Budget(model="deepseek-chat", cost_budget_usd=0.15)
    tokens = Budget(model="deepseek-chat")
    fp_cost, fp_tokens = cost.fingerprint(), tokens.fingerprint()

    monkeypatch.setitem(budget_mod.PRICING, "deepseek-chat", (0.27, 1.10, 0.10))
    assert cost.fingerprint() != fp_cost, \
        "命中价 0.07→0.10 改变了同一笔钱能买到的轮数，指纹必须跟着变"
    assert tokens.fingerprint() == fp_tokens, \
        "token 口径的行为和价格无关，指纹不该跟着价格漂移"


# ---------------------------------------------------------------------------
# 3) 归因
# ---------------------------------------------------------------------------
def test_cost_limit_gets_its_own_bucket():
    row = {"resolved": False, "env_ok": True, "gold_ok": True,
           "halt_code": "COST_LIMIT", "patch_applied": True, "empty_patch": False,
           "audit": {}, "f2p_bad": [], "p2p_bad": [], "f2p_total": 3}
    assert failures.classify(row) == "COST_LIMIT"
    assert "COST_LIMIT" in failures.LABELS


# ---------------------------------------------------------------------------
# 4) 管道：flag 必须走到子进程
# ---------------------------------------------------------------------------
def _args(**overrides):
    base = dict(split="dev", variant="full", run_id="t", model="deepseek-chat",
                token_budget=600_000, max_tool_calls=100, timeout=1800,
                test_timeout=900, cost_budget=None, force=False)
    base.update(overrides)
    return type("Args", (), base)()


def test_child_cmd_forwards_cost_budget():
    cmd = eval_run._child_cmd(_args(cost_budget=0.25), "pallets__flask-5014")
    i = cmd.index("--cost-budget")
    assert cmd[i + 1] == "0.25"


def test_child_cmd_omits_cost_budget_when_unset():
    """token 口径的运行，子进程命令行里不该出现这个 flag ——
    argparse 的 None 序列化成 'None' 会让子进程直接崩。"""
    assert "--cost-budget" not in eval_run._child_cmd(_args(), "pallets__flask-5014")


# ---------------------------------------------------------------------------
# 5) 状态机：COST_LIMIT 走 TOKEN_LIMIT 同一条闸门路径
#    （钱花光了：验证不花钱照跑，Reviewer 花钱必须跳）
# ---------------------------------------------------------------------------
@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "a.py").write_text("x = 0\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "init"], check=True)
    return repo


def test_cost_exhaustion_verifies_but_skips_reviewer(git_repo, tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(orchestrator, "make_plan",
                        lambda *a, **k: {"root_cause_hypothesis": "x"})
    green = TestReport(exit_code=0, passed=3, parsed=True)
    monkeypatch.setattr(orchestrator, "run_tests", lambda *a, **k: green)
    monkeypatch.setattr(orchestrator, "compare", lambda b, a: {
        "status": "still_green", "new_failures": [], "confidence": "high",
        "baseline": "g", "after": "g"})
    monkeypatch.setattr(orchestrator, "run_validation",
                        lambda *a, **k: type("V", (), {"ok": True, "steps": [],
                                                       "render": lambda self: ""})())

    verifies = []
    monkeypatch.setattr(orchestrator, "run_tests_in",
                        lambda *a, **k: (verifies.append(1), green)[1])

    def fake_exec(toolkit, perms, messages, trace, *, ledger=None, **kw):
        (git_repo / "a.py").write_text("x = 1\n")
        ledger.completion_tokens = 50_000       # 50k×$1.10/M = $0.055 > $0.01
        return ("done", 0)
    monkeypatch.setattr(orchestrator, "run_executor", fake_exec)

    def boom_review(*a, **k):
        raise AssertionError("钱已花光，不该再开一次模型调用")
    monkeypatch.setattr(orchestrator, "review", boom_review)

    out = tmp_path / "r.json"
    orchestrator.solve(str(git_repo), "issue", test_cmd="echo", yes=True,
                       budget=Budget(model="deepseek-chat", cost_budget_usd=0.01),
                       result_path=str(out))

    assert verifies, "COST_LIMIT 不该拦验证 —— 跑一次测试一分钱都不花"
    res = json.loads(out.read_text())
    assert res["halt_code"] == "COST_LIMIT"
    assert res["test_status"] == "still_green"
