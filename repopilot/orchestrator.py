"""Agent State Machine：把 Plan-Execute-Verify-Review 串成闭环的总指挥。

LangGraph 卖的就是这张图的框架版；手写出来不过几十行 while + if：

    BASELINE → [START_RUNTIME] → [REPRODUCE] → PLAN → EXECUTE → VERIFY
                                                        │  ▲
                                        不达标且有额度 ──┘  │
                                                        ├──达标/额度耗尽──→ REVIEW
                                                        └──改动超限────────↓
                                                    REPORT → CLEANUP → DONE

方括号里那两个是【条件状态】：不开运行时验证就直接穿过去。

【本轮新增三个状态，以及为什么】
  START_RUNTIME  有一类 bug 只有应用真的跑起来才看得见。这一步负责把
                 前端/后端起来并确认【真的可用】（不是"进程还在"）。
  REPRODUCE      改之前先证明 issue 真的存在。做不到复现，就该停下来说
                 "我认为这个 issue 不成立" —— 那也是一份合格的产出，
                 比自信地猜一个修法有价值。
  CLEANUP        起了服务和浏览器就必须有人负责关。它排在 REPORT 之后、
                 DONE 之前，并且【同时用 try/finally 兜底】—— 因为异常
                 可以在任何状态发生，而漏关的 dev server 会一直占着端口。

两条硬规则没变：
  1) DONE 是【唯一】的真·终止态；所有结局都必须先流经 REPORT 这道出口。
  2) 绝不允许"哑巴终止"（把 state 一设、直接 return，什么都不交代）。
"""

import json
import time

from .adapters import detect
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
from .permissions import Permissions
from .planner import make_plan, render_plan
from .reviewer import review
from .tools import ToolKit
from .trace import Trace
from .verifier import compare, run_tests, run_tests_in, run_validation
from .workspace import Workspace

# 状态严重程度：合并多个子项目的对比结果时，取最差的那个。
# "有一个模块回归了"必须盖过"另外三个模块没事"。
_SEVERITY = {"regressed": 4, "no_change": 3, "improved": 2, "fixed": 1, "still_green": 0}
_OK_STATUSES = ("fixed", "still_green")


def solve(repo: str, issue: str, *, test_cmd: str | None = None,
          yes: bool = False, plan_only: bool = False,
          max_attempts: int = MAX_FIX_ATTEMPTS,
          with_runtime: bool = False, scenario_path: str | None = None,
          ignore_env: bool = False) -> int:
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

    # ---- 能力开关：工具按需暴露，不是有什么就全给（见 tools.py 文件头）----
    groups, scenario, cap_notes = _capabilities(with_runtime, scenario_path, profile)
    trace.event("start", repo=str(ws.root), head=ws.head(),
                project_kind=profile.kind, test_cmd=profile.test_cmd,
                capabilities=list(groups),
                units=[u.path for u in (profile.repository.units if profile.repository else [])])

    print(f"{BOLD}RepoPilot{RESET} @ {ws.root}  (HEAD {ws.head()})")
    print(f"{DIM}{profile.describe()}{RESET}")
    print(f"{DIM}能力：{'、'.join(groups)}{RESET}")
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
        return 2
    print()

    toolkit = ToolKit(ws, profile, groups=groups, artifacts_dir=run_dir, trace=trace)
    perms = Permissions(trust_all=yes)

    try:
        return _loop(ws, profile, issue, toolkit, perms, trace, run_dir,
                     groups=groups, scenario=scenario, plan_only=plan_only,
                     max_attempts=max_attempts)
    finally:
        # 兜底清理。CLEANUP 状态负责正常路径，这里负责异常路径（Ctrl-C、崩溃）。
        # 两层都要有：状态机里那一层是可观察的，finally 这一层是可靠的。
        note = toolkit.cleanup()
        if note != "没有需要清理的资源":
            print(f"{DIM}[清理] {note}{RESET}")
            trace.event("cleanup_final", detail=note)


