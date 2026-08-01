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
from pathlib import Path

from repopilot.budget import Ledger
from repopilot.config import CYAN, DIM, RESET
from repopilot.llm import create_with_retry
from repopilot.tools import TOOLS, ToolKit

# ---------------------------------------------------------------------------
# 基线 A：单轮生成补丁
# ---------------------------------------------------------------------------
# 【为什么不让它输出 unified diff】
# 第一版让它直接吐 `git apply` 能用的 diff，实测结果是一份退化的输出：
# 几十个几乎相同的 `@@` hunk，从头到尾每隔十行加一个空行，行号全是编的。
# 原因是结构性的 —— 基线 A 只拿得到【文件清单】，从来没看过文件内容，
# 它**不可能**写出正确的上下文行和行号。于是这个基线测出来的一大半是
# "会不会写 diff 语法"，而不是"会不会修 bug"。
#
# 换成 search/replace 编辑格式之后，混淆项消失了：
#   它知道那段代码长什么样  → search 块能命中 → 考的是定位和修法
#   它不知道               → 命中不了       → 诚实地失败在该失败的地方
# 这个格式也和 RepoPilot 自己的 edit_file 工具同语义（唯一命中才替换），
# 前后一致。约束没有变松：仍然是一次调用、没有工具、没有验证、没有重试。
BASELINE_A_SYSTEM = """你是一个修复代码仓库 issue 的工程师。你【只有一次机会】：
读完下面的 issue 和仓库文件清单，直接给出要做的代码修改。

只输出一个 JSON 对象：
{"edits": [{"path": "相对路径", "search": "要被替换的原始代码片段", "replace": "替换后的代码"}]}

要求：
- path 必须来自给出的文件清单。
- search 必须是文件里【逐字符精确存在且唯一】的一段代码（含缩进），
  凭你对这个项目的了解写出来。宁可多带几行上下文保证唯一，也不要写错。
- replace 是这段代码替换后的样子。
- 改动要最小：只改与 issue 直接相关的地方。
- 不要输出解释文字，不要 markdown 围栏。"""


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
            "现在直接输出修改。")
    resp = create_with_retry(
        model=budget.model,
        messages=[{"role": "system", "content": BASELINE_A_SYSTEM},
                  {"role": "user", "content": user}],
        temperature=budget.temperature, max_tokens=budget.max_output_tokens,
        response_format={"type": "json_object"},
    )
    ledger.record_usage(getattr(resp, "usage", None), role="baseline_a")
    choice = resp.choices[0]
    raw = choice.message.content or ""
    (trace_dir / "baseline_a_raw.txt").write_text(raw, encoding="utf-8")

    # 【必须单独认出"被截断"这一种失败】否则它长得和"改错了"一模一样。
    # 但前者是【我们的预算】掐断了它唯一一次输出，后者才是它自己的能力问题。
    # 实测踩过：4096 的上限把输出从中间切断，基线 A 因此挂零 ——
    # 那是我们让它输的，不是它输的。把两者混在一起，等于把评测者自己造成的
    # 失败记到被测方案头上。
    truncated = getattr(choice, "finish_reason", "") == "length"

    if truncated:
        applied, note = False, (
            f"模型输出被 max_output_tokens={budget.max_output_tokens} 截断"
            "（预算造成的失败，不是能力问题）")
    else:
        applied, note = _apply_edits(env.repo_dir, raw)

    return {
        "ok": applied,
        "halt_code": ("" if applied else
                      "OUTPUT_TRUNCATED" if truncated else "PATCH_APPLY_FAILED"),
        "abort_reason": None if applied else note,
        "output_truncated": truncated,
        "attempts": 1, "review_rounds": 0,
        "test_status": None, "validation_ok": None,
        "reviewer": {"enabled": False, "decision": None, "rejections": 0, "rounds": []},
        "halting": {}, "budget": budget.to_dict(),
        "budget_fingerprint": budget.fingerprint(), "ledger": ledger.to_dict(),
    }


def _apply_edits(repo_dir, raw: str) -> tuple[bool, str]:
    """把 search/replace 编辑落到磁盘上。语义和 RepoPilot 的 edit_file 一致：
    **必须唯一命中才替换** —— 命中多处说明片段不够具体，盲目替换会误伤。

    失败原因要分得细，因为它们说的是不同的事：
      JSON 都不合法        → 连格式都没遵守
      search 一次都没命中  → 它不知道这段代码长什么样（定位/知识问题）
      search 命中多处      → 它给的片段不够唯一（表达问题）
    这三种混成一句"补丁打不上"，就没法回答"单轮到底卡在哪一步"。
    """
    try:
        payload = json.loads(_strip_fence(raw))
        edits = payload.get("edits") or []
    except (json.JSONDecodeError, AttributeError) as e:
        return False, f"输出不是合法 JSON：{e}"
    if not edits:
        return False, "模型没有给出任何修改"

    applied, problems = 0, []
    for i, ed in enumerate(edits):
        path, search, replace = (ed.get("path"), ed.get("search"), ed.get("replace"))
        if not path or search is None or replace is None:
            problems.append(f"第 {i + 1} 条编辑字段不全")
            continue
        target = Path(repo_dir) / path
        if not target.is_file():
            problems.append(f"{path} 不存在")
            continue
        text = target.read_text(encoding="utf-8", errors="replace")
        hits = text.count(search)
        if hits == 0:
            problems.append(f"{path}: search 片段没有命中（它写的代码和真实代码对不上）")
        elif hits > 1:
            problems.append(f"{path}: search 片段命中 {hits} 处，不唯一")
        else:
            target.write_text(text.replace(search, replace, 1), encoding="utf-8")
            applied += 1

    if applied:
        return True, (f"应用了 {applied}/{len(edits)} 条编辑"
                      + (f"；未应用：{'；'.join(problems[:2])}" if problems else ""))
    return False, "；".join(problems[:3]) or "没有任何编辑被应用"


def _strip_fence(text: str) -> str:
    """模型总会不听话地包一层 ```json 围栏，剥掉。"""
    m = re.search(r"```(?:json)?\s*\n(.+?)```", text, re.S)
    return (m.group(1) if m else text).strip()


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
