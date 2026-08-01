"""声称可信度：resolved 是召回,这个是精确率。

两次独立 sweep 的测量：带 Reviewer 的配置说"修好了"3 次、3 次全真;
不带的说了 14 次、只有 5 次是真的。resolved 率把这两种 agent 混成同一个数,
而"产出能不能免复查直接用"恰恰取决于这个被混掉的差别。

这个文件守三件事：
  1) 单次报告和重复聚合都报这个指标,分子分母口径一致（ok=True ∩ resolved / ok=True）;
  2) Reviewer 默认关（它是按需上岗的合并闸门,不是每次都跑的终审）;
  3) 消融变体不跟着产品默认值漂移 —— "full"永远带 Reviewer,不管默认值改成什么。
"""

import sys
from pathlib import Path

from repopilot.budget import Budget

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))

from harness import repeats, report  # noqa: E402
from harness.inference import budget_for  # noqa: E402


def _row(**kw):
    base = {"instance_id": "x__y-1", "resolved": False, "ok": False, "env_ok": True,
            "gold_ok": True, "patch_applied": True, "empty_patch": False,
            "halt_code": "", "audit": {}, "f2p_bad": [], "p2p_bad": [],
            "f2p_total": 3, "ledger": {}}
    return {**base, **kw}


# ---------------------------------------------------------------------------
# 1) 指标口径
# ---------------------------------------------------------------------------
def test_report_separates_claims_from_resolves():
    rows = [
        _row(instance_id="i1", ok=True, resolved=True),    # 声称且真修好
        _row(instance_id="i2", ok=True, resolved=False),   # 声称但没修好（误报）
        _row(instance_id="i3", ok=False, resolved=True),   # 修好了但没敢声称
        _row(instance_id="i4", ok=False, resolved=False),
    ]
    s = report.summarize(rows, {"instance_count": 4})
    assert s["claims"] == 2
    assert s["claims_true"] == 1
    assert s["quiet_resolved"] == 1
    assert s["resolved"] == 2          # 召回口径不受影响


def test_repeats_aggregate_claims_across_rounds():
    """单轮 1/1 说明不了什么,跨 R 轮累加后这个数字才有分量。"""
    per_instance = {
        "a": [{"ok": True, "resolved": True, "ledger": {}},
              {"ok": True, "resolved": False, "ledger": {}},
              {"ok": False, "resolved": True, "ledger": {}}],
    }
    agg = repeats.aggregate(per_instance)
    assert agg["a"]["claims"] == 2
    assert agg["a"]["claim_hits"] == 1


def test_render_reports_dash_when_never_claimed():
    """从未声称 ≠ 可信度 100%。分母为零必须显示成'—',不能显示成满分。"""
    per_instance = {"a": [{"ok": False, "resolved": True, "ledger": {}}]}
    text = repeats.render("x", repeats.aggregate(per_instance))
    assert "声称可信度：—" in text


# ---------------------------------------------------------------------------
# 2) + 3) Reviewer 的岗位
# ---------------------------------------------------------------------------
def test_reviewer_is_off_by_default_but_pinned_in_ablation_variants():
    assert Budget().allow_reviewer is False, \
        "Reviewer 是按需上岗的合并闸门,默认不该每次都跑"

    base = Budget()
    assert budget_for("full", base).allow_reviewer is True, \
        "消融变体必须钉死,不能跟着产品默认值漂移 —— 否则改一次默认值,'full' 就悄悄变成另一个配置"
    assert budget_for("no-reviewer", base).allow_reviewer is False
    assert budget_for("no-explorer", base).allow_reviewer is True, \
        "no-explorer 只消融 Explorer,Reviewer 必须保持和 full 一致"
