"""报告：同时展示效果与成本，以及【成对】的消融比较。

【为什么必须同时报成本】
"resolved 率 30% → 36%" 单独看是个好消息。加上一行就未必了：

    resolved：30% → 36%
    平均 token：45k → 92k
    平均成本：$0.031 → $0.064

多了六个百分点，钱翻了一倍。这是不是好交易得看场景，但**不报成本就等于
替读者做了这个判断**。所以最终表里效果和成本永远并排。

【为什么消融要成对，不要各跑一批】
分别跑两批不同实例，得到 "完整版 36%、去 Reviewer 30%"，你只知道有 6 个点的
差。但在同一批实例上成对比较，得到的是四个格子：

    实例1：完整版成功，去 Reviewer 失败    ← 完整版独赢，Reviewer 真的救了它
    实例2：两个版本都成功                  ← Reviewer 在这条上没起作用
    实例3：两个版本都失败                  ← Reviewer 也救不了
    实例4：完整版失败，去 Reviewer 成功    ← Reviewer 误杀

"6 个点"是 (独赢 - 独输) 的净值，它把误杀那一列藏起来了。而误杀恰恰是
Reviewer 最需要被追问的地方。**成对结果比百分比更能解释设计价值。**
"""

from __future__ import annotations

import json
from pathlib import Path

from . import failures


def load_rows(run_dir: Path) -> list[dict]:
    """把一次运行的所有实例结果合并成扁平行。"""
    rows = []
    for d in sorted(p for p in run_dir.iterdir() if p.is_dir()):
        merged = {"instance_id": d.name}
        for name in ("inference.json", "grade.json", "env.json"):
            p = d / name
            if p.exists():
                data = json.loads(p.read_text())
                if name == "env.json":
                    merged["env_ok"] = data.get("ok")
                    merged["env_error"] = data.get("error", "")
                else:
                    merged.update(data)
        merged["failure_category"] = failures.classify(merged)
        rows.append(merged)
    return rows


def _avg(values) -> float:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else 0.0


def summarize(rows: list[dict], manifest: dict) -> dict:
    """算出报告要用的每一个数字。分母【固定为冻结清单的长度】。"""
    frozen = manifest.get("instance_count") or len(rows)
    resolved = [r for r in rows if r.get("resolved")]
    env_failed = [r for r in rows if r.get("env_ok") is False]
    artifacts = [r for r in rows if r.get("gold_ok") is False]
    apply_failed = [r for r in rows if r.get("patch_applied") is False]
    timeouts = [r for r in rows if r.get("failure_category") == "TIMEOUT"]
    empty = [r for r in rows if r.get("empty_patch")]
    tampering = [r for r in rows if (r.get("audit") or {}).get("suspicious")]
    # input 级命中 = 我们真的把答案喂给了 agent，这个数【必须】是 0。
    # trace 级命中只是 agent 自己产出的文本里撞了名字，单列、不混算。
    leaks = [r for r in rows if any(f.get("severity") == "input"
                                    for f in (r.get("leak_findings") or []))]
    trace_hits = [r for r in rows if any(f.get("severity") == "trace"
                                         for f in (r.get("leak_findings") or []))]

    # 声称可信度：agent 自己说"修好了"（ok=True）的实例里，官方判分真通过的
    # 比例。resolved 率是召回,这个是精确率 —— 决定人类要不要逐条复查它的产出。
    # 实测这是 Reviewer 唯一可测的贡献：带审查 3/3 全真,不带 5/14。
    # 只报 resolved 率会把这两种 agent 混成同一个数。
    claims = [r for r in rows if r.get("ok")]
    claims_true = sum(1 for r in claims if r.get("resolved"))
    quiet_resolved = sum(1 for r in rows if r.get("resolved") and not r.get("ok"))

    ledgers = [r.get("ledger") or {} for r in rows]
    reviewer = _reviewer_stats(rows)

    # 可判定分母：gold 在本环境能过的那些。两个口径都要报 ——
    # 只报对自己有利的那个，是评测里最隐蔽的一种造假。
    certifiable = [r for r in rows if r.get("gold_ok") is not False
                   and r.get("env_ok") is not False]

    return {
        "frozen_total": frozen,
        "resolved": len(resolved),
        "resolved_rate": len(resolved) / frozen if frozen else 0.0,
        "certifiable": len(certifiable),
        "resolved_on_certifiable": sum(1 for r in certifiable if r.get("resolved")),
        "claims": len(claims),
        "claims_true": claims_true,
        "quiet_resolved": quiet_resolved,
        "env_failed": len(env_failed),
        "gold_artifacts": len(artifacts),
        "apply_failed": len(apply_failed),
        "empty_patch": len(empty),
        "timeouts": len(timeouts),
        "suspicious_patches": len(tampering),
        "leak_findings": len(leaks),
        "trace_name_collisions": len(trace_hits),
        "avg_tokens": _avg(led.get("total_tokens") for led in ledgers),
        "avg_tool_calls": _avg(led.get("tool_calls") for led in ledgers),
        "avg_secs": _avg(r.get("wall_secs") for r in rows),
        "total_cost": sum(led.get("cost_usd") or 0 for led in ledgers),
        "avg_cost": _avg(led.get("cost_usd") for led in ledgers),
        "priced": all(led.get("priced", False) for led in ledgers if led),
        **reviewer,
    }


