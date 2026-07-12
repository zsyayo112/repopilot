"""Independent Reviewer：独立上下文复查 —— 子 agent 上下文隔离的直系应用。

关键设计：Reviewer 故意【看不到】executor 的对话史。
那份历史里全是"我改好了"的自我叙事，看了必然被带偏（自己查自己，永远通过）。
Reviewer 只看四样客观材料：issue、计划、diff、测试前后对比 —— 证据，不是叙述。

这就是 L7 的教训在工程里的样子：隔离上下文不只是省 token，更是【去偏见】。
"""

from .llm import json_call
from .planner import render_plan

REVIEWER_SYSTEM = """你是一位严格的高级代码审查员。你将看到：一个 issue、修复计划、
最终代码 diff、以及修改前后的测试结果对比。请独立判断这次修复的质量。

只输出一个 JSON 对象，字段如下：
- "solves_issue": bool，diff 是否真正解决了 issue 描述的问题（而不是绕过或掩盖）
- "unrelated_changes": 列表，与 issue 无关的改动（没有则为空列表）
- "missing_tests": bool，是否缺少能防止此问题复发的测试
- "risks": 列表，这个 diff 可能引入的回归风险
- "verdict": "approve" 或 "revise"
- "comments": 一段给人看的审查意见（中文，简洁）

审查立场：宁可错杀不可放过。diff 里任何看不懂的改动都算风险。"""


def review(issue: str, plan: dict, diff: str, comparison: dict) -> dict:
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
基线：{comparison['baseline']}
修改后：{comparison['after']}
"""
    return json_call(REVIEWER_SYSTEM, user)
