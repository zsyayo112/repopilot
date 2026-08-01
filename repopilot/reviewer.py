"""Independent Reviewer：独立上下文复查 —— 子 agent 上下文隔离的直系应用。

关键设计：Reviewer 故意【看不到】executor 的对话史。
那份历史里全是"我改好了"的自我叙事，看了必然被带偏（自己查自己，永远通过）。
Reviewer 只看客观材料：issue、计划、diff、测试前后对比、验证流水线结果、
复现脚本前后结果 —— **证据，不是叙述**。

这就是上下文隔离的另一面：隔离不只是省 token，更是【去偏见】。
（同一套机制的另一次应用是 explorer.py，那次的收益是省上下文。）

【为什么输出必须是结构化的，而不是"通过/不通过"】
"我们有 Reviewer" 只是一句架构描述。要证明它有效，得能回答四个问题：

    它驳回了多少次？          → decision == "revise" 的计数
    驳回之后改对了吗？        → 驳回轮之后是否转为 resolved（挽救）
    它误杀过正确的补丁吗？    → 驳回了、但那个补丁其实能过官方判分（误杀）
    它多花了多少 token 和时间？→ ledger 里 role="reviewer" 的分账

前三个问题都需要"它到底要求改什么"这个字段 —— 一个布尔值答不了。
所以 requested_actions 是这次改造里最重要的字段：它既是给 agent 的下一轮
指令，也是判断"Reviewer 是不是在车轱辘话"的依据（见 halting.reviewer_repeats）。
"""

from .llm import json_call
from .planner import render_plan

REVIEWER_SYSTEM = """你是一位严格的高级代码审查员。你将看到：一个 issue、修复计划、
最终代码 diff、修改前后的测试结果对比，可能还有验证流水线（lint/类型检查/构建）
结果和浏览器复现脚本的前后结果。请独立判断这次修复的质量。

只输出一个 JSON 对象，字段如下：
- "decision": "accept" 或 "revise"
- "issue_addressed": bool，diff 是否真正解决了 issue 描述的问题（而不是绕过或掩盖）
- "tests_sufficient": bool，现有测试是否足以防止此问题复发
- "scope_minimal": bool，改动范围是否最小（没有夹带无关重构/格式化）
- "unrelated_changes": 列表，与 issue 无关的改动（没有则为空列表）
- "risks": 列表，这个 diff 可能引入的回归风险
- "requested_actions": 列表，要求 agent 具体做什么才能通过（accept 时为空列表）。
  每条必须是一个可执行的动作，例如"给 parse_config 增加空输入的测试"，
  不要写"建议提高代码质量"这种无法执行的话。
- "comments": 一段给人看的审查意见（中文，简洁）

审查立场：宁可错杀不可放过。diff 里任何看不懂的改动都算风险。
但 requested_actions 必须克制：只写【非改不可】的，不写"顺手也可以做"的 ——
每多一条都会让 agent 多烧一轮 token。
特别注意三件事：
1) 如果测试对比的可信度是 low，说明测试数字没能被解析，"没有新增失败"这个结论
   本身就不可靠 —— 不要把它当成安全的证据。
2) 如果 diff 修改了测试文件本身，要格外警惕：把测试改绿不等于把代码改对。
   删断言、加 skip/xfail、放宽期望值一律判 revise。
3) issue 没被真正解决时，即使测试全绿也必须 revise。"""


