#!/usr/bin/env python3
"""用【官方 SWE-bench 判分器】在官方 Docker 镜像里重判已有的补丁。

    # 先做官方 gold 校准（容器里标准答案应当全过 —— 这是判分器的体检）
    python eval/official_regrade.py --gold

    # 重判若干次运行的全部补丁（推理不重跑,只重判）
    python eval/official_regrade.py --run-ids v0.5.1-dev-full-r1 v0.5.1-dev-full-r2 ...

【为什么要有这个文件】
本地 venv 判分废掉了 15 条里的 6 条：4 条环境装不起来、2 条连官方标准答案
都跑不过（gold_ok=False）。判分器不可信的题上,任何 resolved 数字都是虚的。
官方镜像把每道题的环境钉死在它自己的年代（老 Python、老依赖）,判分结果
才能和 SWE-bench 排行榜同一口径。

【为什么"重判"而不是"重跑"】
补丁已经在 eval/runs/<run_id>/<instance>/agent.patch 里了。判分和推理本来
就是分离的两个阶段（见 run.py 文件头）,换判分器不需要动推理结果 ——
这正是当初把两者拆开的红利。

【输出】
- 官方判分器的报告落在 --report-dir（默认 eval/runs/official/）
- 每条实例的官方结论回写到 <run_dir>/<instance>/official_grade.json
- 最后打一张"本地判分 vs 官方判分"的分歧表 —— 分歧本身就是本地判分器
  误差的直接测量。
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
RUNS_DIR = EVAL_DIR / "runs"
OFFICIAL_DIR = RUNS_DIR / "official"
DATASET = "princeton-nlp/SWE-bench_Verified"
PY = sys.executable


def split_instance_ids(split: str) -> list[str]:
    return json.loads((EVAL_DIR / "splits" / f"verified_{split}.json")
                      .read_text())["instance_ids"]


def build_predictions(run_id: str) -> Path:
    """把一次运行的所有 agent.patch 打包成官方 predictions.jsonl。

    环境失败的实例没有补丁,自然缺席 —— 它们在我们自己的报告里仍占分母,
    这里只是没有东西可判。
    """
    OFFICIAL_DIR.mkdir(parents=True, exist_ok=True)
    out = OFFICIAL_DIR / f"{run_id}.predictions.jsonl"
    rows = []
    for inst_dir in sorted((RUNS_DIR / run_id).iterdir()):
        patch = inst_dir / "agent.patch"
        if inst_dir.is_dir() and patch.exists():
            rows.append({"instance_id": inst_dir.name,
                         "model_name_or_path": run_id,
                         "model_patch": patch.read_text(encoding="utf-8")})
    out.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                   encoding="utf-8")
    print(f"[{run_id}] {len(rows)} 份补丁 → {out}")
    return out, [r["instance_id"] for r in rows]


def run_official(predictions: str, run_id: str, ids: list[str], workers: int) -> None:
    """调官方 harness。namespace=swebench 表示拉 Docker Hub 上的预构建镜像,
    不在本机现场构建（构建要的内存这台机器付不起）。"""
    cmd = [PY, "-m", "swebench.harness.run_evaluation",
           "--dataset_name", DATASET,
           "--predictions_path", str(predictions),
           "--instance_ids", *ids,
           "--run_id", run_id,
           "--namespace", "swebench",
           "--max_workers", str(workers),
           "--cache_level", "instance",     # 镜像留着,15 条题要反复用
           "--report_dir", str(OFFICIAL_DIR)]
    print("$", " ".join(cmd))
    subprocess.run(cmd, check=False, cwd=str(OFFICIAL_DIR))


def collect(run_id: str) -> dict:
    """读官方报告,回写 official_grade.json,返回 {instance_id: resolved}。"""
    reports = sorted(OFFICIAL_DIR.glob(f"*{run_id}*.json"),
                     key=lambda p: p.stat().st_mtime)
    if not reports:
        print(f"[{run_id}] 找不到官方报告 —— 判分大概率没跑完,看上面的日志")
        return {}
    rep = json.loads(reports[-1].read_text())
    resolved = set(rep.get("resolved_ids") or [])
    judged = set(rep.get("submitted_ids") or []) - set(rep.get("error_ids") or [])
    out = {}
    for inst_dir in sorted((RUNS_DIR / run_id).iterdir()):
        if not inst_dir.is_dir() or not (inst_dir / "agent.patch").exists():
            continue
        iid = inst_dir.name
        verdict = {"resolved": iid in resolved,
                   "judged": iid in judged,
                   "judge": "swebench-official-docker",
                   "report": reports[-1].name}
        (inst_dir / "official_grade.json").write_text(
            json.dumps(verdict, ensure_ascii=False, indent=2), encoding="utf-8")
        out[iid] = verdict
    return out


def collect_gold(ids: list[str], run_id: str) -> None:
    """gold 校准结果按实例落盘到 eval/envs/<iid>/official_gold.json。

    这是 report/repeats 的 certifiable 门以后读的东西：可判性从此是
    【官方判分器的测量结果】,不是本地 venv 的,更不是 commit message 里的论断。
    """
    reports = sorted(OFFICIAL_DIR.glob(f"*{run_id}*.json"),
                     key=lambda p: p.stat().st_mtime)
    if not reports:
        print(f"[{run_id}] 找不到官方 gold 报告 —— 校准没跑完,看上面的日志")
        return
    rep = json.loads(reports[-1].read_text())
    resolved = set(rep.get("resolved_ids") or [])
    judged = set(rep.get("submitted_ids") or []) - set(rep.get("error_ids") or [])
    ok = 0
    for iid in ids:
        verdict = {"gold_ok": iid in resolved, "judged": iid in judged,
                   "judge": "swebench-official-docker", "report": reports[-1].name}
        ok += verdict["gold_ok"]
        d = EVAL_DIR / "envs" / iid
        d.mkdir(parents=True, exist_ok=True)
        (d / "official_gold.json").write_text(
            json.dumps(verdict, ensure_ascii=False, indent=2), encoding="utf-8")
        if not verdict["gold_ok"]:
            print(f"  ✗ gold 不过：{iid}（官方判分器在这条上不可信,报告单列）")
    print(f"[{run_id}] 官方 gold 校准：{ok}/{len(ids)} 过,"
          f"结果已写入 eval/envs/<iid>/official_gold.json")


def diff_table(run_id: str, official: dict) -> None:
    """本地判分 vs 官方判分的分歧表。分歧 = 本地判分器误差的直接测量。"""
    print(f"\n== {run_id}: 本地 vs 官方 ==")
    agree = disagree = 0
    for iid, v in sorted(official.items()):
        g = (RUNS_DIR / run_id / iid / "grade.json")
        local = json.loads(g.read_text()).get("resolved") if g.exists() else None
        mark = "一致" if bool(local) == v["resolved"] else "**分歧**"
        if bool(local) == v["resolved"]:
            agree += 1
        else:
            disagree += 1
            print(f"  {iid}: 本地={local} 官方={v['resolved']}  {mark}")
    print(f"  一致 {agree} / 分歧 {disagree}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-ids", nargs="*", default=[])
    ap.add_argument("--gold", action="store_true",
                    help="官方 gold 校准：容器里跑标准答案,应当全过")
    ap.add_argument("--workers", type=int, default=2,
                    help="并发判分数。镜像各自独立,瓶颈在内存,8GB 机器别超过 2")
    ap.add_argument("--split", default="dev", choices=("dev", "test", "holdout"),
                    help="--gold 校准哪个划分（重判不需要它：实例清单从 run 目录推导）")
    args = ap.parse_args()

    if args.gold:
        # 'gold' 是官方 harness 的保留字：用数据集自带的标准答案当预测
        ids = split_instance_ids(args.split)
        rid = f"gold-calibration-{args.split}"
        run_official("gold", rid, ids, args.workers)
        collect_gold(ids, rid)
        return

    for rid in args.run_ids:
        preds, ids = build_predictions(rid)
        if not ids:
            print(f"[{rid}] 没有补丁可判,跳过")
            continue
        run_official(str(preds), rid, ids, args.workers)
        diff_table(rid, collect(rid))


if __name__ == "__main__":
    main()
