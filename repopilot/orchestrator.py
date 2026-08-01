"""Agent State Machine：把 Plan-Execute-Verify-Review 串成闭环的总指挥。

LangGraph 卖的就是这张图的框架版；手写出来不过几十行 while + if：

    BASELINE → [START_RUNTIME] → [REPRODUCE] → PLAN → EXECUTE → VERIFY
                                                        │  ▲        │
                                        不达标且有额度 ──┘  │        │
                                                           │  达标/额度耗尽
                                                           │        ↓
                                        Reviewer 驳回且有额度 ──── REVIEW
                                                                    ↓
                                                    REPORT → CLEANUP → DONE

方括号里那两个是【条件状态】：不开运行时验证就直接穿过去。

【本轮改造：把"什么时候停"从模型手里收回来】
以前只有一个停机条件：重试次数用完。评测轨迹显示这远远不够 —— 失败实例
里出现过 120 次工具调用、901 秒，全程在同一个错误方向上反复微调。
现在每轮 VERIFY 之后都过一遍 halting.HaltingPolicy，它只看可观测量：
失败指纹变了吗、diff 是不是在变大、同一个文件改了几轮、token 用掉几成。
它可以给出四种裁决：continue / narrow（收掉探索工具）/ rollback（回到最佳
检查点）/ stop。还有一条兜底：**交卷时交的是历史最佳补丁，不是最后一版**。

【Reviewer 从"终点"变成了"环"】
以前 REVIEW 是通往 REPORT 的单行道，审查意见只是一段打印出来的评语。
现在它可以驳回并把 requested_actions 送回 EXECUTE（额度由 budget 控制），
于是"Reviewer 有没有用"变成一个可测量的问题：驳回了几次、驳回后转绿了几个。

两条硬规则没变：
  1) DONE 是【唯一】的真·终止态；所有结局都必须先流经 REPORT 这道出口。
  2) 绝不允许"哑巴终止"（把 state 一设、直接 return，什么都不交代）。
"""

import json
import time
from pathlib import Path

from .adapters import detect
from .budget import Budget, Ledger
from .config import (
    BOLD,
    CLONES_DIR,
    DIM,
    GREEN,
    MAX_BASELINE_UNITS,
    MAX_FIX_ATTEMPTS,
    MAX_MODIFIED_FILES,
    RED,
    RESET,
    RUNS_DIR,
    YELLOW,
)
from .doctor import diagnose
from .executor import build_initial_messages, run_executor
from .halting import HaltingPolicy
from .patch_audit import audit as audit_patch
from .permissions import Permissions
from .planner import make_plan, render_plan
from .reviewer import render_request, review
from .tools import ToolKit
from .trace import Trace
from .verifier import compare, run_tests, run_tests_in, run_validation
from .workspace import Workspace

# 状态严重程度：合并多个子项目的对比结果时，取最差的那个。
# "有一个模块回归了"必须盖过"另外三个模块没事"。
_SEVERITY = {"regressed": 4, "no_change": 3, "improved": 2, "fixed": 1, "still_green": 0}
_OK_STATUSES = ("fixed", "still_green")

# 预算闸门分两档 —— 超支的【种类】要和状态的【开销】对得上，一刀切会误伤。
#   花 token 的状态：任何一种超支都拦。token 用光了还去调模型，
#       调不出东西还要计费。
#   只花时间的状态：只有【墙钟】超了才拦。一次全量验证要十几分钟，
#       但一个 token 都不花 —— 为了 TOKEN_LIMIT 把它跳掉是白白丢掉信息，
#       而这次评测真正被耗尽的资源是时间，不是 token。
#   两张表都没有的：BASELINE（此时表刚起步，拦不到东西）以及
#       REVIEW / REPORT / CLEANUP / DONE。REVIEW 不整体拦是因为它前半段
#       （存 final.diff、跑补丁审计）不花钱又正好是评测要的产物，真正贵的
#       那次模型调用由它内部自己查表；REPORT 之后是交卷路径，任何情况下
#       都不许跳（见文件头第 1 条硬规则）。
_TOKEN_SPENDING_STATES = ("PLAN", "EXECUTE")
_TIME_ONLY_STATES = ("START_RUNTIME", "REPRODUCE", "VERIFY")


def _gate_reason(state: str, ledger: Ledger) -> str | None:
    """这个状态现在还准不准进。返回拦下的原因码，放行返回 None。"""
    over = ledger.exhausted()
    if over is None:
        return None
    if state in _TOKEN_SPENDING_STATES:
        return over
    if state in _TIME_ONLY_STATES:
        return over if over == "TIMEOUT" else None
    return None


