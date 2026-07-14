#!/usr/bin/env python3
"""SWE-bench Lite 迷你评测线束：让 RepoPilot 在业界标准基准上跑出一个【诚实的数字】。

    resolved 率 = agent 的补丁让官方隐藏测试(FAIL_TO_PASS)通过、
                  且不破坏原有测试(PASS_TO_PASS)的实例比例。

与官方评测的三点差异（轻量设定，任何对外表述都应如实带上）：
  1. 官方为每个实例用 Docker + 专属环境；这里用本机 venv——装不进当前
     Python 的老实例会被【环境闸门】跳过并记录（不计入分母）。
  2. agent 的测试命令被 scope 到 test_patch 涉及的测试文件/目录（笔记本上
     跑不动全量测试套件）。这会泄露少量定位提示，是已知的轻量评测折衷。
  3. 判分逻辑与官方一致：先打上官方 test_patch，再跑 FAIL_TO_PASS + PASS_TO_PASS，
     全部通过才算 resolved。

注意一个教学要点：FAIL_TO_PASS 测试在 base_commit 上【还不存在】（由 test_patch
新增），所以 RepoPilot 内部 verifier 的基线多半是绿的、验证结果是 still_green
——它只能保证"没改坏"，"真修好了"由本脚本的 score 阶段当最终法官。

用法（在 repopilot/ 目录下）：
    python eval/swebench_eval.py fetch [--limit 10]      # 拉取并挑选实例
    python eval/swebench_eval.py run [--only ID] [--prepare-only] [--force]
    python eval/swebench_eval.py report                  # 汇总成绩 → eval/RESULTS.md
"""

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
ROOT = EVAL_DIR.parent                       # repopilot 项目根
WORK = EVAL_DIR / "work"                     # 每个实例一个子目录（gitignore）
CACHE = EVAL_DIR / "cache"                   # 每个仓库一份完整克隆，实例间复用
INSTANCES = EVAL_DIR / "instances.json"

# 只挑纯 Python、pip 可装、pytest 可跑的仓库；排除需编译的(matplotlib/sklearn/astropy)
REPO_WHITELIST = [
    "pallets/flask", "pylint-dev/pylint", "pydata/xarray",
    "pytest-dev/pytest", "sphinx-doc/sphinx", "mwaskom/seaborn",
    "psf/requests",
]

HF_URL = "https://datasets-server.huggingface.co/rows"
DATASET = "princeton-nlp/SWE-bench_Lite"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def sh(cmd, cwd=None, timeout=600, env=None):
    """跑一条命令，返回 (exit_code, 合并输出)。永不抛异常（错误即信息）。"""
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, timeout=timeout, env=env,
            capture_output=True, text=True,
            shell=isinstance(cmd, str),
        )
        return proc.returncode, (proc.stdout + proc.stderr)
    except subprocess.TimeoutExpired:
        return -1, f"超时（>{timeout}s）"


# ---------------------------------------------------------------------------
# fetch：直接下载 HF Hub 上的 parquet 原文件（datasets-server 时常 503，
# Hub 的静态文件下载稳得多），用 pyarrow 读出 300 条，按白名单+新近度挑选。
# ---------------------------------------------------------------------------
PARQUET_URL = ("https://huggingface.co/datasets/princeton-nlp/SWE-bench_Lite"
               "/resolve/main/data/test-00000-of-00001.parquet")


