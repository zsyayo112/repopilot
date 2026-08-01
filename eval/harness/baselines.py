"""三个基线里的前两个。没有它们，RepoPilot 的数字不说明任何事情。

【为什么只测自己等于什么都没测】
"RepoPilot 在 30 条上 resolved 30%" 这句话孤立地看，无法回答一个最基本的
问题：**那个状态机、Explorer、Reviewer、验证重试，到底带来了什么？**
也许把它们全删掉、直接让模型读 issue 写补丁，也能到 27%。那这套工程就是
在给一个 3 个百分点付出十倍的复杂度和成本。

所以必须有下界：

    基线 A（单轮生成）   Issue + 仓库摘要 → 模型直接吐一个 diff。
                        没有工具、没有验证、没有重试。它测的是
                        "这批题目里有多少是纯靠读 issue 猜就能猜对的"。
    基线 B（朴素 ReAct） 搜索 → 阅读 → 修改 → 测试 的裸循环。
                        有工具，但没有状态机、没有 Explorer、没有 Reviewer、
                        没有基线对比、没有停机策略。它测的是
                        "有了工具之后，剩下那些结构还值多少"。
    完整版 RepoPilot     状态机 + Explorer + 验证重试 + Reviewer + 停机策略。

三者【同模型、同实例、同预算】。预算相同这件事由 Budget 对象保证：
基线拿到的是同一个 Budget，只是能力开关不同（见 budget.py 里的 FULL/NO_*）。

【基线要弱得诚实，不要弱得可笑】
一个故意写坏的基线能让主方案的数字很好看，但那是自欺。所以这两个基线：
  · 用同一个模型、同一份 issue、同样的仓库访问权限；
  · 基线 A 拿得到仓库文件清单（不给的话它连路径都写不对，那不是"简单"，是"残废"）；
  · 基线 B 拿得到和主 agent 完全相同的核心工具。
它们唯一少的就是【被消融掉的那个结构】。
"""

from __future__ import annotations

import json
import re

from repopilot.budget import Ledger
from repopilot.config import CYAN, DIM, RESET
from repopilot.llm import create_with_retry
from repopilot.tools import TOOLS, ToolKit

# ---------------------------------------------------------------------------
# 基线 A：单轮生成补丁
# ---------------------------------------------------------------------------
BASELINE_A_SYSTEM = """你是一个修复代码仓库 issue 的工程师。你【只有一次机会】：
读完下面的 issue 和仓库文件清单，直接输出一个可以用 `git apply` 打上的统一 diff。

要求：
- 只输出 diff，不要任何解释文字、不要 markdown 围栏。
- 路径必须来自给出的文件清单，格式为 `diff --git a/路径 b/路径`。
- 必须包含正确的 `@@` 行号信息和上下文行。
- 改动要最小：只改与 issue 直接相关的地方。"""


def run_baseline_a(env, issue: str, budget, trace_dir) -> dict:
    """单轮生成。返回和 orchestrator 同形状的 result 字典。

    它拿不到文件内容，只有文件清单 —— 这正是"单轮"的含义：
    没有工具就没有观察，模型只能凭 issue 和路径名猜。猜中的那些题，
    说明题目本身在 issue 里就写明了答案；那部分成绩不属于任何 agent 设计。
    """
    ledger = Ledger(budget=budget)
    _, tree = _git(env.repo_dir, "ls-files")
    files = "\n".join(tree.splitlines()[:400])

    user = (f"## Issue\n{issue}\n\n## 仓库文件清单\n{files}\n\n"
            "现在直接输出修复用的 unified diff。")
    resp = create_with_retry(
        model=budget.model,
        messages=[{"role": "system", "content": BASELINE_A_SYSTEM},
                  {"role": "user", "content": user}],
        temperature=budget.temperature, max_tokens=budget.max_output_tokens,
    )
    ledger.record_usage(getattr(resp, "usage", None), role="baseline_a")
    raw = resp.choices[0].message.content or ""
    diff = _extract_diff(raw)
    (trace_dir / "baseline_a_raw.txt").write_text(raw, encoding="utf-8")

    if diff.strip():
        applied, note = _apply(env.repo_dir, diff)
    else:
        applied, note = False, "模型没有输出任何 diff"

    return {
        "ok": applied,
        "halt_code": "" if applied else "PATCH_APPLY_FAILED",
        "abort_reason": None if applied else note,
        "attempts": 1, "review_rounds": 0,
        "test_status": None, "validation_ok": None,
        "reviewer": {"enabled": False, "decision": None, "rejections": 0, "rounds": []},
        "halting": {}, "budget": budget.to_dict(),
        "budget_fingerprint": budget.fingerprint(), "ledger": ledger.to_dict(),
    }