def _capabilities(with_runtime: bool, scenario_path: str | None, profile):
    """算出这次任务该开哪些工具组。"""
    from .scenario import Scenario

    groups, notes = ["core"], []
    scenario = None

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
          groups, scenario, plan_only, max_attempts) -> int:
    state = "BASELINE"
    attempt = 0
    baselines: dict = {}
    plan = messages = comparison = verdict = None
    validation = None
    repro_before = repro_after = None
    abort_reason = None      # 非 None 表示"跑偏中止"，REPORT 据此走另一套输出
    runtime_note = ""
    exit_code = 1

    while state != "DONE":   # DONE 是唯一真·终止态，一切结局都得先过 REPORT
        trace.event("state", state=state, attempt=attempt)

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
                comparison = {"status": "still_green", "baseline": "(未修改)",
                              "after": "(未修改)", "new_failures": []}
                state = "REPORT"
                continue
            state = "PLAN"

        elif state == "PLAN":
            print(f"{YELLOW}[计划] Planner 正在分析 issue 和仓库结构…{RESET}")
            plan = make_plan(issue, ws, profile, _primary(baselines))
            trace.save("plan.json", render_plan(plan))
            print(render_plan(plan) + "\n")
            state = "DONE" if plan_only else "EXECUTE"
            if plan_only:
                exit_code = 0

        elif state == "EXECUTE":
            attempt += 1
            print(f"{YELLOW}[执行] 第 {attempt}/{max_attempts} 轮…{RESET}")
            if messages is None:
                messages = build_initial_messages(
                    issue, render_plan(plan), _primary(baselines).summary(),
                    groups=groups,
                    extra_context=_execute_context(profile, runtime_note, repro_before),
                )
            summary, _ = run_executor(toolkit, perms, messages, trace)
            trace.event("executor_summary", attempt=attempt, summary=summary[:500])
            state = "VERIFY"

        elif state == "VERIFY":
            print(f"\n{YELLOW}[验证] 修改后再跑一遍测试，与基线对比…{RESET}")
            modified = ws.modified_files()

            # 硬约束：改动规模超限判为跑偏。不送 REVIEW（对一堆乱改跑审查既费钱
            # 又无意义），但【仍要经过 REPORT】——把 diff 和回滚命令交给用户。
            if len(modified) > MAX_MODIFIED_FILES:
                print(f"{RED}[中止] 改动了 {len(modified)} 个文件，超过上限 "
                      f"{MAX_MODIFIED_FILES}：{modified}{RESET}")
                trace.event("abort", reason="modified_files_exceeded", files=modified)
                abort_reason = (f"改动了 {len(modified)} 个文件，超过上限 "
                                f"{MAX_MODIFIED_FILES}，疑似跑偏，已跳过审查。")
                comparison = comparison or {"status": "no_change", "baseline": "-",
                                            "after": "-", "new_failures": []}
                state = "REPORT"
                continue

            comparison = _verify_tests(ws, profile, baselines, modified, trace)
            print(f"{DIM}状态：{comparison['status']}"
                  f"（判定可信度：{comparison.get('confidence', '?')}）\n"
                  f"{comparison['after']}{RESET}")
            trace.event("verify", **comparison)

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
            elif attempt < max_attempts:
                messages.append({"role": "user", "content": _retry_feedback(
                    attempt, comparison, validation, repro_after, baselines)})
                state = "EXECUTE"
            else:
                print(f"{RED}[重试额度耗尽] 仍未达标，进入审查阶段如实报告。{RESET}")
                state = "REVIEW"

        elif state == "REVIEW":
            print(f"\n{YELLOW}[审查] Reviewer 在独立上下文中复查（看不到 executor 的对话史）…{RESET}")
            diff = ws.diff()
            trace.save("final.diff", diff or "(空 diff)")
            verdict = review(issue, plan, diff, comparison,
                             validation=validation.render() if validation else "",
                             scenario_result=_scenario_evidence(repro_before, repro_after))
            trace.save("review.json", json.dumps(verdict, ensure_ascii=False, indent=2))
            state = "REPORT"

        elif state == "REPORT":
            aborted = abort_reason is not None
            ok = (not aborted
                  and comparison["status"] in _OK_STATUSES
                  and (validation is None or validation.ok)
                  and (scenario is None or (repro_after is not None and repro_after.passed))
                  and verdict is not None and verdict.get("verdict") == "approve")
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
                print(f"  审查结论：{color}{verdict.get('verdict')}{RESET} —— "
                      f"{verdict.get('comments', '')}")
                if verdict.get("unrelated_changes"):
                    print(f"  {YELLOW}无关改动：{verdict['unrelated_changes']}{RESET}")
                if verdict.get("missing_tests"):
                    print(f"  {YELLOW}缺少防复发测试{RESET}")
            print(f"\n{ws.diff_stat() or '(无改动)'}")
            print(f"\n{DIM}完整轨迹：{run_dir}/")
            print("满意 → 自己去 commit（agent 被策略禁止 commit，最后一步永远归人）：")
            print(f"    cd {ws.root} && git add -p && git commit")
            print("不满意 → 一键回滚：")
            print(f"    cd {ws.root} && git checkout -- . && git clean -fd{RESET}")
            trace.event("report", ok=ok, status=comparison["status"],
                        verdict=(None if verdict is None else verdict.get("verdict")),
                        validation_ok=(None if validation is None else validation.ok),
                        scenario_passed=(None if repro_after is None else repro_after.passed),
                        aborted=aborted)
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
# 辅助：基线、验证、反馈
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