def review(issue: str, plan: dict, diff: str, comparison: dict,
           validation: str = "", scenario_result: str = "",
           audit: dict | None = None, *, budget=None, ledger=None) -> dict:
    user = f"""## Issue
{issue}

## 修复计划
{render_plan(plan)}

## 最终 Diff
```diff
{diff[:8000]}
```

## 测试对比
状态：{comparison['status']}
判定可信度：{comparison.get('confidence', '未知')}
基线：{comparison['baseline']}
修改后：{comparison['after']}
"""
    if comparison.get("per_project"):
        user += f"各子项目状态：{comparison['per_project']}\n"
    if validation:
        user += f"\n## 验证流水线（lint / 类型检查 / 构建）\n{validation[:2000]}\n"
    if scenario_result:
        user += f"\n## 浏览器复现脚本（修改前 vs 修改后）\n{scenario_result[:3000]}\n"
    # 机器先扫一遍补丁策略（改测试、加 skip、删断言…），把客观事实摆在
    # Reviewer 面前。模型读 diff 会漏，正则不会 —— 两者是互补而不是替代。
    if audit and audit.get("flags"):
        user += ("\n## 补丁策略自动检查（机器扫描结果，属于客观事实）\n"
                 + "\n".join(f"- {f}" for f in audit["flags"]) + "\n")

    verdict = json_call(REVIEWER_SYSTEM, user,
                        budget=budget, ledger=ledger, role="reviewer")
    return normalize(verdict)


def normalize(v: dict) -> dict:
    """把模型的输出补齐成固定形状。

    为什么必须做这一步：下游（状态机、报告、消融统计）会直接读这些字段。
    模型偶尔漏一个字段是常态，而 `verdict.get("requested_actions")` 返回
    None 时 `for a in None` 会当场炸掉整个 run —— 一次审查的格式抖动
    不该毁掉一次要花几分钟和真金白银的运行。

    同时保留旧字段名 verdict/solves_issue/missing_tests：
    老的测试、trace 消费者和 README 都还在用它们。
    """
    decision = str(v.get("decision") or
                   ("accept" if v.get("verdict") == "approve" else "revise")).lower()
    if decision not in ("accept", "revise"):
        decision = "revise"          # 看不懂就当没通过：审查失败不该被当成通过

    actions = v.get("requested_actions") or []
    if isinstance(actions, str):
        actions = [actions]

    issue_addressed = v.get("issue_addressed")
    if issue_addressed is None:
        issue_addressed = bool(v.get("solves_issue", decision == "accept"))
    tests_sufficient = v.get("tests_sufficient")
    if tests_sufficient is None:
        tests_sufficient = not bool(v.get("missing_tests", False))

    return {
        "decision": decision,
        "issue_addressed": bool(issue_addressed),
        "tests_sufficient": bool(tests_sufficient),
        "scope_minimal": bool(v.get("scope_minimal", True)),
        "unrelated_changes": list(v.get("unrelated_changes") or []),
        "risks": list(v.get("risks") or []),
        "requested_actions": [str(a) for a in actions],
        "comments": str(v.get("comments") or ""),
        # --- 兼容旧字段名 ---
        "verdict": "approve" if decision == "accept" else "revise",
        "solves_issue": bool(issue_addressed),
        "missing_tests": not bool(tests_sufficient),
    }


def render_request(verdict: dict, round_no: int) -> str:
    """把驳回意见变成给 executor 的下一轮输入。

    只回填 requested_actions，不回填 comments —— comments 是给人看的评语，
    喂给模型只会变成一段它复述一遍然后照旧的客套话。
    """
    actions = verdict.get("requested_actions") or ["审查未通过，但没给出具体要求。"]
    lines = [f"第 {round_no} 轮独立审查【未通过】。审查员看不到你的对话历史，"
             "只看了 issue、diff 和测试证据，所以下面这些是对结果本身的判断：", ""]
    lines += [f"{i}. {a}" for i, a in enumerate(actions, 1)]
    if verdict.get("unrelated_changes"):
        lines += ["", "另外这些改动被判定与 issue 无关，请撤回："]
        lines += [f"  - {c}" for c in verdict["unrelated_changes"]]
    lines += ["", "请按上面的要求修改，然后照常跑测试验证。"
              "如果你认为某条要求是错的，就明确说明理由并保持原样 —— "
              "不要为了让审查通过而改坏代码。"]
    return "\n".join(lines)
