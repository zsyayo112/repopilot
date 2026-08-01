"""重复实验的聚合：把单次运行的抖动压下去，让效应量露出来。

【为什么必须有这一层】
首轮 sweep 测出一个硬事实：两个【行为完全等价】的配置
（唯一差别是一个从头到尾没被调用过的工具），resolved 在 9 条里翻了 2 条。
也就是说单次运行的逐实例翻转率约 22%。

在这个底噪上，任何"某某机制让成功率 +1 条"的说法都读不出来 ——
成对比较消得掉实例难度的差异，**消不掉运行间方差**。

唯一的办法是重复：同一配置跑 R 次，每条实例取**多数票**。
R=3 时，一条实例要被误判，需要连续两次抖到同一边，概率从 22% 掉到约 5%。

【多数票之外还要报什么】
只报多数票会把"这条题目它稳定做对"和"这条题目它三次里勉强对了两次"
混成同一个 ✅。所以每条实例同时报 **稳定度**（R 次里成功了几次）：

    3/3  稳定成功
    2/3  多数成功，但不稳
    1/3  多数失败，但不是完全不会
    0/3  稳定失败

稳定度这一列比多数票本身更有信息量 —— 它直接告诉你哪些实例值得再看。
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path


def load_repeat(run_ids: list[str], runs_dir: Path) -> dict[str, list[dict]]:
    """读若干次重复运行，返回 instance_id → [每次的记录]。"""
    per_instance: dict[str, list[dict]] = {}
    for rid in run_ids:
        d = runs_dir / rid
        if not d.exists():
            continue
        for p in sorted(x for x in d.iterdir() if x.is_dir()):
            rec: dict = {"run_id": rid}
            for name in ("inference.json", "grade.json", "env.json"):
                f = p / name
                if f.exists():
                    data = json.loads(f.read_text())
                    if name == "env.json":
                        rec["env_ok"] = data.get("ok")
                    else:
                        rec.update(data)
            per_instance.setdefault(p.name, []).append(rec)
    return per_instance


def aggregate(per_instance: dict[str, list[dict]]) -> dict[str, dict]:
    """每条实例聚合成：多数票 + 稳定度 + 成本中位数。

    成本取【中位数】而不是平均：单次跑飞（撞上限、超时）会把平均拉走，
    而我们想知道的是"典型情况要花多少"。
    """
    out = {}
    for iid, runs in per_instance.items():
        resolved = [bool(r.get("resolved")) for r in runs if "resolved" in r]
        toks = [(r.get("ledger") or {}).get("total_tokens", 0) for r in runs]
        cost = [(r.get("ledger") or {}).get("cost_usd", 0) for r in runs]
        secs = [r.get("wall_secs", 0) for r in runs]
        n = len(resolved)
        wins = sum(resolved)
        out[iid] = {
            "n": n,
            "wins": wins,
            "majority": bool(n and wins * 2 > n),
            "stable": n > 0 and (wins == n or wins == 0),
            # 声称可信度的分子分母。跨 R 次累加 —— 单轮的 1/1 说明不了什么，
            # 聚合后它才第一次有统计上的分量。
            "claims": sum(1 for r in runs if r.get("ok")),
            "claim_hits": sum(1 for r in runs if r.get("ok") and r.get("resolved")),
            "gold_ok": all(r.get("gold_ok") is not False for r in runs),
            "env_ok": all(r.get("env_ok") is not False for r in runs),
            "median_tokens": int(statistics.median(toks)) if toks else 0,
            "median_cost": statistics.median(cost) if cost else 0.0,
            "median_secs": statistics.median(secs) if secs else 0.0,
            "halt_codes": [r.get("halt_code") or "正常" for r in runs],
        }
    return out


def flip_rate(agg: dict[str, dict]) -> float:
    """不稳定实例的比例 —— 这就是这个配置自己的噪声底噪。

    它是一条【自带的】误差棒：不需要另做阴性对照，重复实验本身就报出了
    自己的分辨率。任何小于它的效应量都不该被当成结论。
    """
    usable = [v for v in agg.values() if v["n"] > 1 and v["gold_ok"] and v["env_ok"]]
    if not usable:
        return 0.0
    return sum(not v["stable"] for v in usable) / len(usable)


def render(name: str, agg: dict[str, dict]) -> str:
    cert = {k: v for k, v in agg.items() if v["gold_ok"] and v["env_ok"]}
    maj = sum(v["majority"] for v in cert.values())
    claims = sum(v.get("claims", 0) for v in cert.values())
    claim_hits = sum(v.get("claim_hits", 0) for v in cert.values())
    lines = [
        f"### {name}",
        "",
        f"- 多数票 resolved：**{maj}/{len(cert)}**（gold 可判定实例）",
        f"- 不稳定实例（R 次里有输有赢）：{sum(not v['stable'] for v in cert.values())}"
        f"/{len(cert)} → 本配置自身噪声 {flip_rate(agg):.0%}",
        f"- 声称可信度：**{claim_hits}/{claims}**（说\"修好了\"里真修好的；"
        "resolved 是召回,这个是精确率,决定产出能不能免复查直接用）"
        if claims else "- 声称可信度：—（R 次里从未声称过修好）",
        f"- 中位 token：{int(statistics.median([v['median_tokens'] for v in cert.values()])):,}"
        if cert else "",
        "",
        "| 实例 | 稳定度 | 多数票 | 中位 token | 中位耗时 |",
        "|---|:---:|:---:|---:|---:|",
    ]
    for k, v in sorted(cert.items()):
        mark = "✅" if v["majority"] else "❌"
        flag = "" if v["stable"] else "  ⚠不稳"
        lines.append(f"| {k} | {v['wins']}/{v['n']}{flag} | {mark} | "
                     f"{v['median_tokens']:,} | {v['median_secs']:.0f}s |")
    return "\n".join(lines)


def compare(agg_a: dict, agg_b: dict, label_a: str, label_b: str) -> str:
    """两个配置的多数票成对比较，并【和各自的噪声底噪并排给出】。

    这是本模块存在的全部意义：一个效应量旁边必须有它的误差棒，
    否则读者无从判断它是不是真的。
    """
    common = [k for k in sorted(set(agg_a) & set(agg_b))
              if agg_a[k]["gold_ok"] and agg_b[k]["gold_ok"]
              and agg_a[k]["env_ok"] and agg_b[k]["env_ok"]]
    a_only = [k for k in common if agg_a[k]["majority"] and not agg_b[k]["majority"]]
    b_only = [k for k in common if agg_b[k]["majority"] and not agg_a[k]["majority"]]
    both = [k for k in common if agg_a[k]["majority"] and agg_b[k]["majority"]]
    neither = [k for k in common if not agg_a[k]["majority"] and not agg_b[k]["majority"]]

    noise = max(flip_rate(agg_a), flip_rate(agg_b)) * len(common)
    effect = abs(len(a_only) - len(b_only))
    verdict = ("**效应量小于噪声，测不出差异**" if effect <= noise
               else f"**效应量 {effect} 条 > 噪声 {noise:.1f} 条，差异可信**")

    ta = statistics.median([agg_a[k]["median_tokens"] for k in common]) if common else 0
    tb = statistics.median([agg_b[k]["median_tokens"] for k in common]) if common else 0

    def _prec(agg):
        c = sum(agg[k].get("claims", 0) for k in common)
        h = sum(agg[k].get("claim_hits", 0) for k in common)
        return f"{h}/{c}" if c else "—"

    return "\n".join([
        f"### {label_a} vs {label_b}（多数票，n={len(common)}）",
        "",
        "| | 独赢 | 同赢 | 同败 | 多数票 resolved | 声称可信度 | 中位 token |",
        "|---|---:|---:|---:|---:|---:|---:|",
        f"| {label_a} | {len(a_only)} | {len(both)} | {len(neither)} | "
        f"{len(a_only) + len(both)}/{len(common)} | {_prec(agg_a)} | {ta:,.0f} |",
        f"| {label_b} | {len(b_only)} | {len(both)} | {len(neither)} | "
        f"{len(b_only) + len(both)}/{len(common)} | {_prec(agg_b)} | {tb:,.0f} |",
        "",
        f"噪声底噪（两者中较大的不稳定率 × n）：**{noise:.1f} 条**",
        f"结论：{verdict}",
        "",
        "声称可信度没有误差棒问题 —— 它不比较两配置的 resolve 差,只统计"
        "各自说过的话有几句是真的,分母是声称次数而不是实例数。",
        "",
        (f"- `{label_a}` 独赢：{', '.join(a_only) or '无'}"),
        (f"- `{label_b}` 独赢：{', '.join(b_only) or '无'}"),
    ])