def solve(repo: str, issue: str, *, test_cmd: str | None = None,
          yes: bool = False, plan_only: bool = False,
          max_attempts: int | None = None,
          with_runtime: bool = False, scenario_path: str | None = None,
          ignore_env: bool = False, budget: Budget | None = None,
          result_path: str | None = None) -> int:
    """跑完整个闭环，返回退出码（0=成功且审查通过）。

    budget 是这次运行被允许花的全部资源（见 budget.py）。不传就用默认值 ——
    但评测【必须】显式传一个，否则两次运行不可比。
    result_path 指定后会额外写一份机器可读结果，评测线束据此判定，
    不再靠 grep 标准输出里的横幅（那种判据一改文案就全线崩）。
    """
    budget = budget or Budget(max_modified_files=MAX_MODIFIED_FILES,
                              max_fix_attempts=MAX_FIX_ATTEMPTS)
    if max_attempts is not None:
        budget = budget.variant(max_fix_attempts=max_attempts)
    if with_runtime:
        budget = budget.variant(allow_runtime=True)
    ledger = Ledger(budget=budget)

    # ---- 准备工作区（门禁：git 仓库 + 工作区干净）----
    ws = Workspace.prepare(repo, CLONES_DIR)
    ws.ensure_clean()
    profile = detect(ws.root, test_cmd)
    if not profile.test_cmd:
        print(f"{RED}{profile.describe()}{RESET}")
        return 2

    run_dir = RUNS_DIR / time.strftime("%Y%m%d-%H%M%S")
    trace = Trace(run_dir)

    # ---- 能力开关：工具按需暴露，不是有什么就全给（见 tools.py 文件头）----
    groups, scenario, cap_notes = _capabilities(budget, scenario_path, profile)
    trace.event("start", repo=str(ws.root), head=ws.head(),
                project_kind=profile.kind, test_cmd=profile.test_cmd,
                capabilities=list(groups), budget=budget.to_dict(),
                budget_fingerprint=budget.fingerprint(),
                units=[u.path for u in (profile.repository.units if profile.repository else [])])

    print(f"{BOLD}RepoPilot{RESET} @ {ws.root}  (HEAD {ws.head()})")
    print(f"{DIM}{profile.describe()}{RESET}")
    print(f"{DIM}能力：{'、'.join(groups)}{RESET}")
    print(f"{DIM}预算：{budget.describe()}  [{budget.fingerprint()}]{RESET}")
    for note in cap_notes:
        print(f"{YELLOW}  ! {note}{RESET}")
    print(f"{DIM}轨迹目录：{run_dir}{RESET}\n")

    # ---- 环境体检：宁可现在就停，也不要花 900 秒去"修"一个环境问题 ----
    env = diagnose(ws, profile, want_browser="browser" in groups)
    print(env.render())
    trace.save("doctor.txt", env.render(color=False))
    if not env.ok and not ignore_env:
        print(f"\n{RED}环境未就绪，已停止。装好上面标 ✗ 的依赖再来，"
              f"或者用 --ignore-env 强行继续（不建议：测试失败会分不清是谁的问题）。{RESET}")
        trace.event("abort", reason="environment_not_ready",
                    blockers=[c.name for c in env.blockers()])
        _write_result(run_dir, result_path, {
            "ok": False, "halt_code": "ENVIRONMENT_ERROR",
            "error": "环境体检未通过：" + ", ".join(c.name for c in env.blockers()),
            "budget": budget.to_dict(), "ledger": ledger.to_dict()})
        return 2
    print()

    toolkit = ToolKit(ws, profile, groups=groups, artifacts_dir=run_dir, trace=trace,
                      budget=budget, ledger=ledger)
    perms = Permissions(trust_all=yes)

    try:
        return _loop(ws, profile, issue, toolkit, perms, trace, run_dir,
                     groups=groups, scenario=scenario, plan_only=plan_only,
                     budget=budget, ledger=ledger, result_path=result_path)
    finally:
        # 兜底清理。CLEANUP 状态负责正常路径，这里负责异常路径（Ctrl-C、崩溃）。
        # 两层都要有：状态机里那一层是可观察的，finally 这一层是可靠的。
        note = toolkit.cleanup()
        if note != "没有需要清理的资源":
            print(f"{DIM}[清理] {note}{RESET}")
            trace.event("cleanup_final", detail=note)


def _capabilities(budget: Budget, scenario_path: str | None, profile):
    """算出这次任务该开哪些工具组。能力现在由 budget 决定 —— 它是被冻结的那份约定。"""
    from .scenario import Scenario

    groups, notes = ["core"], []
    scenario = None
    with_runtime = budget.allow_runtime

    if scenario_path:
        scenario = Scenario.load(scenario_path)
        with_runtime = True          # 给了复现脚本就必然需要浏览器

    if with_runtime:
        groups.append("runtime")
        if not profile.services():
            notes.append("开了 --with-runtime，但 adapter 没识别出这个项目怎么启动 "
                         "—— 服务相关工具会返回明确的提示")
        try:
            import playwright  # noqa: F401
            groups.append("browser")
        except ImportError:
            notes.append('未安装 Playwright，浏览器工具不会暴露。'
                         '要用请装：pip install -e ".[browser]" && playwright install chromium')
            if scenario is not None:
                raise RuntimeError(
                    "指定了 --scenario 但没装 Playwright —— 复现脚本必须在浏览器里跑。\n"
                    'pip install -e ".[browser]" && playwright install chromium')
    return tuple(groups), scenario, notes


