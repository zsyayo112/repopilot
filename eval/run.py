#!/usr/bin/env python3
"""评测线束的命令行入口。推理与判分是【两条命令】，不是一条命令的两个阶段。

    # 一次性：冻结实例划分（提交进 git，之后不许改）
    python eval/run.py freeze

    # 环境层（可缓存、与 agent 版本无关）：装依赖 + 跑基线 + gold 校准
    python eval/run.py prepare  --split dev
    python eval/run.py calibrate --split dev

    # 推理（不接触任何答案）
    python eval/run.py infer --split dev --variant full --run-id v0.5.0-dev

    # 判分（独立进程，此时才载入 test_patch / F2P / P2P）
    python eval/run.py grade  --run-id v0.5.0-dev

    # 报告
    python eval/run.py report --run-id v0.5.0-dev
    python eval/run.py compare --a v0.5.0-full --b v0.5.0-no-reviewer

【为什么 infer 和 grade 必须是两条命令】
不是为了好看。分成两个进程之后，推理进程的地址空间里【从来没有出现过】
test_patch 和 F2P/P2P —— 这是"不存在隐藏测试泄露"最直接的证明方式。
写在一个函数里、靠"我这一段没读那个字段"来保证，是证明不了的。

顺带的好处：判分可以在推理全部跑完之后统一做。这样"跑到一半发现某条太难，
把它从分母里去掉"这个动作在流程上就没有位置了。
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
ROOT = EVAL_DIR.parent
sys.path.insert(0, str(ROOT))

# 这几个 import 必须在 sys.path 补好之后 —— eval/ 不是安装包，
# 靠脚本自己把项目根塞进搜索路径。E402 在这里是有意的。
from harness import dataset  # noqa: E402
from harness import manifest as manifest_mod  # noqa: E402
from harness.environment import Env, env_dir, prepare  # noqa: E402
from harness.instances import PublicInstance  # noqa: E402

RUNS_DIR = EVAL_DIR / "runs"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _instances(split: str, only: list[str] | None) -> list[PublicInstance]:
    ids = dataset.load_split(split)
    if only:
        missing = [i for i in only if i not in ids]
        if missing:
            raise SystemExit(f"这些实例不在 {split} 划分里：{missing}\n"
                             "冻结清单之外的实例不允许参与评测。")
        ids = [i for i in ids if i in only]
    return dataset.load_public(ids)


def _load_env(instance_id: str) -> Env | None:
    """读缓存下来的环境记录。

    只取 Env 当前认识的字段 —— env.json 是一层【跨代码版本存活】的缓存，
    今天加一个字段、明天删一个字段，都不该让几十分钟的装机成果变成
    一个 TypeError。
    """
    stamp = env_dir(instance_id) / "env.json"
    if not stamp.exists():
        return None
    data = json.loads(stamp.read_text())
    known = set(Env.__dataclass_fields__)
    env = Env(**{k: v for k, v in data.items() if k in known and k != "repo_dir"})
    env.repo_dir = env_dir(instance_id) / "repo"
    return env


# ---------------------------------------------------------------------------
def cmd_freeze(args):
    splits = dataset.freeze_splits(force=args.force)
    for name, ids in splits.items():
        print(f"\n{name}（{len(ids)} 条）")
        for i in ids:
            print(f"  {i}")
    print(f"\n已写入 {dataset.SPLITS_DIR}/。**把它们提交进 git，之后不要再改。**")


def cmd_prepare(args):
    """环境层。这一层可以缓存、可以跨 agent 版本复用，因为它不含任何 agent 产出。"""
    for inst in _instances(args.split, args.only):
        log(f"=== {inst.instance_id} ===")
        env = prepare(inst, force=args.force, baseline_timeout=args.baseline_timeout)
        log(f"  {'✓ 就绪' if env.ok else '✗ ' + env.error[:120]}"
            + (f"  测试范围：{env.test_roots or '全量'}"
               f"  全量耗时 {env.baseline_secs}s"
               f"{'（未跑完，仅记录）' if env.baseline_timed_out else ''}" if env.ok else ""))


def cmd_calibrate(args):
    """gold 校准。**在推理之前跑** —— 事后跑就有了挑实例的空间。"""
    from harness import grader
    from harness.dataset import load_secret

    insts = _instances(args.split, args.only)
    secrets = load_secret([i.instance_id for i in insts])
    for inst in insts:
        env = _load_env(inst.instance_id)
        if env is None or not env.ok:
            log(f"{inst.instance_id}: 环境未就绪，跳过校准（仍留在分母里）")
            continue
        log(f"=== {inst.instance_id} gold 校准 ===")
        out = grader.calibrate_gold(env, secrets[inst.instance_id],
                                    timeout=args.timeout)
        stamp = env_dir(inst.instance_id) / "gold.json"
        stamp.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"  gold_ok = {out['gold_ok']}  {out['gold_note'][:100]}")


def cmd_infer(args):
    """推理。这个进程里【不会】出现 test_patch / F2P / P2P。"""
    # 必须在 import repopilot 之前设 —— config.py 在导入时就把它读成常量了。
    # 为什么要调大：测试范围不再从 test_patch 抠（那是泄露），改跑仓库自己的
    # 测试目录，于是 seaborn 这类项目一轮全量套件要好几分钟。默认 360 秒会把
    # 它们一律判超时 —— 那等于"堵住泄露"的代价是"这些实例全废"。
    os.environ.setdefault("REPOPILOT_TEST_TIMEOUT", str(args.test_timeout))

    from harness.inference import VARIANTS, budget_for, run_one

    from repopilot.budget import Budget

    if args.variant not in VARIANTS:
        raise SystemExit(f"未知变体 {args.variant}，可选：{', '.join(VARIANTS)}")

    insts = _instances(args.split, args.only)
    base = Budget(model=args.model, token_budget=args.token_budget,
                  max_tool_calls=args.max_tool_calls,
                  instance_timeout=args.timeout)
    budget = budget_for(args.variant, base)

    run_dir = RUNS_DIR / args.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    # 子进程【不写】manifest。它带的是 --only 一条实例，实例清单校验和当然和
    # 整批不同，于是不可变守卫会（正确地）把它拦下。子进程是同一次运行的一个
    # 切片，不是另一次实验 —— 权威 manifest 由父进程写一次。
    # 守卫本身不放松：它挡的是"同一个 run_id 跑两套不同配置"，那条依然成立。
    if not args.child:
        mani = manifest_mod.build(
            run_id=args.run_id, split=args.split,
            instance_ids=[i.instance_id for i in insts], budget=budget,
            variant=args.variant, dataset=dataset.dataset_info(), notes=args.notes)
        manifest_mod.write(run_dir, mani)
        log(f"manifest 已冻结：{run_dir/'manifest.json'}")
        print(f"  agent={mani['agent_commit']} prompt={mani['prompt_hash']} "
              f"tools={mani['tool_schema_hash']} budget={mani['budget_fingerprint']}")

    if args.workers > 1:
        return _infer_parallel(args, insts, run_dir)

    for inst in insts:
        out_dir = run_dir / inst.instance_id
        if (out_dir / "inference.json").exists() and not args.force:
            log(f"{inst.instance_id}: 已跑过，跳过（--force 可重跑）")
            continue
        env = _load_env(inst.instance_id)
        if env is None or not env.ok:
            # 环境失败的实例【仍然留在分母里】，只是没有推理结果。
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "env.json").write_text(json.dumps(
                (env.to_dict() if env else {"ok": False, "error": "未 prepare"}),
                ensure_ascii=False, indent=2), encoding="utf-8")
            log(f"{inst.instance_id}: 环境未就绪，记为环境失败（仍在分母里）")
            continue
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "env.json").write_text(
            json.dumps(env.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

        log(f"=== {inst.instance_id} [{args.variant}] ===")
        res = run_one(inst, env, variant=args.variant, budget=budget, out_dir=out_dir)
        led = res.get("ledger") or {}
        log(f"  → halt={res.get('halt_code') or '正常'} "
            f"patch={res.get('patch_bytes', 0)}B "
            f"tokens={led.get('total_tokens', 0)} {res.get('wall_secs')}s")


def _infer_parallel(args, insts, run_dir):
    """按【实例】并发。每个实例一个子进程，输出各自落盘。

    【为什么并发是安全的】每个实例有自己的 venv 和工作树，彼此之间没有共享
    状态。真正不能并发的是**同一实例的不同变体** —— 它们共用同一个工作树，
    而每次推理前后都要把工作树复位。所以并发只切实例这一维，
    变体之间靠"分开跑 infer"天然串行。

    【为什么用子进程而不是线程】solve() 会往 stdout 打一大片人看的过程输出。
    多线程会把几个实例的输出搅在一起，事后没法读。子进程各自重定向到
    <实例>/infer.log，既隔离了输出，也隔离了崩溃。

    【为什么默认并发度这么低】瓶颈是内存不是 CPU：本机 12 核但只有 7GB，
    而 seaborn 单次 pytest 峰值 1.4GB。开满核会 OOM，OOM 出来的失败会被
    记成实例失败 —— 又是一次"评测者自己造成的失败记到被测方案头上"。
    """
    import subprocess
    from concurrent.futures import ThreadPoolExecutor

    pending = [i for i in insts
               if args.force or not (run_dir / i.instance_id / "inference.json").exists()]
    skipped = len(insts) - len(pending)
    if skipped:
        log(f"{skipped} 条已跑过，跳过（--force 可重跑）")
    log(f"并发 {args.workers} 跑 {len(pending)} 条")

    def one(inst):
        out_dir = run_dir / inst.instance_id
        out_dir.mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, str(Path(__file__).resolve()), "infer",
               "--split", args.split, "--variant", args.variant,
               "--run-id", args.run_id, "--only", inst.instance_id,
               "--model", args.model, "--token-budget", str(args.token_budget),
               "--max-tool-calls", str(args.max_tool_calls),
               "--timeout", str(args.timeout),
               "--test-timeout", str(args.test_timeout), "--workers", "1",
               "--child"]
        if args.force:
            cmd.append("--force")
        with (out_dir / "infer.log").open("w", encoding="utf-8") as f:
            code = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT).returncode
        res = out_dir / "inference.json"
        if res.exists():
            d = json.loads(res.read_text())
            return (f"{inst.instance_id}: halt={d.get('halt_code') or '正常'} "
                    f"patch={d.get('patch_bytes', 0)}B "
                    f"tokens={(d.get('ledger') or {}).get('total_tokens', 0)} "
                    f"{d.get('wall_secs')}s")
        return f"{inst.instance_id}: 子进程 exit={code} 且没有结果，见 infer.log"

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for line in pool.map(one, pending):
            log("  → " + line)


def cmd_grade(args):
    """判分。到这一步才 import grader —— 答案在这里才第一次被载入。"""
    from harness import grader
    from harness.dataset import load_secret

    run_dir = RUNS_DIR / args.run_id
    mani = manifest_mod.load(run_dir)
    secrets = load_secret(mani["instance_ids"])

    for iid in mani["instance_ids"]:
        out_dir = run_dir / iid
        if not (out_dir / "inference.json").exists():
            continue        # 环境失败的实例没有补丁可判，报告里单列
        if (out_dir / "grade.json").exists() and not args.force:
            continue
        env = _load_env(iid)
        if env is None or not env.ok:
            continue
        log(f"=== {iid} 判分 ===")
        patch = (out_dir / "agent.patch").read_text(encoding="utf-8")
        res = grader.grade_patch(env, secrets[iid], patch, timeout=args.timeout)

        # gold 校准结果并进来（它是 prepare/calibrate 阶段的产物）
        gold_file = env_dir(iid) / "gold.json"
        if gold_file.exists():
            g = json.loads(gold_file.read_text())
            res.gold_ok, res.gold_note = g.get("gold_ok"), g.get("gold_note", "")

        # 事后泄露扫描：轨迹里出现过隐藏测试的 id 或路径吗
        res.leak_findings = grader.audit_inference(out_dir, secrets[iid])
        grader.save(out_dir, res)
        flag = "✅" if res.resolved else "❌"
        log(f"  {flag} resolved={res.resolved} gold_ok={res.gold_ok} "
            f"{res.note[:90]}")


def cmd_report(args):
    from harness import report

    run_dir = RUNS_DIR / args.run_id
    mani = manifest_mod.load(run_dir)
    rows = report.load_rows(run_dir)
    text = report.render(run_dir, rows, mani)
    (run_dir / "REPORT.md").write_text(text, encoding="utf-8")
    print(text)
    log(f"已写入 {run_dir/'REPORT.md'}")


def cmd_compare(args):
    """成对消融：同一批实例上比两次运行。"""
    from harness import report

    runs = [args.a, args.b, *(args.extra or [])]
    loaded = {}
    for rid in runs:
        d = RUNS_DIR / rid
        mani = manifest_mod.load(d)
        loaded[rid] = (mani, report.load_rows(d))

    base_mani = loaded[args.a][0]
    for rid, (mani, _) in loaded.items():
        ok, notes = manifest_mod.comparable(base_mani, mani)
        if notes:
            print(f"{'!' if not ok else '·'} {args.a} vs {rid}：{'；'.join(notes)}")
        if not ok:
            raise SystemExit("上面标 ! 的差异会让比较失去意义，已停止。")

    pairs = [report.paired(loaded[args.a][1], loaded[rid][1],
                           loaded[args.a][0]["variant"], loaded[rid][0]["variant"])
             for rid in runs[1:]]
    costs = {mani["variant"]: report.summarize(rows, mani)
             for mani, rows in loaded.values()}
    text = report.render_paired(pairs, costs)
    out = RUNS_DIR / f"COMPARE-{args.a}.md"
    out.write_text(text, encoding="utf-8")
    print(text)
    log(f"已写入 {out}")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("freeze", help="生成冻结的 dev/test/holdout 划分")
    f.add_argument("--force", action="store_true", help="重新抽样（会作废此前所有结果）")

    for name, help_text in (("prepare", "装环境 + 跑基线（可缓存）"),
                            ("calibrate", "gold 校准（必须在 infer 之前）")):
        c = sub.add_parser(name, help=help_text)
        c.add_argument("--split", default="dev", choices=("dev", "test", "holdout"))
        c.add_argument("--only", nargs="*")
        c.add_argument("--force", action="store_true")
        c.add_argument("--timeout", type=int, default=1800)
        c.add_argument("--baseline-timeout", type=int, default=900,
                       help="量全量套件耗时的上限。超了只记录，不判死实例")

    i = sub.add_parser("infer", help="推理（不接触任何答案）")
    i.add_argument("--split", default="dev", choices=("dev", "test", "holdout"))
    i.add_argument("--variant", default="full")
    i.add_argument("--run-id", required=True)
    i.add_argument("--only", nargs="*")
    i.add_argument("--force", action="store_true")
    i.add_argument("--model", default="deepseek-chat")
    i.add_argument("--token-budget", type=int, default=600_000)
    i.add_argument("--max-tool-calls", type=int, default=100)
    i.add_argument("--timeout", type=int, default=1800)
    i.add_argument("--test-timeout", type=int, default=900,
                   help="agent 单次跑测试的超时（秒）。全量套件比 scope 过的慢得多")
    i.add_argument("--child", action="store_true",
                   help="内部使用：由并发启动器设置，表示「我是整批里的一个切片」，"
                        "因此不重写 manifest（权威 manifest 由父进程写一次）")
    i.add_argument("--workers", type=int, default=1,
                   help="并发跑几个实例。瓶颈是内存不是 CPU（seaborn 单次 pytest "
                        "峰值 1.4GB），本机 7GB 内存建议不超过 3")
    i.add_argument("--notes", default="")

    g = sub.add_parser("grade", help="判分（独立进程，此时才载入答案）")
    g.add_argument("--run-id", required=True)
    g.add_argument("--force", action="store_true")
    g.add_argument("--timeout", type=int, default=1800)

    r = sub.add_parser("report", help="生成单次运行的报告")
    r.add_argument("--run-id", required=True)

    c = sub.add_parser("compare", help="成对消融比较")
    c.add_argument("--a", required=True)
    c.add_argument("--b", required=True)
    c.add_argument("--extra", nargs="*")

    args = p.parse_args()
    {"freeze": cmd_freeze, "prepare": cmd_prepare, "calibrate": cmd_calibrate,
     "infer": cmd_infer, "grade": cmd_grade, "report": cmd_report,
     "compare": cmd_compare}[args.cmd](args)


if __name__ == "__main__":
    main()
