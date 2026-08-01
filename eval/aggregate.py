#!/usr/bin/env python3
"""聚合重复实验：多数票 + 稳定度 + 自带误差棒。

    python eval/aggregate.py --a v0.5.1-dev-full-r1 v0.5.1-dev-full-r2 v0.5.1-dev-full-r3 \
                             --b v0.5.1-dev-react-r1 v0.5.1-dev-react-r2 v0.5.1-dev-react-r3 \
                             --label-a full --label-b baseline-b
"""

import argparse
import sys
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(EVAL_DIR.parent))

from harness import repeats  # noqa: E402

RUNS_DIR = EVAL_DIR / "runs"


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--a", nargs="+", required=True, help="配置 A 的多次 run-id")
    p.add_argument("--b", nargs="+", help="配置 B 的多次 run-id")
    p.add_argument("--label-a", default="A")
    p.add_argument("--label-b", default="B")
    p.add_argument("--out", default=str(RUNS_DIR / "REPEATS.md"))
    args = p.parse_args()

    agg_a = repeats.aggregate(repeats.load_repeat(args.a, RUNS_DIR))
    blocks = ["# 重复实验聚合", "",
              "单次运行的逐实例翻转率约 22%（首轮 sweep 用阴性对照测得）。"
              "重复取多数票把它压下去，并让每个配置**自带一根误差棒** —— "
              "任何小于它的效应量都不该当成结论。", "",
              repeats.render(args.label_a, agg_a)]
    if args.b:
        agg_b = repeats.aggregate(repeats.load_repeat(args.b, RUNS_DIR))
        blocks += ["", repeats.render(args.label_b, agg_b),
                   "", repeats.compare(agg_a, agg_b, args.label_a, args.label_b)]
    text = "\n".join(blocks) + "\n"
    Path(args.out).write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