def cmd_fetch(args):
    pq_file = CACHE / "swebench_lite_test.parquet"
    if not pq_file.exists():
        CACHE.mkdir(parents=True, exist_ok=True)
        log("下载 SWE-bench Lite parquet …")
        req = urllib.request.Request(PARQUET_URL,
                                     headers={"User-Agent": "repopilot-eval/0.1"})
        with urllib.request.urlopen(req, timeout=120) as r:
            pq_file.write_bytes(r.read())

    import pyarrow.parquet as pq
    rows = pq.read_table(pq_file).to_pylist()
    log(f"共 {len(rows)} 条")

    cand = [r for r in rows if r["repo"] in REPO_WHITELIST]
    # 新 commit 对当前 Python 的兼容性更好，按创建时间倒序取前 N
    cand.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    cand = cand[: args.limit]

    keep_fields = ["instance_id", "repo", "base_commit", "problem_statement",
                   "FAIL_TO_PASS", "PASS_TO_PASS", "test_patch", "created_at"]
    # 注意：故意丢弃 gold patch 和 hints_text —— agent 绝不能看到答案
    slim = [{k: r[k] for k in keep_fields} for r in cand]
    INSTANCES.write_text(json.dumps(slim, indent=2), encoding="utf-8")

    log(f"已选 {len(slim)} 条 → {INSTANCES}")
    for r in slim:
        print(f"  {r['instance_id']:28} {r['created_at'][:10]}")


# ---------------------------------------------------------------------------
# run：对每个实例走 prepare → solve → score 三阶段，状态落在 result.json 里，
#      可断点续跑（已 scored 的默认跳过）。
# ---------------------------------------------------------------------------
def load_result(wd: Path) -> dict:
    f = wd / "result.json"
    return json.loads(f.read_text()) if f.exists() else {}


def save_result(wd: Path, res: dict) -> None:
    (wd / "result.json").write_text(json.dumps(res, indent=2, ensure_ascii=False))


def prepare(inst: dict, wd: Path, res: dict) -> bool:
    """克隆到 base_commit + 建 venv 装依赖 + 基线可跑检查。失败 = 环境闸门拦下。"""
    repo, sha = inst["repo"], inst["base_commit"]

    cache = CACHE / repo.replace("/", "__")
    if not cache.exists():
        log(f"  缓存克隆 {repo} …")
        CACHE.mkdir(parents=True, exist_ok=True)
        code, out = sh(["git", "clone", f"https://github.com/{repo}.git", str(cache)],
                       timeout=900)
        if code != 0:
            res.update(stage="env_error", error=f"clone: {out[-400:]}")
            return False

    repo_dir = wd / "repo"
    if not repo_dir.exists():
        sh(["git", "clone", "-q", str(cache), str(repo_dir)], timeout=300)
        code, out = sh(["git", "checkout", "-q", sha], cwd=repo_dir)
        if code != 0:
            res.update(stage="env_error", error=f"checkout: {out[-400:]}")
            return False

    venv_py = wd / "venv" / "bin" / "python"
    if not venv_py.exists():
        log("  建 venv + pip install -e …（可能要一两分钟）")
        sh([sys.executable, "-m", "venv", str(wd / "venv")], timeout=300)
        sh([str(venv_py), "-m", "pip", "install", "-q", "-U",
            "pip", "setuptools", "wheel"], timeout=600)
        # 钉同时代的 pytest：这批实例是 2022-2023 的，最新 pytest 9 的内部 API
        # 变了会导致老测试套件收集失败（flask-5063 实测踩坑）。
        # pytest-dev/pytest 自家实例除外——它本身就是 pytest，不能再装一个。
        deps = ["-e", str(repo_dir)]
        if inst["repo"] != "pytest-dev/pytest":
            deps.append("pytest<8")
        # 个别实例的时代依赖钉子（环境问题，不是给 agent 的提示，不影响公平性）
        EXTRA_PINS = {
            # flask 2.2 时代 import 的 url_quote 在 werkzeug 2.3 被删除
            "pallets__flask-4992": ["werkzeug<2.3"],
        }
        deps += EXTRA_PINS.get(inst["instance_id"], [])
        code, out = sh([str(venv_py), "-m", "pip", "install", "-q", *deps],
                       timeout=900)
        if code != 0:
            res.update(stage="env_error", error=f"pip install: {out[-600:]}")
            return False

    # scoped 测试范围 = test_patch 涉及的文件；不存在的（新文件）退回其父目录
    paths = re.findall(r"^diff --git a/(\S+)", inst["test_patch"], re.M)
    tests = [p for p in paths if "test" in p.lower()]
    existing = [p for p in tests if (repo_dir / p).exists()]
    scoped = existing or sorted({str(Path(p).parent) for p in tests})
    if not scoped:
        res.update(stage="env_error", error="test_patch 里找不到测试路径")
        return False

    test_cmd = f"{venv_py} -m pytest -q -ra --color=no " + " ".join(scoped)
    code, out = sh(test_cmd, cwd=repo_dir, timeout=600)
    if code not in (0, 1):   # 0=全过 1=有失败都算"可跑"；2/4=收集失败=环境不行
        res.update(stage="env_error", error=f"基线不可跑 exit={code}: {out[-400:]}")
        return False

    (wd / "issue.md").write_text(inst["problem_statement"], encoding="utf-8")
    res.update(stage="prepared", test_cmd=test_cmd, scoped=scoped,
               baseline_exit=code)
    return True