def _loop(ws, profile, issue, toolkit, perms, trace, run_dir, *,
          groups, scenario, plan_only, budget, ledger, result_path) -> int:
    state = "BASELINE"
    attempt = review_round = 0
    baselines: dict = {}
    plan = messages = comparison = verdict = None
    validation = None
    after_report = None       # 最近一次验证的主报告，停机策略要它的失败指纹
    repro_before = repro_after = None
    abort_reason = None       # 非 None 表示"跑偏中止"，REPORT 据此走另一套输出
    halt_code = ""            # 停机/失败分类码，会进 result.json
    runtime_note = ""
    exit_code = 1
    reviewer_rounds: list[dict] = []      # 每一轮审查的结论，用来统计驳回/挽救
    halting = HaltingPolicy(budget=budget, enabled=budget.allow_halting_policy)

    while state != "DONE":   # DONE 是唯一真·终止态，一切结局都得先过 REPORT
        trace.event("state", state=state, attempt=attempt)

        # ---- 墙钟闸门 -----------------------------------------------------
        # 预算要真是硬约束，就得在【每次状态切换】上查，而不是只在 executor
        # 的循环里查。实测过一条 pylint 实例：executor 314 秒就因 TOKEN_LIMIT
        # 停了（还在预算内），随后 VERIFY 跑了 922 秒、停机决策 591 秒、
        # REVIEW 进去再没出来 —— 1800 秒的预算跑成 65 分钟，而超支的那 35 分钟
        # 全发生在 executor 之外，没有任何一处代码在看表。
        #
        # 闸门只能拦【状态入口】，拦不住已经进去的一次调用：一次全量测试可以
        # 跑满 TEST_TIMEOUT，一次模型调用可以在网络退避里耗掉几分钟。所以外层
        # harness 还要有一道硬杀（eval/run.py），两道合起来才封得住。
        if over := _gate_reason(state, ledger):
            print(f"\n{RED}[预算闸门] {over} —— {ledger.summary()}{RESET}")
            print(f"{RED}  在进入 {state} 之前停下，交出当前最佳补丁。{RESET}")
            trace.event("budget_gate", state=state, code=over,
                        elapsed_secs=round(ledger.elapsed, 1))
            halt_code = halt_code or over
            _restore_best(ws, halting, trace)
            if comparison is None:
                comparison = _unverified_comparison(over)
            state = "REVIEW"
            continue

        if state == "BASELINE":
            print(f"{YELLOW}[基线] 修改前先跑一遍测试…{RESET}")
            baselines = _run_baselines(ws, profile)
            for path, report in baselines.items():
                label = "" if path == "." else f"[{path}] "
                print(f"{DIM}{label}{report.summary()}{RESET}")
            trace.event("baseline", **{p: r.summary() for p, r in baselines.items()})
            print()
            state = "START_RUNTIME"

        elif state == "START_RUNTIME":
            if "runtime" not in groups or not profile.services():
                state = "REPRODUCE"
                continue
            print(f"{YELLOW}[启动] 把应用跑起来，并等健康检查通过…{RESET}")
            runtime_note = toolkit.start_services()
            print(f"{DIM}{runtime_note}{RESET}\n")
            trace.event("runtime_started", detail=runtime_note[:800])
            # 起不来【不中止任务】：很多 issue 光靠读代码和单测也能修。
            # 但要如实记下来，最后报告里说清楚"运行时验证这一层是缺的"。
            state = "REPRODUCE"

        elif state == "REPRODUCE":
            if scenario is None:
                state = "PLAN"
                continue
            print(f"{YELLOW}[复现] 改之前先证明 issue 真的存在…{RESET}")
            print(f"{DIM}{scenario.render()}{RESET}")
            repro_before = _run_scenario(scenario, toolkit, run_dir, "before")
            print(f"{DIM}{repro_before.render()}{RESET}\n")
            trace.event("reproduce", passed=repro_before.passed)
            if repro_before.passed:
                # 复现脚本在【修改前】就通过了 = 这个 issue 在当前代码上不成立。
                # 这时候硬着头皮去改，改的一定是别的东西。
                abort_reason = (
                    "复现脚本在修改前就【通过】了 —— 说明这个 issue 在当前代码上不成立，"
                    "或者复现步骤描述得不对。没有动任何代码。")
                halt_code = "ISSUE_NOT_REPRODUCED"
                comparison = {"status": "still_green", "baseline": "(未修改)",
                              "after": "(未修改)", "new_failures": []}
                state = "REPORT"
                continue
            state = "PLAN"

        elif state == "PLAN":
            print(f"{YELLOW}[计划] Planner 正在分析 issue 和仓库结构…{RESET}")
            plan = make_plan(issue, ws, profile, _primary(baselines),
                             budget=budget, ledger=ledger)
            trace.save("plan.json", render_plan(plan))
            print(render_plan(plan) + "\n")
            state = "DONE" if plan_only else "EXECUTE"
            if plan_only:
                exit_code = 0

        elif state == "EXECUTE":
            attempt += 1
            print(f"{YELLOW}[执行] 第 {attempt}/{budget.max_fix_attempts} 轮…"
                  f"{DIM}（已花 {ledger.summary()}）{RESET}")
            if messages is None:
                messages = build_initial_messages(
                    issue, render_plan(plan), _primary(baselines).summary(),
                    groups=groups,
                    extra_context=_execute_context(profile, runtime_note, repro_before),
                )
            summary, _ = run_executor(toolkit, perms, messages, trace,
                                      budget=budget, ledger=ledger,
                                      narrowed=halting.narrowed)
            trace.event("executor_summary", attempt=attempt, summary=summary[:500])
            state = "VERIFY"

        elif state == "VERIFY":
            print(f"\n{YELLOW}[验证] 修改后再跑一遍测试，与基线对比…{RESET}")
            modified = ws.modified_files()

            comparison, after_report = _verify_tests(ws, profile, baselines, modified, trace)
            print(f"{DIM}状态：{comparison['status']}"
                  f"（判定可信度：{comparison.get('confidence', '?')}）\n"
                  f"{comparison['after']}{RESET}")
            trace.event("verify", **comparison)

            # 记检查点。**交卷时交的是历史最佳，不是最后一版** —— 这行代码
            # 就是那句话的全部实现（见 halting.py 文件头的 A/B/C 例子）。
            checkpoint = halting.record(attempt, comparison, after_report,
                                        ws.diff(), modified)
            decision = halting.decide(ledger, modified)
            halting.note(decision, attempt)
            if decision.action != "continue":
                trace.event("halting", action=decision.action, code=decision.code,
                            reason=decision.reason)
                print(f"{YELLOW}[停机策略] {decision.reason}{RESET}")

            if decision.action == "stop":
                halt_code = decision.code
                _restore_best(ws, halting, trace)
                if decision.code == "MODIFIED_FILES_EXCEEDED":
                    # 改动规模超限判为跑偏：不送 REVIEW（对一堆乱改跑审查既费钱
                    # 又无意义），但【仍要经过 REPORT】——把 diff 和回滚命令交给用户。
                    abort_reason = decision.reason
                    state = "REPORT"
                else:
                    state = "REVIEW"
                continue

            if decision.action == "rollback":
                _restore_best(ws, halting, trace)
                comparison, after_report = _rebuild_comparison(halting, comparison)

            tests_ok = comparison["status"] in _OK_STATUSES

            # 测试绿了不等于没问题：跑一遍 lint/typecheck/build。
            # 只在测试已经绿的时候跑 —— 测试都红着就没必要为构建再等几分钟。
            if tests_ok:
                print(f"{YELLOW}[验证] 测试达标，继续跑 lint / typecheck / build…{RESET}")
                validation = run_validation(ws, profile, only=None)
                print(f"{DIM}{validation.render()}{RESET}")
                trace.event("validation", ok=validation.ok,
                            steps=[(s.name, s.exit_code, s.skipped) for s in validation.steps])

            # 有复现脚本就必须跑同一份，这是"真修好"的唯一客观证据
            if tests_ok and validation is not None and validation.ok and scenario is not None:
                print(f"{YELLOW}[验证] 重跑同一份复现脚本…{RESET}")
                repro_after = _run_scenario(scenario, toolkit, run_dir, "after")
                print(f"{DIM}{repro_after.render()}{RESET}")
                trace.event("reproduce_after", passed=repro_after.passed)

            done = (tests_ok
                    and (validation is None or validation.ok)
                    and (scenario is None or (repro_after is not None and repro_after.passed)))

            if done:
                state = "REVIEW"
            elif attempt < budget.max_fix_attempts:
                messages.append({"role": "user", "content": _retry_feedback(
                    attempt, comparison, after_report, validation, repro_after,
                    checkpoint, halting)})
                state = "EXECUTE"
            else:
                print(f"{RED}[重试额度耗尽] 仍未达标，进入审查阶段如实报告。{RESET}")
                halt_code = halt_code or "ATTEMPTS_EXHAUSTED"
                state = "REVIEW"

        elif state == "REVIEW":
            diff = ws.diff()
            trace.save("final.diff", diff or "(空 diff)")
            audit = audit_patch(diff).to_dict()
            trace.save("patch_audit.json", json.dumps(audit, ensure_ascii=False, indent=2))
            if audit["flags"]:
                print(f"{YELLOW}[补丁审计] " + "；".join(audit["flags"][:3]) + RESET)

            if not budget.allow_reviewer:
                # 消融配置：关掉 Reviewer。要如实说明"这一层是缺的"，
                # 而不是悄悄跳过让报告看起来一样。
                print(f"{DIM}[审查] 本次运行按预算关闭了独立审查（消融配置）。{RESET}")
                trace.event("review_skipped", reason="budget.allow_reviewer=False")
                state = "REPORT"
                continue

            # 预算耗尽还去调 Reviewer，是评测卡死的直接原因：executor 那边早就
            # 因为超支停了，REVIEW 却又开了一次谁都没在看表的模型调用，撞上网络
            # 退避（每次 10+30+60+120 秒，json_call 还能再叠三倍）之后，一条实例
            # 在这里卡了半小时。
            #
            # 这和上面那个消融开关是【两件事】，所以分开记：那个是"这次实验故意
            # 不要这一层"，这个是"想要但没钱了"。报告里混成一句会让消融结论失真。
            if over := ledger.exhausted():
                print(f"{YELLOW}[审查] 预算已耗尽（{over}），跳过独立审查 —— "
                      f"补丁照常交出，但这一层是缺的。{RESET}")
                trace.event("review_skipped", reason=f"budget_exhausted:{over}")
                halt_code = halt_code or over
                state = "REPORT"
                continue

            review_round += 1
            print(f"\n{YELLOW}[审查] 第 {review_round} 轮，Reviewer 在独立上下文中复查"
                  f"（看不到 executor 的对话史）…{RESET}")
            verdict = review(issue, plan, diff, comparison,
                             validation=validation.render() if validation else "",
                             scenario_result=_scenario_evidence(repro_before, repro_after),
                             audit=audit, budget=budget, ledger=ledger)
            reviewer_rounds.append({"round": review_round, **verdict})
            trace.save("review.json", json.dumps(verdict, ensure_ascii=False, indent=2))
            trace.event("review", round=review_round, decision=verdict["decision"],
                        requested_actions=verdict["requested_actions"])

            # 驳回 → 回到 EXECUTE 再修一轮。三道闸门决定还修不修：
            # 还有审查额度、还有重试额度、以及 Reviewer 不是在重复同一句话。
            repeated = halting.reviewer_repeats(verdict)
            can_revise = (verdict["decision"] == "revise"
                          and review_round <= budget.max_review_rounds
                          and attempt < budget.max_fix_attempts
                          and not ledger.exhausted())
            if can_revise and repeated:
                print(f"{YELLOW}[停机策略] Reviewer 第二次提出同一个要求 —— "
                      f"它不会被说服，停止重修。{RESET}")
                halt_code = halt_code or "REVIEWER_REPEAT"
            elif can_revise:
                print(f"{RED}[审查未通过] 按审查意见再修一轮。{RESET}")
                messages.append({"role": "user",
                                 "content": render_request(verdict, review_round)})
                state = "EXECUTE"
                continue
            elif verdict["decision"] == "revise":
                halt_code = halt_code or "REVIEWER_REJECTED"
            state = "REPORT"

        elif state == "REPORT":
            aborted = abort_reason is not None
            ok = (not aborted
                  and comparison["status"] in _OK_STATUSES
                  and (validation is None or validation.ok)
                  and (scenario is None or (repro_after is not None and repro_after.passed))
                  and (not budget.allow_reviewer
                       or (verdict is not None and verdict["decision"] == "accept")))
            exit_code = 0 if ok else 1
            color = GREEN if ok else RED

            print(f"\n{BOLD}{'=' * 60}{RESET}")
            print(f"{BOLD}RepoPilot 报告{RESET}")
            if aborted:
                print(f"  {RED}中止：{abort_reason}{RESET}")
            print(f"  测试对比：{comparison['status']}"
                  f"（可信度 {comparison.get('confidence', '?')}）")
            if validation is not None:
                print(f"  验证流水线：{'全过' if validation.ok else '有失败'}")
                for step in validation.steps:
                    print(f"  {step.render()}")
            if scenario is not None:
                before = "失败（issue 已复现）" if repro_before and not repro_before.passed else "?"
                after = ("通过（issue 已修复）" if repro_after and repro_after.passed
                         else "仍未通过" if repro_after else "未执行")
                print(f"  复现脚本：修改前 {before} → 修改后 {after}")
            if not aborted and verdict is not None:
                print(f"  审查结论：{color}{verdict['decision']}{RESET}"
                      f"（{review_round} 轮）—— {verdict.get('comments', '')}")
                if verdict.get("requested_actions"):
                    print(f"  {YELLOW}未满足的要求：{'；'.join(verdict['requested_actions'][:3])}{RESET}")
                if verdict.get("unrelated_changes"):
                    print(f"  {YELLOW}无关改动：{verdict['unrelated_changes']}{RESET}")
            best = halting.best
            if best is not None and best.attempt != attempt:
                print(f"  {YELLOW}已回滚到第 {best.attempt} 轮的最佳补丁"
                      f"（失败 {best.failures} 个，优于最后一轮）{RESET}")
            print(f"  预算消耗：{ledger.summary()}")
            print(f"\n{ws.diff_stat() or '(无改动)'}")
            print(f"\n{DIM}完整轨迹：{run_dir}/")
            print("满意 → 自己去 commit（agent 被策略禁止 commit，最后一步永远归人）：")
            print(f"    cd {ws.root} && git add -p && git commit")
            print("不满意 → 一键回滚：")
            print(f"    cd {ws.root} && git checkout -- . && git clean -fd{RESET}")

            trace.event("report", ok=ok, status=comparison["status"],
                        verdict=(None if verdict is None else verdict["decision"]),
                        validation_ok=(None if validation is None else validation.ok),
                        scenario_passed=(None if repro_after is None else repro_after.passed),
                        aborted=aborted, halt_code=halt_code, **ledger.to_dict())
            _write_result(run_dir, result_path, _result_payload(
                ok=ok, aborted=aborted, abort_reason=abort_reason, halt_code=halt_code,
                comparison=comparison, validation=validation, verdict=verdict,
                reviewer_rounds=reviewer_rounds, attempts=attempt,
                review_rounds=review_round, halting=halting, budget=budget,
                ledger=ledger, diff=ws.diff(), run_dir=run_dir))
            state = "CLEANUP"

        elif state == "CLEANUP":
            note = toolkit.cleanup()
            if note != "没有需要清理的资源":
                print(f"\n{DIM}[清理] {note}{RESET}")
            trace.event("cleanup", detail=note)
            state = "DONE"

    trace.event("end", exit_code=exit_code)
    return exit_code