def _extract_diff(text: str) -> str:
    """模型总会不听话地包一层 ```diff 围栏，剥掉。"""
    m = re.search(r"```(?:diff|patch)?\s*\n(.+?)```", text, re.S)
    body = m.group(1) if m else text
    start = body.find("diff --git")
    return body[start:] if start >= 0 else body


def _apply(repo_dir, diff: str) -> tuple[bool, str]:
    import subprocess
    proc = subprocess.run(["git", "apply", "--whitespace=nowarn", "-"],
                          input=diff if diff.endswith("\n") else diff + "\n",
                          cwd=repo_dir, capture_output=True, text=True)
    return proc.returncode == 0, (proc.stdout + proc.stderr)[-300:]


def _git(repo_dir, *args):
    from .environment import sh
    return sh(["git", *args], cwd=repo_dir)


# ---------------------------------------------------------------------------
# 基线 B：朴素 ReAct 循环
# ---------------------------------------------------------------------------
BASELINE_B_SYSTEM = """你是一个在真实 git 仓库里修 issue 的编程 agent。
你可以用工具搜索代码、阅读文件、修改文件、跑测试。

工作方式：搜索 → 阅读 → 修改 → 跑测试，反复直到你认为修好了。
修好之后停止调用工具，用一段话说明你改了什么。"""

# 基线 B 的工具集 = 主 agent 核心工具【减去】被消融掉的那两个：
# explore（Explorer 子 agent）和 run_validation（验证流水线）。
# 其余完全相同 —— 消融要只差一项，不能顺手把别的也削了。
BASELINE_B_TOOLS = ("read_file", "list_files", "search_code", "list_symbols",
                    "edit_file", "write_file", "run_tests", "git_diff")


def run_baseline_b(env, issue: str, budget, trace_dir, ws, profile) -> dict:
    """裸 ReAct：一个 while 循环 + 工具分发，没有状态机、没有基线对比。

    注意它【没有】"改之前先跑一遍基线、改之后再跑一遍、比较差值"这一步。
    这是它和完整版最本质的差别：它对"修好了"的判断，来自模型看着测试输出
    自己说的一句话；完整版的判断来自两次测量的差值。
    """
    ledger = Ledger(budget=budget)
    toolkit = ToolKit(ws, profile, groups=("core",), artifacts_dir=trace_dir,
                      budget=budget, ledger=ledger)
    tools = [t for t in TOOLS if t["function"]["name"] in BASELINE_B_TOOLS]
    messages = [{"role": "system", "content": BASELINE_B_SYSTEM},
                {"role": "user", "content": f"## Issue\n{issue}\n\n开始吧。"}]
    transcript = []
    halt_code = ""

    for _ in range(budget.max_turns):
        if over := ledger.exhausted():
            halt_code = over
            break
        resp = create_with_retry(model=budget.model, messages=messages, tools=tools,
                                 temperature=budget.temperature,
                                 max_tokens=budget.max_output_tokens)
        ledger.record_usage(getattr(resp, "usage", None), role="baseline_b")
        msg = resp.choices[0].message
        calls = msg.tool_calls or []
        messages.append({
            "role": "assistant", "content": msg.content or "",
            **({"tool_calls": [{"id": c.id, "type": "function",
                                "function": {"name": c.function.name,
                                             "arguments": c.function.arguments}}
                               for c in calls]} if calls else {}),
        })
        if not calls:
            transcript.append({"final": msg.content or ""})
            break
        for c in calls:
            ledger.note_tool_call()
            try:
                args = json.loads(c.function.arguments or "{}")
            except json.JSONDecodeError as e:
                result = f"错误：参数不是合法 JSON（{e}）"
            else:
                result = toolkit.execute(c.function.name, args)
            print(f"  {CYAN}🔧 {c.function.name}{RESET} {DIM}{str(args)[:70]}{RESET}")
            transcript.append({"tool": c.function.name, "args": args,
                               "result": result[:400]})
            messages.append({"role": "tool", "tool_call_id": c.id, "content": result})
    else:
        halt_code = "MAX_TURNS"

    (trace_dir / "baseline_b_trace.json").write_text(
        json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "ok": not halt_code,
        "halt_code": halt_code,
        "abort_reason": None,
        "attempts": 1, "review_rounds": 0,
        "test_status": None, "validation_ok": None,
        "reviewer": {"enabled": False, "decision": None, "rejections": 0, "rounds": []},
        "halting": {}, "budget": budget.to_dict(),
        "budget_fingerprint": budget.fingerprint(), "ledger": ledger.to_dict(),
    }