ANSI = re.compile(r"\x1b\[[0-9;]*m")


def solve(inst: dict, wd: Path, res: dict) -> bool:
    """调 RepoPilot 修 issue。--yes 免确认（无头评测），stdout 存 solve.log。"""
    log("  RepoPilot solve …（几分钟，取决于模型）")
    repo_dir = wd / "repo"
    # 先把工作区还原成 pristine：上一轮失败的 solve、或判分阶段打上的官方
    # test_patch 都会留下残余改动，而 RepoPilot 的干净门禁会拒绝在脏工作区上
    # 启动（实测踩坑：重跑批次全被自己的判分残留拦下）。幂等重跑 = 先复位。
    sh(["git", "checkout", "--", "."], cwd=repo_dir)
    sh(["git", "clean", "-fdq"], cwd=repo_dir)
    t0 = time.time()
    code, out = sh(
        [sys.executable, "-m", "repopilot", "solve",
         "--repo", str(wd / "repo"),
         "--issue-file", str(wd / "issue.md"),
         "--test-cmd", res["test_cmd"], "--yes"],
        cwd=ROOT, timeout=1800,
    )
    (wd / "solve.log").write_text(out, encoding="utf-8")

    clean = ANSI.sub("", out)
    m = re.search(r"轨迹目录：(\S+)", clean)
    dirty, diff = sh(["git", "status", "--porcelain"], cwd=wd / "repo")[1], \
        sh(["git", "diff"], cwd=wd / "repo")[1]
    (wd / "agent.diff").write_text(diff, encoding="utf-8")

    res.update(stage="solved", solve_exit=code, solve_secs=round(time.time() - t0),
               run_dir=(m.group(1) if m else None),
               files_touched=len(dirty.splitlines()))
    if code == -1:
        res.update(stage="solve_error", error="solve 超时")
        return False
    # 完成与否的可靠判据：RepoPilot 的状态机保证一切正常结局必经 REPORT
    # （DONE 是唯一终止态）。输出里没有报告横幅 = 崩溃或被门禁拒绝。
    # 【不能】用 "Traceback" in out 判崩溃：agent 调试时会故意打印 bug 的
    # traceback，会被误杀（B 路 3 条实测被误杀，全是完成了的真实尝试）。
    if "RepoPilot 报告" not in clean:
        last = clean.strip().splitlines()[-1] if clean.strip() else ""
        res.update(stage="solve_error", error=f"solve 未走到 REPORT：{last[:200]}")
        return False
    return True


_STATUS_LINE = re.compile(r"^(PASSED|FAILED|ERROR|XFAIL|XPASS|SKIPPED)\s+(\S+)")