# ---------------------------------------------------------------------------
# 结果落盘：给评测线束的机器可读出口
# ---------------------------------------------------------------------------
def _result_payload(*, ok, aborted, abort_reason, halt_code, comparison, validation,
                    verdict, reviewer_rounds, attempts, review_rounds, halting,
                    budget, ledger, diff, run_dir) -> dict:
    """一次运行的全部可统计事实。

    评测线束【只读这个文件】来判断"这次求解到底做了什么"。以前它 grep 标准输出
    里的"RepoPilot 报告"横幅，那种判据一改文案就全线崩，而且分不清"崩溃了"
    和"完成了但没修好"—— 两者对失败归因的意义完全不同。
    """
    return {
        "ok": ok,
        "aborted": aborted,
        "abort_reason": abort_reason,
        "halt_code": halt_code,
        "test_status": comparison["status"] if comparison else None,
        "test_confidence": (comparison or {}).get("confidence"),
        "new_failures": (comparison or {}).get("new_failures", []),
        "validation_ok": None if validation is None else validation.ok,
        "attempts": attempts,
        "review_rounds": review_rounds,
        "reviewer": {
            "enabled": budget.allow_reviewer,
            "decision": None if verdict is None else verdict["decision"],
            "rejections": sum(1 for r in reviewer_rounds if r["decision"] == "revise"),
            "rounds": reviewer_rounds,
        },
        "halting": halting.to_dict(),
        "patch_audit": audit_patch(diff).to_dict(),
        "budget": budget.to_dict(),
        "budget_fingerprint": budget.fingerprint(),
        "ledger": ledger.to_dict(),
        "run_dir": str(run_dir),
    }