def _reviewer_stats(rows: list[dict]) -> dict:
    """Reviewer 的四个问题：驳回几次、挽救几个、误杀几个、多花多少。

    挽救 / 误杀的判定：
      挽救 = 被驳回过 且 最终 resolved     —— 驳回后那一轮把它改对了
      误杀 = 被驳回过 且 最终没 resolved 且补丁其实没问题（测试全绿）
             —— 它拦下了一个本来就该放行的补丁
    第二条只是【疑似】：真正的误杀要看那个被驳回的补丁能不能过官方判分，
    而我们只对最终补丁判了分。所以字段名叫 suspected_false_kills，不夸大。
    """
    rejected = [r for r in rows if (r.get("reviewer") or {}).get("rejections")]
    saved = [r for r in rejected if r.get("resolved")]
    false_kill = [r for r in rejected
                  if not r.get("resolved") and r.get("test_status") in
                  ("fixed", "still_green")]
    extra = sum((r.get("ledger") or {}).get("tokens_by_role", {}).get("reviewer", 0)
                for r in rows)
    return {
        "reviewer_enabled": any((r.get("reviewer") or {}).get("enabled") for r in rows),
        "reviewer_rejections": sum((r.get("reviewer") or {}).get("rejections", 0)
                                   for r in rows),
        "reviewer_saved": len(saved),
        "reviewer_suspected_false_kills": len(false_kill),
        "reviewer_tokens": extra,
    }


# ---------------------------------------------------------------------------
# 单次运行的报告
# ---------------------------------------------------------------------------
def render(run_dir: Path, rows: list[dict], manifest: dict) -> str:
    s = summarize(rows, manifest)
    cost = f"${s['avg_cost']:.4f}" if s["priced"] else "（无价格表）"
    total_cost = f"${s['total_cost']:.3f}" if s["priced"] else "—"

    out = [
        f"# 评测报告 · {manifest['run_id']}",
        "",
        f"- 变体：`{manifest['variant']}` | 划分：`{manifest['split']}` | "
        f"模型：`{manifest['model']}` T={manifest['temperature']}",
        f"- agent 版本：`{manifest['agent_commit']}` | "
        f"prompt `{manifest['prompt_hash']}` | tools `{manifest['tool_schema_hash']}` | "
        f"budget `{manifest['budget_fingerprint']}`",
        f"- 实例清单：`{manifest['instance_checksum']}`（{s['frozen_total']} 条，"
        "在推理开始前冻结，跑不动的实例仍留在分母里）",
        "",
        "## 效果与成本",
        "",
        "| 指标 | 结果 |",
        "|---|---:|",
        f"| 固定实例数 | {s['frozen_total']} |",
        f"| **Resolved** | **{s['resolved']}/{s['frozen_total']} = "
        f"{s['resolved_rate']:.0%}** |",
        f"| Resolved（仅 gold 可判定的实例） | {s['resolved_on_certifiable']}/"
        f"{s['certifiable']} |",
        f"| **声称可信度**（说\"修好了\"里真修好的） | **{s['claims_true']}/{s['claims']}"
        f"{'' if s['claims'] else '（没声称过）'}** |",
        f"| 修好了但没敢声称 | {s['quiet_resolved']} |",
        f"| 环境失败 | {s['env_failed']} |",
        f"| gold 也过不了（环境伪影） | {s['gold_artifacts']} |",
        f"| 补丁无法应用 | {s['apply_failed']} |",
        f"| 空补丁 | {s['empty_patch']} |",
        f"| 超时实例 | {s['timeouts']} |",
        f"| 平均 Token | {s['avg_tokens']:,.0f} |",
        f"| 平均工具调用 | {s['avg_tool_calls']:.1f} |",
        f"| 平均耗时 | {s['avg_secs']:.0f}s |",
        f"| 单实例平均成本 | {cost} |",
        f"| 本次总成本 | {total_cost} |",
    ]
    if s["reviewer_enabled"]:
        out += [
            f"| Reviewer 驳回次数 | {s['reviewer_rejections']} |",
            f"| Reviewer 挽救实例 | {s['reviewer_saved']} |",
            f"| Reviewer 疑似误杀 | {s['reviewer_suspected_false_kills']} |",
            f"| Reviewer 额外 Token | {s['reviewer_tokens']:,} |",
        ]
    out += [
        f"| 触发补丁策略告警的实例 | {s['suspicious_patches']} |",
        f"| **推理输入中出现官方测试标识（必须为 0）** | **{s['leak_findings']}** |",
        f"| agent 产出中撞上官方用例名（巧合，仅记录） | {s['trace_name_collisions']} |",
        "",
        "## 失败归因",
        "",
        "```",
        failures.render_tree(rows),
        "```",
        "",
        "## 逐实例",
        "",
        "| 实例 | 结果 | 失败主因 | 工具 | Token | 耗时 | 补丁告警 |",
        "|---|---|---|---:|---:|---:|---|",
    ]
    for r in sorted(rows, key=lambda x: x["instance_id"]):
        led = r.get("ledger") or {}
        audit = r.get("audit") or {}
        mark = ("✅ resolved" if r.get("resolved")
                else "⏭ env" if r.get("env_ok") is False
                else "⚠ gold 不可判定" if r.get("gold_ok") is False
                else "❌")
        flags = "；".join((audit.get("flags") or [])[:1]) or ""
        out.append(
            f"| {r['instance_id']} | {mark} | {r.get('failure_category', '')} | "
            f"{led.get('tool_calls', '')} | {led.get('total_tokens', '')} | "
            f"{r.get('wall_secs', '')} | {flags[:48]} |")

    out += ["", "## 口径声明", "",
            "- 分母是【推理开始前冻结】的实例清单，跑不起来的实例留在分母里，"
            "只在上表中单列，绝不从分母删除。",
            "- gold 校准在推理【之前】完成，结果不受 agent 表现影响。",
            "- gold patch 仅用于环境校准与修改范围的辅助分析，"
            "**不用文本相似度判定正确性** —— 同一个问题有多种正确修法。",
            "- 推理侧从未接触 test_patch / FAIL_TO_PASS / PASS_TO_PASS / gold patch；"
            "测试范围只来自 adapter 探测与仓库目录结构（见 harness/environment.py）。"]
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# 成对消融比较
# ---------------------------------------------------------------------------
def paired(rows_a: list[dict], rows_b: list[dict], label_a: str, label_b: str) -> dict:
    """在【相同实例】上做四格比较。只统计两边都跑过的实例。"""
    a = {r["instance_id"]: bool(r.get("resolved")) for r in rows_a}
    b = {r["instance_id"]: bool(r.get("resolved")) for r in rows_b}
    common = sorted(set(a) & set(b))
    cells = {"a_only": [], "b_only": [], "both": [], "neither": []}
    for iid in common:
        key = ("both" if a[iid] and b[iid] else
               "a_only" if a[iid] else
               "b_only" if b[iid] else "neither")
        cells[key].append(iid)
    return {"label_a": label_a, "label_b": label_b, "n": len(common), **cells}