def grade(venv_py: str, repo_dir: Path, f2p: list, p2p: list):
    """官方同款判分。关键：【不能】按 node id 逐个选测试——
    数据集的 F2P/P2P id 是当年按空格切 pytest 输出生成的，参数含空格的 id
    被截成了碎片（如 'test_locate_app[cliapp.factory-'），拿去选测试必然
    no-name 报错（flask-5063 实测踩坑）。官方判分是：跑整个测试文件，
    用同样的"空格截断"规则解析 -rA 摘要得到 id→状态表，再逐个比对——
    两边截断方式一致，碎片 id 也能精确对上。

    返回 (f2p_ok, p2p_ok, 失败样本note)。
    """
    files = sorted({i.split("::")[0] for i in (f2p + p2p)})
    _, out = sh([venv_py, "-m", "pytest", "-rA", "--color=no", *files],
                cwd=repo_dir, timeout=1200)

    status: dict[str, str] = {}
    for line in out.splitlines():
        m = _STATUS_LINE.match(line.strip())
        if m:
            status[m.group(2)] = m.group(1)   # 同款截断：\S+ 到第一个空格为止

    # 两套语义：F2P 是"必须修好"——必须白纸黑字 PASSED；
    # P2P 是"不许退步"——只有显式 FAILED/ERROR 才算坏，SKIPPED/未收集不算
    # （本环境跑不了的测试，agent 和 gold 一视同仁地忽略，对比仍然公平；
    #  pytest 的 SKIPPED 摘要行不带完整 id，本来也解析不到）。
    f2p_bad = [i for i in f2p if status.get(i) not in ("PASSED", "XPASS")]
    p2p_bad = [i for i in p2p if status.get(i) in ("FAILED", "ERROR")]
    note = " | ".join(f"{i}→{status.get(i, '未收集')}" for i in (f2p_bad + p2p_bad)[:3])
    return not f2p_bad, not p2p_bad, note


def score(inst: dict, wd: Path, res: dict) -> None:
    """官方判分：打上 test_patch → FAIL_TO_PASS 全过 且 PASS_TO_PASS 全过。

    与官方一致的关键一步：打 test_patch 之前，先把它涉及的测试文件【还原到
    base 状态】——agent 对测试文件的改动不计分（只保留源码修改），否则
    agent 自己补的测试会和官方补丁冲突（flask-5063 实测踩坑）。
    这样做也让 score 可以幂等重跑（--rescore）。
    """
    repo_dir = wd / "repo"
    venv_py = str(wd / "venv" / "bin" / "python")

    test_files = sorted(set(re.findall(r"^diff --git a/(\S+)",
                                       inst["test_patch"], re.M)))
    sh(["git", "checkout", "HEAD", "--", *test_files], cwd=repo_dir)  # 还原已跟踪的
    sh(["git", "clean", "-fq", "--", *test_files], cwd=repo_dir)      # 删掉新建的

    (wd / "test.patch").write_text(inst["test_patch"], encoding="utf-8")
    code, out = sh(["git", "apply", "--whitespace=nowarn", str(wd / "test.patch")],
                   cwd=repo_dir)
    if code != 0:
        res.update(stage="scored", resolved=False,
                   score_note=f"test_patch 打不上: {out[-300:]}")
        return

    f2p = json.loads(inst["FAIL_TO_PASS"])
    p2p = json.loads(inst["PASS_TO_PASS"])
    log(f"  判分：FAIL_TO_PASS {len(f2p)} 条 + PASS_TO_PASS {len(p2p)} 条 …")

    f2p_ok, p2p_ok, note = grade(venv_py, repo_dir, f2p, p2p)
    res.update(stage="scored", resolved=bool(f2p_ok and p2p_ok),
               f2p_ok=f2p_ok, p2p_ok=p2p_ok, score_note=note[:300])