def _write_result(run_dir: Path, result_path: str | None, payload: dict) -> None:
    blob = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    try:
        (Path(run_dir) / "result.json").write_text(blob, encoding="utf-8")
        if result_path:
            Path(result_path).parent.mkdir(parents=True, exist_ok=True)
            Path(result_path).write_text(blob, encoding="utf-8")
    except OSError:
        pass          # 写不出结果不该弄死一次已经跑完的运行


# ---------------------------------------------------------------------------
# 辅助：基线、验证、回滚、反馈
# ---------------------------------------------------------------------------
def _run_baselines(ws, profile) -> dict:
    """跑基线。monorepo 会给每个子项目各跑一份。

    【为什么必须给每个子项目单独存基线】
    因为"比较"只能在同一把尺子上进行。改了 web/ 之后去跑 web 的测试，却拿
    根目录的基线来比，得到的差值毫无意义。**你只能比较你测量过的东西。**
    """
    repo = profile.repository
    units = [u for u in (repo.units if repo else []) if u.test_cmd]
    # --test-cmd 是人给的一句明确指令，它压过一切自动探测：
    # 人说"用这条命令跑测试"，就不该背着人再去跑四个子项目各自的命令。
    if profile.command_overridden or repo is None or not repo.is_monorepo or len(units) <= 1:
        return {".": run_tests(ws, profile)}

    picked = units[:MAX_BASELINE_UNITS]
    if len(units) > MAX_BASELINE_UNITS:
        print(f"{YELLOW}  ! 有 {len(units)} 个子项目，只给前 {MAX_BASELINE_UNITS} 个跑基线："
              f"{', '.join(u.path for u in picked)}{RESET}")
    return {u.path: run_tests_in(ws, profile, u.test_cmd, u.path, adapter=u.adapter)
            for u in picked}


