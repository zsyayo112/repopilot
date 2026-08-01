"""推理侧：输入 Issue + Base Commit，输出 Agent Patch + Trace。仅此而已。

**本模块【不】import grader、【不】import SecretInstance、【不】读 F2P/P2P。**
这条约束由 tests/test_eval_isolation.py 用 AST 静态检查守住 —— 靠自觉守不住的
东西，就该让 CI 来守。

三个变体走同一个入口，因为"同预算、同实例、同模型"这句话只有在同一段代码里
才做得到。变体之间的差别全部收敛成两样东西：
  variant 名字（决定调哪个 agent）
  Budget   （决定它被允许用什么、花多少）
"""

from __future__ import annotations

import json
import time
import traceback
from pathlib import Path

from repopilot.adapters import detect
from repopilot.budget import Budget
from repopilot.workspace import Workspace

from .environment import Env, assert_command_public, capture_patch, reset
from .instances import PublicInstance, assert_public_only

VARIANTS = ("full", "no-reviewer", "no-explorer", "no-halting", "baseline-a", "baseline-b")


def budget_for(variant: str, base: Budget) -> Budget:
    """变体 → 预算。**规模和 token 完全一致，只有能力开关不同。**

    这是"统一预算后再比较"那条要求的落点：想说明 Reviewer 有用，就不能让
    带 Reviewer 的那版顺便多拿 100k token —— 那样比出来的是钱，不是设计。
    """
    # 每个变体的能力开关【全部显式钉死】，不继承 Budget 的默认值 ——
    # 产品侧的默认值会跟着证据改（Reviewer 已改成默认关），而消融变体的
    # 定义一旦被默认值牵着走，"full"就会在某次改默认后悄悄变成另一个配置，
    # 指纹和历史全对不上。
    return {
        "full": base.variant(allow_reviewer=True, allow_explorer=True,
                             allow_halting_policy=True),
        "no-reviewer": base.variant(allow_reviewer=False, allow_explorer=True,
                                    allow_halting_policy=True),
        "no-explorer": base.variant(allow_reviewer=True, allow_explorer=False,
                                    allow_halting_policy=True),
        "no-halting": base.variant(allow_reviewer=True, allow_explorer=True,
                                   allow_halting_policy=False),
        # 两个基线本来就没有这些能力，显式关掉，让 fingerprint 如实反映它们
        "baseline-a": base.variant(allow_reviewer=False, allow_explorer=False,
                                   allow_halting_policy=False, max_turns=1,
                                   max_fix_attempts=1),
        "baseline-b": base.variant(allow_reviewer=False, allow_explorer=False,
                                   allow_halting_policy=False, max_fix_attempts=1),
    }[variant]


def run_one(inst: PublicInstance, env: Env, *, variant: str, budget: Budget,
            out_dir: Path) -> dict:
    """对一个实例跑一次推理。返回结果字典，并把补丁写进 out_dir/agent.patch。

    返回值里【没有】resolved —— 推理侧根本不知道自己有没有做对。
    那是判分侧的事，而且判分要等所有推理都跑完之后独立进行。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()

    # 事前断言：真正发给 agent 的东西过一遍禁词。这道检查很便宜，
    # 但它是"我们能证明不存在隐藏测试泄露"这句话唯一的物证。
    payload = {"instance": inst.to_dict(), "test_cmd": env.test_cmd}
    assert_public_only(payload, "推理侧输入")
    assert_command_public(env.test_cmd)
    (out_dir / "inference_input.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # 每次推理前强制复位：上一个 agent 版本的补丁、判分阶段打上的 test_patch、
    # 上一轮的半成品，一样都不许带进来（见 environment.py 的缓存策略）。
    reset(env)

    issue_file = out_dir / "issue.md"
    issue_file.write_text(inst.problem_statement, encoding="utf-8")

    result: dict
    try:
        result = _dispatch(variant, inst, env, budget, out_dir)
    except Exception as e:                      # 单个实例炸掉绝不弄死整批
        result = {"ok": False, "halt_code": "HARNESS_ERROR",
                  "abort_reason": f"{type(e).__name__}: {e}",
                  "budget": budget.to_dict(),
                  "budget_fingerprint": budget.fingerprint(),
                  "ledger": {}, "attempts": 0, "review_rounds": 0,
                  "reviewer": {"enabled": budget.allow_reviewer, "rejections": 0,
                               "rounds": []}, "halting": {}}
        (out_dir / "error.txt").write_text(traceback.format_exc(), encoding="utf-8")

    patch = capture_patch(env)
    (out_dir / "agent.patch").write_text(patch, encoding="utf-8")

    result.update(instance_id=inst.instance_id, variant=variant,
                  wall_secs=round(time.time() - started, 1),
                  patch_bytes=len(patch), empty_patch=not patch.strip())
    (out_dir / "inference.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    # 交完卷立刻复位。下一个变体必须从同一个 pristine 起点开始 ——
    # 否则 no-reviewer 版会站在 full 版的补丁上开跑，比较就成了笑话。
    reset(env)
    return result


def _dispatch(variant: str, inst: PublicInstance, env: Env, budget: Budget,
              out_dir: Path) -> dict:
    if variant.startswith("baseline"):
        from . import baselines
        ws = Workspace(env.repo_dir)
        if variant == "baseline-a":
            return baselines.run_baseline_a(env, inst.problem_statement, budget, out_dir)
        profile = detect(env.repo_dir, env.test_cmd)
        return baselines.run_baseline_b(env, inst.problem_statement, budget,
                                        out_dir, ws, profile)

    # 完整版及其消融：走真正的状态机
    from repopilot.orchestrator import solve

    result_path = out_dir / "solve_result.json"
    solve(str(env.repo_dir), inst.problem_statement,
          test_cmd=env.test_cmd, yes=True, budget=budget,
          result_path=str(result_path))
    if result_path.exists():
        return json.loads(result_path.read_text())
    return {"ok": False, "halt_code": "NO_RESULT",
            "abort_reason": "solve 没有写出 result.json —— 视为崩溃",
            "budget": budget.to_dict(), "budget_fingerprint": budget.fingerprint(),
            "ledger": {}, "attempts": 0, "review_rounds": 0,
            "reviewer": {"enabled": budget.allow_reviewer, "rejections": 0, "rounds": []},
            "halting": {}}