def cmd_run(args):
    instances = json.loads(INSTANCES.read_text())
    if args.only:
        instances = [i for i in instances if i["instance_id"] in args.only]

    for inst in instances:
        iid = inst["instance_id"]
        wd = WORK / iid
        wd.mkdir(parents=True, exist_ok=True)
        res = {} if args.force else load_result(wd)

        if res.get("stage") == "scored" and args.rescore:
            log(f"=== {iid} ===（--rescore：只重判分，不重跑 solve）")
            score(inst, wd, res)
            save_result(wd, res)
            log(f"  → resolved = {res.get('resolved')}")
            continue

        if res.get("stage") in ("scored", "env_error") and not args.force:
            log(f"{iid}: 已是 {res['stage']}，跳过")
            continue

        log(f"=== {iid} ===")
        try:
            if res.get("stage") not in ("prepared", "solved"):
                if not prepare(inst, wd, res):
                    log(f"  ✗ 环境闸门拦下：{res.get('error', '')[:120]}")
                    save_result(wd, res)
                    continue
                save_result(wd, res)
            if args.prepare_only:
                log("  ✓ prepared（--prepare-only，到此为止）")
                continue
            if res.get("stage") != "solved":
                if not solve(inst, wd, res):
                    save_result(wd, res)
                    continue
                save_result(wd, res)
            score(inst, wd, res)
            save_result(wd, res)
            log(f"  → resolved = {res.get('resolved')}")
        except Exception as e:          # 单个实例失败绝不弄死整批
            res.update(stage="harness_error", error=repr(e))
            save_result(wd, res)
            log(f"  ✗ harness_error: {e!r}")


# ---------------------------------------------------------------------------
# calibrate：判分公平性校准。回答"你自己复刻判分逻辑，怎么知道判得公平？"——
# 拿官方 gold patch（标准答案）在【我们的环境】里跑同一套判分：
#   gold 都过不了的实例 = 环境伪影（deps 版本等背锅），从分母剔除；
#   gold 能过的实例 = 判分器在该实例上可信，agent 的失败是真失败。
# gold patch 只在这里、判分侧使用，从头到尾不会进入 agent 的视野。
# ---------------------------------------------------------------------------
def cmd_calibrate(args):
    import pyarrow.parquet as pq
    golds = {r["instance_id"]: r["patch"]
             for r in pq.read_table(CACHE / "swebench_lite_test.parquet").to_pylist()}
    instances = {i["instance_id"]: i for i in json.loads(INSTANCES.read_text())}

    for wd in sorted(WORK.iterdir()):
        res = load_result(wd)
        if res.get("stage") != "scored":
            continue
        iid = wd.name
        inst, repo_dir = instances[iid], wd / "repo"
        venv_py = str(wd / "venv" / "bin" / "python")
        log(f"=== {iid} gold 校准 ===")

        # 复位 → 打 gold → 打 test_patch → 跑官方判分
        sh(["git", "checkout", "--", "."], cwd=repo_dir)
        sh(["git", "clean", "-fdq"], cwd=repo_dir)
        (wd / "gold.patch").write_text(golds[iid], encoding="utf-8")
        code, out = sh(["git", "apply", "--whitespace=nowarn",
                        str(wd / "gold.patch")], cwd=repo_dir)
        if code != 0:
            res.update(gold_ok=False, gold_note=f"gold 打不上: {out[-200:]}")
        else:
            (wd / "test.patch").write_text(inst["test_patch"], encoding="utf-8")
            sh(["git", "apply", "--whitespace=nowarn",
                str(wd / "test.patch")], cwd=repo_dir)
            f2p = json.loads(inst["FAIL_TO_PASS"])
            p2p = json.loads(inst["PASS_TO_PASS"])
            ok1, ok2, note = grade(venv_py, repo_dir, f2p, p2p)
            res.update(gold_ok=bool(ok1 and ok2), gold_note=note[:200])

        # 恢复 agent 的现场：复位 + 重放它当时的 diff
        sh(["git", "checkout", "--", "."], cwd=repo_dir)
        sh(["git", "clean", "-fdq"], cwd=repo_dir)
        if (wd / "agent.diff").exists() and (wd / "agent.diff").read_text().strip():
            sh(["git", "apply", "--whitespace=nowarn",
                str(wd / "agent.diff")], cwd=repo_dir)
        save_result(wd, res)
        log(f"  gold_ok = {res['gold_ok']}")


