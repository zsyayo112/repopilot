"""Agent State Machine：把 Plan-Execute-Verify-Review 串成闭环的总指挥。

LangGraph 卖的就是这张图的框架版；手写出来不过几十行 while + if：

    BASELINE → PLAN → EXECUTE → VERIFY ──不达标且有重试额度──→ EXECUTE
                                  │
                                  └──达标 / 额度耗尽──→ REVIEW → REPORT

状态显式化的价值：每次状态切换都写进 trace，事后能精确回放
"它在哪个状态花了多少步、为什么转移" —— 这是可观察性的地基。
"""

import json
import time

from .adapters import detect
from .config import (
    BOLD, CLONES_DIR, DIM, GREEN, MAX_FIX_ATTEMPTS, MAX_MODIFIED_FILES,
    RED, RESET, RUNS_DIR, YELLOW,
)
from .executor import build_initial_messages, run_executor
from .permissions import Permissions
from .planner import make_plan, render_plan
from .reviewer import review
from .tools import ToolKit
from .trace import Trace
from .verifier import compare, run_tests
from .workspace import Workspace


def solve(repo: str, issue: str, *, test_cmd: str | None = None,
          yes: bool = False, plan_only: bool = False,
          max_attempts: int = MAX_FIX_ATTEMPTS) -> int:
    """跑完整个闭环，返回退出码（0=成功且审查通过）。"""

    # ---- 准备工作区（门禁：git 仓库 + 工作区干净）----
    ws = Workspace.prepare(repo, CLONES_DIR)
    ws.ensure_clean()
    profile = detect(ws.root, test_cmd)
    if not profile.test_cmd:
        print(f"{RED}{profile.describe()}{RESET}")
        return 2

    run_dir = RUNS_DIR / time.strftime("%Y%m%d-%H%M%S")
    trace = Trace(run_dir)
    trace.event("start", repo=str(ws.root), head=ws.head(),
                project_kind=profile.kind, test_cmd=profile.test_cmd)

    toolkit = ToolKit(ws, profile)
    perms = Permissions(trust_all=yes)

    print(f"{BOLD}RepoPilot{RESET} @ {ws.root}  (HEAD {ws.head()})")
    print(f"{DIM}{profile.describe()}{RESET}")
    print(f"{DIM}轨迹目录：{run_dir}{RESET}\n")

    # ---- 状态机主循环 ----
    state = "BASELINE"
    attempt = 0
    baseline = plan = messages = comparison = verdict = None
    exit_code = 1

    while state not in ("DONE", "FAILED"):
        trace.event("state", state=state, attempt=attempt)

        if state == "BASELINE":
            print(f"{YELLOW}[基线] 修改前先跑一遍测试…{RESET}")
            baseline = run_tests(ws, profile)
            print(f"{DIM}{baseline.summary()}{RESET}\n")
            trace.event("baseline", summary=baseline.summary())
            state = "PLAN"

        elif state == "PLAN":
            print(f"{YELLOW}[计划] Planner 正在分析 issue 和仓库结构…{RESET}")
            plan = make_plan(issue, ws, profile, baseline)
            trace.save("plan.json", render_plan(plan))
            print(render_plan(plan) + "\n")
            state = "DONE" if plan_only else "EXECUTE"
            if plan_only:
                exit_code = 0

        elif state == "EXECUTE":
            attempt += 1
            print(f"{YELLOW}[执行] 第 {attempt}/{max_attempts} 轮…{RESET}")
            if messages is None:
                messages = build_initial_messages(issue, render_plan(plan),
                                                  baseline.summary())
            summary, _ = run_executor(toolkit, perms, messages, trace)
            trace.event("executor_summary", attempt=attempt, summary=summary[:500])
            state = "VERIFY"

        elif state == "VERIFY":
            print(f"\n{YELLOW}[验证] 修改后再跑一遍测试，与基线对比…{RESET}")
            after = run_tests(ws, profile)
            comparison = compare(baseline, after)
            trace.event("verify", **comparison)
            print(f"{DIM}状态：{comparison['status']}\n{after.summary()}{RESET}")

            # 硬约束：改动规模超限直接判负（防"改跑偏"洗掉半个仓库）
            modified = ws.modified_files()
            if len(modified) > MAX_MODIFIED_FILES:
                print(f"{RED}[中止] 改动了 {len(modified)} 个文件，超过上限 "
                      f"{MAX_MODIFIED_FILES}：{modified}{RESET}")
                trace.event("abort", reason="modified_files_exceeded", files=modified)
                state = "FAILED"
            elif comparison["status"] in ("fixed", "still_green"):
                state = "REVIEW"
            elif attempt < max_attempts:
                # 失败反馈直接追加进同一份对话：模型记得上一轮做了什么
                messages.append({"role": "user", "content": (
                    f"测试仍未通过（这是第 {attempt} 轮尝试后的结果）：\n"
                    f"{after.render()}\n\n"
                    "请分析失败原因，继续修复。如果发现方向错了，先撤回你之前的改动思路。"
                )})
                state = "EXECUTE"
            else:
                print(f"{RED}[重试额度耗尽] 仍未达标，进入审查阶段如实报告。{RESET}")
                state = "REVIEW"

        elif state == "REVIEW":
            print(f"\n{YELLOW}[审查] Reviewer 在独立上下文中复查（看不到 executor 的对话史）…{RESET}")
            diff = ws.diff()
            trace.save("final.diff", diff or "(空 diff)")
            verdict = review(issue, plan, diff, comparison)
            trace.save("review.json", json.dumps(verdict, ensure_ascii=False, indent=2))
            state = "REPORT"

        elif state == "REPORT":
            ok = comparison["status"] in ("fixed", "still_green") \
                and verdict.get("verdict") == "approve"
            exit_code = 0 if ok else 1
            color = GREEN if ok else RED

            print(f"\n{BOLD}{'=' * 60}{RESET}")
            print(f"{BOLD}RepoPilot 报告{RESET}")
            print(f"  测试对比：{comparison['status']}")
            print(f"  审查结论：{color}{verdict.get('verdict')}{RESET} —— "
                  f"{verdict.get('comments', '')}")
            if verdict.get("unrelated_changes"):
                print(f"  {YELLOW}无关改动：{verdict['unrelated_changes']}{RESET}")
            if verdict.get("missing_tests"):
                print(f"  {YELLOW}缺少防复发测试{RESET}")
            print(f"\n{ws.diff_stat() or '(无改动)'}")
            print(f"\n{DIM}完整轨迹：{run_dir}/")
            print(f"满意 → 自己去 commit（agent 被策略禁止 commit，最后一步永远归人）：")
            print(f"    cd {ws.root} && git add -p && git commit")
            print(f"不满意 → 一键回滚：")
            print(f"    cd {ws.root} && git checkout -- . && git clean -fd{RESET}")
            trace.event("report", ok=ok, status=comparison["status"],
                        verdict=verdict.get("verdict"))
            state = "DONE"

    trace.event("end", exit_code=exit_code)
    return exit_code