def render_paired(pairs: list[dict], costs: dict[str, dict]) -> str:
    """把若干组成对比较渲染成那张表，并附上各自的成本。"""
    out = ["# 成对消融比较", "",
           "同一批实例、同一个模型、同一份预算规模，只差被消融的那一项。", "",
           "| 对比 | n | A 独赢 | B 独赢 | 同时成功 | 同时失败 |",
           "|---|---:|---:|---:|---:|---:|"]
    for p in pairs:
        out.append(f"| {p['label_a']} vs {p['label_b']} | {p['n']} | "
                   f"{len(p['a_only'])} | {len(p['b_only'])} | "
                   f"{len(p['both'])} | {len(p['neither'])} |")

    out += ["", "## 净值背后的两列", "",
            "净差 =（A 独赢 − B 独赢）。单看净差会把 **B 独赢** 那一列藏起来 —— "
            "而那一列正是「A 的这个结构反而帮了倒忙」的实例。", ""]
    for p in pairs:
        if p["b_only"]:
            out.append(f"- `{p['label_b']}` 独赢（即 `{p['label_a']}` 的额外结构"
                       f"在这些实例上是负作用）：{', '.join(p['b_only'])}")
    out += ["", "## 效果 vs 成本", "",
            "| 变体 | Resolved | 平均 Token | 平均成本 | 平均工具调用 |",
            "|---|---:|---:|---:|---:|"]
    for name, s in costs.items():
        cost = f"${s['avg_cost']:.4f}" if s["priced"] else "—"
        out.append(f"| {name} | {s['resolved']}/{s['frozen_total']} "
                   f"({s['resolved_rate']:.0%}) | {s['avg_tokens']:,.0f} | "
                   f"{cost} | {s['avg_tool_calls']:.1f} |")
    out += ["", "读这张表的方法：先看 Resolved 的差，再看 Token 的倍数。"
            "多两个点、翻一倍成本，值不值是场景问题 —— 但两个数字必须一起看。"]
    return "\n".join(out) + "\n"