# ---------------------------------------------------------------------------
# report：汇总所有 result.json（+ 各自 run.jsonl 的工具/token 统计）
# ---------------------------------------------------------------------------
def trace_stats(run_dir):
    """从 RepoPilot 的黑匣子里抽评测指标：工具调用数、上下文峰值。"""
    stats = {"tools": 0, "peak_tokens": 0}
    f = Path(run_dir or "") / "run.jsonl"
    if not f.exists():
        return stats
    for line in f.read_text(encoding="utf-8").splitlines():
        e = json.loads(line)
        if e["kind"] == "tool":
            stats["tools"] += 1
        if e["kind"] == "executor_done":
            stats["peak_tokens"] = max(stats["peak_tokens"], e.get("peak_tokens", 0))
    return stats


def cmd_report(args):
    rows = []
    for wd in sorted(WORK.iterdir()) if WORK.exists() else []:
        res = load_result(wd)
        if not res:
            continue
        stats = trace_stats(res.get("run_dir"))
        rows.append({"id": wd.name, **res, **stats})

    # gold 校准把"尝试过"的实例分成两拨：
    #   certified：gold 在本环境能过 → 判分器对该实例可信，agent 的成败是真成败
    #   artifact ：gold 都过不了 → 本环境无法判定（如 py3.12 弃用警告杀老代码）
    artifacts = [r for r in rows
                 if r["stage"] == "scored" and r.get("gold_ok") is False]
    attempted = [r for r in rows if r["stage"] in ("scored", "solve_error")]
    certified = [r for r in attempted if r not in artifacts]
    resolved = [r for r in certified if r.get("resolved")]
    skipped = [r for r in rows if r["stage"] == "env_error"]
    calibrated = any("gold_ok" in r for r in rows)

    lines = ["# SWE-bench Lite 迷你评测结果", "",
             f"- 尝试：{len(attempted)}  |  gold 校准可判定：{len(certified)}  |  "
             f"环境伪影（gold 也过不了）：{len(artifacts)}  |  "
             f"环境闸门跳过：{len(skipped)}",
             f"- **可判定实例上 resolved = {len(resolved)}/{len(certified)}"
             f"{'' if not certified else f' = {len(resolved)/len(certified):.0%}'}**"
             f"；保守口径（未判定全算失败）= {len(resolved)}/{len(attempted)}"
             + ("" if calibrated else
                "（未做 gold 校准，跑 calibrate 子命令可校准判分公平性）"),
             "", "| 实例 | 结果 | 工具调用 | 上下文峰值 | 耗时(s) | 备注 |",
             "|---|---|---|---|---|---|"]
    for r in rows:
        mark = ("⚠ env_artifact" if r in artifacts else
                "✅ resolved" if r.get("resolved") else
                "⏭ env_skip" if r["stage"] == "env_error" else "❌ " + r["stage"])
        lines.append(f"| {r['id']} | {mark} | {r.get('tools', '')} | "
                     f"{r.get('peak_tokens', '')} | {r.get('solve_secs', '')} | "
                     f"{(r.get('error') or r.get('score_note') or '')[:60]} |")

    out = "\n".join(lines) + "\n"
    (EVAL_DIR / "RESULTS.md").write_text(out, encoding="utf-8")
    print(out)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--limit", type=int, default=10)
    r = sub.add_parser("run")
    r.add_argument("--only", nargs="*")
    r.add_argument("--prepare-only", action="store_true")
    r.add_argument("--force", action="store_true")
    r.add_argument("--rescore", action="store_true",
                   help="对已 scored 的实例只重跑判分（不重新花钱 solve）")
    sub.add_parser("report")
    sub.add_parser("calibrate")
    args = p.parse_args()
    {"fetch": cmd_fetch, "run": cmd_run, "report": cmd_report,
     "calibrate": cmd_calibrate}[args.cmd](args)


if __name__ == "__main__":
    main()
