"""失败归因与报告：把"3/8 resolved"变成一份能指导下一步的东西。

上一版报告里只有一句"3/8 resolved"。那句话不指导任何行动 —— 它不告诉你
该去改 Explorer 还是改 Reviewer。归因表告诉你：大部分失败是根因定位错误，
就该去改代码检索，而不是继续加审查轮数。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))

from harness import failures, report  # noqa: E402


def _row(**kw):
    base = {"instance_id": "x__y-1", "resolved": False, "env_ok": True,
            "gold_ok": True, "patch_applied": True, "empty_patch": False,
            "halt_code": "", "audit": {}, "f2p_bad": [], "p2p_bad": [],
            "f2p_total": 3, "ledger": {}}
    return {**base, **kw}


# ---------------------------------------------------------------------------
# 归因
# ---------------------------------------------------------------------------
def test_resolved_instances_are_not_classified():
    assert failures.classify(_row(resolved=True)) == ""


def test_environment_comes_first_because_the_run_never_started():
    """环境没装起来的实例，后面所有信号都没有意义 —— 必须最先判掉。"""
    assert failures.classify(
        _row(env_ok=False, halt_code="TOKEN_LIMIT", f2p_bad=["a"])) == "ENVIRONMENT_ERROR"


def test_gold_uncertifiable_outranks_the_agents_own_failure():
    """gold 自己都过不了 = 判分器在这条上不可信，不能把它算成 agent 的失败。"""
    assert failures.classify(
        _row(gold_ok=False, f2p_bad=["a", "b", "c"])) == "GOLD_UNCERTIFIABLE"


def test_regression_outranks_incomplete_fix():
    """把本来好的改坏了，比"没修完"严重，必须先判。"""
    row = _row(p2p_bad=["t::old"], f2p_bad=["t::new"])
    assert failures.classify(row) == "REGRESSION"


def test_all_f2p_red_is_root_cause_wrong_but_some_red_is_incomplete():
    """一条都没转绿 = 根因整个判断错了；转绿一部分 = 方向对，没修完。

    这两类对应的下一步完全不同（改检索 vs 改收敛），所以必须分开。
    """
    assert failures.classify(_row(f2p_bad=["a", "b", "c"], f2p_total=3)) \
        == "ROOT_CAUSE_WRONG"
    assert failures.classify(_row(f2p_bad=["a"], f2p_total=3)) == "INCOMPLETE_FIX"


def test_test_tampering_is_its_own_category():
    row = _row(audit={"suspicious": True, "test_tampering_detected": True},
               f2p_bad=["a"])
    assert failures.classify(row) == "TEST_TAMPERING"


def test_halting_codes_map_to_tool_loop():
    for code in ("SAME_FAILURE_TWICE", "NO_PROGRESS", "DIFF_GROWING"):
        assert failures.classify(_row(halt_code=code, f2p_bad=["a"])) == "TOOL_LOOP"


def test_editing_files_gold_never_touched_is_a_localisation_failure():
    row = _row(audit={"files_overlap": 0.0, "gold_files": ["src/real.py"]},
               f2p_bad=["a"], f2p_total=3)
    assert failures.classify(row) == "WRONG_FILE_EDITED"


def test_apply_failure_and_empty_patch_are_distinguished():
    assert failures.classify(_row(patch_applied=False)) == "PATCH_APPLY_FAILED"
    assert failures.classify(_row(empty_patch=True)) == "NO_PATCH"


def test_tree_renders_counts_in_descending_order():
    rows = [_row(instance_id=f"i{i}") for i in range(4)]
    for r, cat in zip(rows, ["ROOT_CAUSE_WRONG", "ROOT_CAUSE_WRONG",
                             "TOOL_LOOP", "TIMEOUT"]):
        r["failure_category"] = cat
    tree = failures.render_tree(rows)
    assert tree.startswith("4 个失败")
    assert tree.index("ROOT_CAUSE_WRONG") < tree.index("TOOL_LOOP")


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------
def test_denominator_is_the_frozen_list_not_the_rows_that_ran():
    """跑不动的实例留在分母里 —— 这是整份报告最重要的一条口径。"""
    rows = [_row(instance_id="a", resolved=True),
            _row(instance_id="b", env_ok=False)]
    s = report.summarize(rows, {"instance_count": 10})
    assert s["frozen_total"] == 10
    assert s["resolved"] == 1 and s["resolved_rate"] == 0.1
    assert s["env_failed"] == 1
    # 同时也给"仅可判定实例"口径，两个都报，不挑对自己有利的那个
    assert s["certifiable"] == 1 and s["resolved_on_certifiable"] == 1


def test_reviewer_saves_and_suspected_false_kills_are_counted_separately():
    saved = _row(instance_id="s", resolved=True,
                 reviewer={"enabled": True, "rejections": 1},
                 ledger={"tokens_by_role": {"reviewer": 5000}})
    killed = _row(instance_id="k", resolved=False, test_status="still_green",
                  reviewer={"enabled": True, "rejections": 2},
                  ledger={"tokens_by_role": {"reviewer": 7000}})
    s = report.summarize([saved, killed], {"instance_count": 2})
    assert s["reviewer_rejections"] == 3
    assert s["reviewer_saved"] == 1
    assert s["reviewer_suspected_false_kills"] == 1
    assert s["reviewer_tokens"] == 12000


def test_paired_comparison_exposes_the_column_a_net_number_hides():
    """净差把"简化版独赢"那一列藏起来了 —— 而那正是要追问的地方。"""
    full = [_row(instance_id="i1", resolved=True), _row(instance_id="i2", resolved=True),
            _row(instance_id="i3", resolved=False), _row(instance_id="i4", resolved=False)]
    ablated = [_row(instance_id="i1", resolved=False), _row(instance_id="i2", resolved=True),
               _row(instance_id="i3", resolved=False), _row(instance_id="i4", resolved=True)]
    p = report.paired(full, ablated, "full", "no-reviewer")

    assert p["n"] == 4
    assert p["a_only"] == ["i1"]        # Reviewer 真的救了它
    assert p["b_only"] == ["i4"]        # Reviewer 误杀
    assert p["both"] == ["i2"] and p["neither"] == ["i3"]
    # 净差是 0，但四个格子讲的是完全不同的故事
    assert len(p["a_only"]) - len(p["b_only"]) == 0


def test_paired_only_counts_instances_both_variants_ran():
    a = [_row(instance_id="i1", resolved=True), _row(instance_id="i2", resolved=True)]
    b = [_row(instance_id="i1", resolved=False)]
    assert report.paired(a, b, "x", "y")["n"] == 1