def _verify_tests(ws, profile, baselines: dict, modified: list[str], trace) -> dict:
    """只重跑【受改动影响的】子项目，各自和自己的基线比，再取最差的结论。"""
    repo = profile.repository
    targets = list(baselines)

    if repo is not None and repo.is_monorepo and modified:
        affected = {u.path for u in repo.units_for(modified)}
        picked = [p for p in baselines if p in affected]
        if picked:
            targets = picked
            trace.event("verify_scope", affected=sorted(affected), running=picked)
            print(f"{DIM}改动集中在 {', '.join(picked)}，只重跑这些子项目的测试{RESET}")

    comparisons = {}
    for path in targets:
        # 根目录那份始终走 profile（它才带着 --test-cmd 覆盖）；
        # 子单元走自己的命令和自己的解析器。必须和 _run_baselines 里的选择一致 ——
        # 基线和验证用了不同的命令，比出来的差值毫无意义。
        unit = _unit(repo, path) if path != "." else None
        cmd = unit.test_cmd if unit else profile.test_cmd
        after = run_tests_in(ws, profile, cmd, path,
                             adapter=unit.adapter if unit else None)
        comparisons[path] = compare(baselines[path], after)

    return _merge(comparisons)


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


def _retry_feedback(attempt: int, comparison: dict, validation, repro_after,
                    baselines: dict) -> str:
    """把这一轮为什么没过，如实喂回同一份对话。

    分开讲很重要：测试红、类型检查红、复现脚本没过，是三种不同的失败，
    对应三种不同的修法。糊成一句"还没好"，模型只能瞎试。
    """
    parts = [f"第 {attempt} 轮尝试后仍未达标，具体如下："]

    if comparison["status"] not in _OK_STATUSES:
        parts.append(f"### 测试：{comparison['status']}\n{comparison['after']}")
        if comparison.get("new_failures"):
            parts.append("新增失败（这些是你改出来的回归，优先处理）：\n"
                         + "\n".join(f"  - {n}" for n in comparison["new_failures"][:10]))
        if comparison.get("confidence") == "low":
            parts.append("注意：本项目的测试输出无法被解析成数字，只有 exit_code 可信。"
                         "别依赖失败数变化，直接读测试输出。")
    elif validation is not None and not validation.ok:
        bad = validation.failed_step
        parts.append(f"### 测试通过了，但验证流水线的 {bad.name} 没过\n{validation.render()}")
        parts.append("这类失败和测试失败一样必须修 —— 单测绿而类型检查/构建红，代码是不能上线的。")
    elif repro_after is not None and not repro_after.passed:
        parts.append("### 测试通过了，但复现脚本仍未通过\n" + repro_after.render())
        parts.append("说明你改的地方没有真正解决 issue 描述的那个行为。"
                     "用 browser_errors / read_service_logs 看运行证据，别猜。")

    parts.append("请分析失败原因，继续修复。如果发现方向错了，先撤回之前的改动思路。")
    return "\n\n".join(parts)
