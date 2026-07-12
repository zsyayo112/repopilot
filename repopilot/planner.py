"""Planner：先想清楚，再动手。

一次普通的 LLM 调用 + 强制 JSON 输出，没有任何魔法。计划的价值有二：
  1) 给 executor 当路线图，避免它在大仓库里乱逛烧 token
  2) 存进 trace —— 事后能看到"它当时是怎么想的"，debug 和评测都靠这个

设计取舍：Planner 只拿到【目录树 + issue + 基线测试结果】，不拿文件内容。
它的假设可以错 —— executor 动手前会用 search/read 核实。这比让 Planner
先读一堆文件便宜得多（Plan 阶段的上下文成本是固定的小常数）。
"""

import json

from .llm import json_call

PLANNER_SYSTEM = """你是资深软件工程师，负责为一个代码仓库的 issue 修复任务制定计划。

只输出一个 JSON 对象，字段如下：
- "root_cause_hypothesis": 对问题根因的假设，具体到模块/函数级（字符串）
- "files_to_inspect": 最值得先查看的文件路径列表（不超过 5 个，必须来自给出的文件清单）
- "change_steps": 修改步骤列表，每步一句话，可执行、可检查
- "test_plan": 如何验证修复成功（字符串）
- "risks": 这次修改可能引入的风险列表

要求：假设可以不确定，但必须具体、可验证。不要泛泛而谈。"""


def make_plan(issue: str, ws, profile, baseline) -> dict:
    user = f"""## Issue
{issue}

## 项目信息
{profile.describe()}

## 基线测试结果（修改前）
{baseline.summary()}

## 仓库文件清单
{ws.tree_summary()}
"""
    return json_call(PLANNER_SYSTEM, user)


def render_plan(plan: dict) -> str:
    """把计划渲染成人能快速扫读的样子（打印 & 存档共用）。"""
    return json.dumps(plan, ensure_ascii=False, indent=2)