def _primary(baselines: dict):
    """给 Planner / executor 看的那份基线：主单元的。"""
    return baselines.get(".") or next(iter(baselines.values()))


def _verify_tests(ws, profile, baselines: dict, modified: list[str], trace):
    """只重跑【受改动影响的】子项目，各自和自己的基线比，再取最差的结论。

    返回 (合并后的对比结论, 主单元的 TestReport)。后者是新增的：停机策略需要
    它的失败指纹来判断"这一轮和上一轮是不是同一个错"，而合并后的 dict 里
    只有字符串摘要，摘要含耗时、每次都不同，做不了相等判断。
    """
    repo = profile.repository
    targets = list(baselines)

    if repo is not None and repo.is_monorepo and modified:
        affected = {u.path for u in repo.units_for(modified)}
        picked = [p for p in baselines if p in affected]
        if picked:
            targets = picked
            trace.event("verify_scope", affected=sorted(affected), running=picked)
            print(f"{DIM}改动集中在 {', '.join(picked)}，只重跑这些子项目的测试{RESET}")

    comparisons, reports = {}, {}
    for path in targets:
        # 根目录那份始终走 profile（它才带着 --test-cmd 覆盖）；
        # 子单元走自己的命令和自己的解析器。必须和 _run_baselines 里的选择一致 ——
        # 基线和验证用了不同的命令，比出来的差值毫无意义。
        unit = _unit(repo, path) if path != "." else None
        cmd = unit.test_cmd if unit else profile.test_cmd
        after = run_tests_in(ws, profile, cmd, path,
                            adapter=unit.adapter if unit else None)
        comparisons[path] = compare(baselines[path], after)
        reports[path] = after

    primary = reports.get(".") or next(iter(reports.values()), None)
    return _merge(comparisons), primary


