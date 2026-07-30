"""Independent Reviewer：独立上下文复查 —— 子 agent 上下文隔离的直系应用。

关键设计：Reviewer 故意【看不到】executor 的对话史。
那份历史里全是"我改好了"的自我叙事，看了必然被带偏（自己查自己，永远通过）。
Reviewer 只看客观材料：issue、计划、diff、测试前后对比、验证流水线结果、
复现脚本前后结果 —— **证据，不是叙述**。

这就是上下文隔离的另一面：隔离不只是省 token，更是【去偏见】。
（同一套机制的另一次应用是 explorer.py，那次的收益是省上下文。）
"""

from .llm import json_call
from .planner import render_plan

REVIEWER_SYSTEM = """你是一位严格的高级代码审查员。你将看到：一个 issue、修复计划、
最终代码 diff、修改前后的测试结果对比，可能还有验证流水线（lint/类型检查/构建）
结果和浏览器复现脚本的前后结果。请独立判断这次修复的质量。

只输出一个 JSON 对象，字段如下：
- "solves_issue": bool，diff 是否真正解决了 issue 描述的问题（而不是绕过或掩盖）
- "unrelated_changes": 列表，与 issue 无关的改动（没有则为空列表）
- "missing_tests": bool，是否缺少能防止此问题复发的测试
- "risks": 列表，这个 diff 可能引入的回归风险
- "verdict": "approve" 或 "revise"
- "comments": 一段给人看的审查意见（中文，简洁）

审查立场：宁可错杀不可放过。diff 里任何看不懂的改动都算风险。
特别注意两件事：
1) 如果测试对比的可信度是 low，说明测试数字没能被解析，"没有新增失败"这个结论
   本身就不可靠 —— 不要把它当成安全的证据。
2) 如果 diff 修改了测试文件本身，要格外警惕：把测试改绿不等于把代码改对。"""


def review(issue: str, plan: dict, diff: str, comparison: dict,
           validation: str = "", scenario_result: str = "") -> dict:
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
    return json_call(REVIEWER_SYSTEM, user)