def _unit(repo, path: str):
    return next((u for u in (repo.units if repo else []) if u.path == path), None)


def _merge(comparisons: dict) -> dict:
    """多个子项目的对比结果合成一个：取最差的那个当总结论。

    "三个模块没事、一个模块回归了"必须报回归 —— 平均一下就把回归洗掉了。
    """
    if len(comparisons) == 1:
        return next(iter(comparisons.values()))

    worst_path = max(comparisons, key=lambda p: _SEVERITY.get(comparisons[p]["status"], 3))
    merged = dict(comparisons[worst_path])
    merged["per_project"] = {p: c["status"] for p, c in comparisons.items()}
    merged["baseline"] = "\n".join(f"[{p}] {c['baseline']}" for p, c in comparisons.items())
    merged["after"] = "\n".join(f"[{p}] {c['after']}" for p, c in comparisons.items())
    # 只要有一个子项目判定不可信，整体结论就不可信
    if any(c.get("confidence") == "low" for c in comparisons.values()):
        merged["confidence"] = "low"
    return merged


def _restore_best(ws, halting: HaltingPolicy, trace) -> None:
    """回滚到历史最佳补丁。失败就保持现状 —— 回滚失败不该再把现场弄坏一次。"""
    best = halting.best
    if best is None or not halting.history:
        return
    if best is halting.history[-1]:
        return                              # 最新的就是最好的，不用动
    ws.reset_hard()
    ok = ws.apply_diff(best.diff)
    trace.event("rollback", to_attempt=best.attempt, failures=best.failures, applied=ok)
    print(f"{YELLOW}[回滚] 已{'' if ok else '尝试'}回到第 {best.attempt} 轮的补丁"
          f"（失败 {best.failures} 个）{'' if ok else '——但补丁重放失败，现场已复位'}{RESET}")


def _unverified_comparison(code: str) -> dict:
    """预算在跑出【任何一次】验证之前就耗尽了，但报告仍然要出。

    这里绝不能填一个看起来正常的状态。写 "no_change" 的话，失败归因和
    报告都会把它当成"真的跑过测试、结果没变"来统计 —— 那是一句凭空捏造的
    观测。补丁没被验证过是另一个事实，得让它在下游一眼可见：
    "unverified" 不在 _OK_STATUSES 里，所以这条实例判不了成功；
    confidence="none" 让它在可信度分布里也和真跑过的那些分开。
    """
    return {"status": "unverified", "confidence": "none",
            "baseline": "(未完成)", "after": f"(预算耗尽 {code}，未执行验证)",
            "new_failures": []}


def _rebuild_comparison(halting: HaltingPolicy, current: dict) -> tuple[dict, None]:
    """回滚之后，对外的结论也要跟着换成最佳检查点那一版的。

    不换会出现一个很难查的错：工作区里是补丁 B，报告里写的却是补丁 C 的成绩。
    """
    best = halting.best
    if best is None:
        return current, None
    restored = dict(current)
    restored["status"] = best.status
    restored["after"] = best.summary or current.get("after", "")
    restored["rolled_back_to_attempt"] = best.attempt
    return restored, None


def _run_scenario(scenario, toolkit, run_dir, label: str):
    """跑复现脚本，并把这一轮的运行证据单独存档（before / after 各一份）。"""
    from . import scenario as scenario_mod

    browser = toolkit.browser
    browser.evidence.start_recording(label)
    result = scenario_mod.run(scenario, browser)
    browser.evidence.save(run_dir, label)
    (run_dir / f"scenario-{label}.txt").write_text(result.render(), encoding="utf-8")
    return result


def _scenario_evidence(before, after) -> str:
    if before is None:
        return ""
    lines = ["修改前：" + ("通过" if before.passed else "未通过"),
             before.render()]
    if after is not None:
        lines += ["", "修改后：" + ("通过" if after.passed else "未通过"), after.render()]
    return "\n".join(lines)


def _execute_context(profile, runtime_note: str, repro_before) -> str:
    """给 executor 的额外背景。只放它真正用得上的，不堆信息。"""
    blocks = []
    repo = profile.repository
    if repo is not None and repo.is_monorepo:
        blocks.append("## 这是一个 monorepo\n" + repo.render()
                      + "\n先判断 issue 属于哪个单元，改完用 run_tests(project=\"…\") 只跑那个单元。")
    if runtime_note:
        blocks.append("## 应用运行状态\n" + runtime_note[:1200])
    if repro_before is not None:
        blocks.append("## 修改前的复现结果（issue 已确认存在）\n" + repro_before.render()
                      + "\n\n改完之后必须让这份【完全相同】的脚本通过。")
    return "\n\n".join(blocks)


def _retry_feedback(attempt: int, comparison: dict, report, validation, repro_after,
                    checkpoint, halting: HaltingPolicy) -> str:
    """把这一轮为什么没过，如实喂回同一份对话。

    分开讲很重要：测试红、类型检查红、复现脚本没过，是三种不同的失败，
    对应三种不同的修法。糊成一句"还没好"，模型只能瞎试。

    喂回去的是【结构化失败证据】（用例 id + 异常 + 源码位置），不是整段日志：
    位置那一列直接告诉它该去改哪个文件，而整段日志只会让它在 warning 里挑错重点。
    还会显式告诉它"和上一轮相比失败是多了还是少了"—— 模型自己不会去比，
    但这恰恰是判断方向对不对的唯一依据。
    """
    parts = [f"第 {attempt} 轮尝试后仍未达标，具体如下："]

    if comparison["status"] not in _OK_STATUSES:
        evidence = report.evidence() if report is not None else comparison["after"]
        parts.append(f"### 测试：{comparison['status']}\n{evidence}")
        if comparison.get("new_failures"):
            parts.append("新增失败（这些是你改出来的回归，优先处理）：\n"
                         + "\n".join(f"  - {n}" for n in comparison["new_failures"][:10]))
        if comparison.get("confidence") == "low":
            parts.append("注意：本项目的测试输出无法被解析成数字，只有 exit_code 可信。"
                         "别依赖失败数变化，直接读测试输出。")
        # 趋势：这一轮是变好了、变坏了，还是原地踏步
        prev = halting.history[-2] if len(halting.history) >= 2 else None
        if prev is not None and checkpoint is not None:
            if checkpoint.signature == prev.signature:
                parts.append("### 趋势：失败集合和上一轮【完全相同】\n"
                             "你这一轮的改动没有改变任何测试结果。不要继续微调 —— "
                             "重新审视根因假设，它很可能一开始就错了。")
            elif checkpoint.failures > prev.failures:
                parts.append(f"### 趋势：失败从 {prev.failures} 个涨到 {checkpoint.failures} 个\n"
                             "这一轮是负收益。先撤回这一轮的改动，再换方向。")
            elif checkpoint.failures < prev.failures:
                parts.append(f"### 趋势：失败从 {prev.failures} 个降到 {checkpoint.failures} 个\n"
                             "方向对，沿着同一条线索继续。")
    elif validation is not None and not validation.ok:
        bad = validation.failed_step
        parts.append(f"### 测试通过了，但验证流水线的 {bad.name} 没过\n{validation.render()}")
        parts.append("这类失败和测试失败一样必须修 —— 单测绿而类型检查/构建红，代码是不能上线的。")
    elif repro_after is not None and not repro_after.passed:
        parts.append("### 测试通过了，但复现脚本仍未通过\n" + repro_after.render())
        parts.append("说明你改的地方没有真正解决 issue 描述的那个行为。"
                     "用 browser_errors / read_service_logs 看运行证据，别猜。")

    if halting.narrowed:
        parts.append("注意：token 预算已用掉八成，explore 工具已被关闭。"
                     "把剩下的额度用在收尾上，不要再开新的探索。")

    parts.append("请分析失败原因，继续修复。如果发现方向错了，先撤回之前的改动思路。")
    return "\n\n".join(parts)
