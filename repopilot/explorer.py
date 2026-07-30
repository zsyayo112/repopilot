"""Explorer：一个只读的探索子 agent，用来治"定位阶段污染主上下文"。

【它治的是哪个具体问题】
上下文峰值的大头不是模型的思考，是**工具返回的文件内容**。定位阶段是最脏的：
为了找到 bug 在哪，可能要读十个文件，其中九个读完发现没用 —— 但这九个文件
的全文已经【永久留在主上下文里】了，后面每一轮请求都要重新把它们发一遍。

子 agent 的价值不是"分工"，是**上下文隔离**：

    主循环  ──问──▶  Explorer（自己一份全新的对话历史）
                        ├─ read_file × 10   ← 这些脏东西全留在它那边
                        └─ 回一句结论
    主循环  ◀─结论──   "问题在 seaborn/_core/scales.py 的 116-134 行"

主 agent 不需要知道它读了十个文件，只需要知道那句结论。

【和 Reviewer 是同一套模式的第二次应用】
Reviewer 也是"新开一份消息数组 + 受限输入 + 只回结论"。区别是目的：
Reviewer 隔离是为了【去偏见】（自己审自己永远通过），
Explorer 隔离是为了【省上下文】。同一个机制，两种收益。

【什么时候不该拆子 agent】（这条比会拆更重要）
  · 要传进去的上下文超过主上下文的 30% —— 传递成本接近不拆，还多一层损耗
  · 需要频繁来回问 —— 每次往返两次模型调用，通信开销 > 收益
  · 拆得太细 —— 读文件、搜索【该是工具不是 agent】：
    需要多轮推理才配叫 agent，一次调用出结果的叫函数
"""

from __future__ import annotations

import json

from .config import DIM, EXPLORER_MAX_OUTPUT, EXPLORER_MAX_TURNS, MODEL, RESET
from .llm import create_with_retry

# 只给只读工具。这不是"为了安全"那种泛泛的说法，是有具体后果的：
# 一个能改文件的探索 agent，会在主 agent 还没决定方案之前就动手改代码 ——
# 主 agent 之后拿到的 diff 里混着自己没做过的改动，无从解释。
READONLY_TOOL_NAMES = {"read_file", "list_files", "search_code", "list_symbols", "git_diff"}

EXPLORER_SYSTEM = """你是一个代码库探索助手。你的唯一任务是回答一个定位问题，然后把结论交出去。

工作方式：
- 用 search_code 先缩小范围，用 list_symbols 看大文件骨架，再用 read_file 的行号范围精读。
- 你【只有只读工具】，不要试图修改任何文件。
- 大胆多读几个文件 —— 你的上下文是独立的，读脏了不影响主流程。

输出要求（非常重要）：
- 确认结论后就停止调用工具，直接用一段话回答。
- 结论必须具体到【文件路径 + 行号范围 + 一句话说明】。
- 你读过的文件内容【不要】复制到结论里，只写结论和关键的几行代码。
- 如果找不到，就明确说"没找到"，并说明你排除了哪些可能 —— 空结论也是有用的结论，
  比编一个像样的答案有用得多。"""


def explore(toolkit, question: str, trace=None, quiet: bool = False) -> str:
    """在隔离上下文里回答一个定位问题，只返回结论文本。

    注意它复用主 toolkit 实例（同一个仓库、同一套护栏），但【不复用 messages】——
    隔离的是对话历史，不是能力。
    """
    from .tools import TOOLS  # 延迟导入：tools 会导入本模块，避免环形依赖

    tools = [t for t in TOOLS if t["function"]["name"] in READONLY_TOOL_NAMES]
    messages = [
        {"role": "system", "content": EXPLORER_SYSTEM},
        {"role": "user", "content": f"定位问题：{question}"},
    ]
    calls = 0

    for _ in range(EXPLORER_MAX_TURNS):
        resp = create_with_retry(model=MODEL, messages=messages, tools=tools)
        msg = resp.choices[0].message
        tool_calls = msg.tool_calls or []

        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            **({"tool_calls": [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in tool_calls]} if tool_calls else {}),
        })

        if not tool_calls:
            conclusion = (msg.content or "").strip() or "（探索子 agent 没有给出结论）"
            if trace:
                trace.event("explorer_done", question=question, calls=calls,
                            conclusion=conclusion[:500])
            return _cap(conclusion)

        for tc in tool_calls:
            calls += 1
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError as e:
                result = f"错误：参数不是合法 JSON（{e}）"
            else:
                # 双重保险：即使模型凭记忆调了个写工具，这里也拦住
                result = (toolkit.execute(name, args) if name in READONLY_TOOL_NAMES
                          else f"错误：探索子 agent 只能使用只读工具，{name} 不可用")
            if not quiet:
                print(f"{DIM}    ↳ explorer: {name}({str(args)[:60]}){RESET}")
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    if trace:
        trace.event("explorer_max_turns", question=question, calls=calls)
    return ("（探索子 agent 用完了圈数还没给出结论。这通常说明问题问得太大了，"
            "把它拆成更小的定位问题重问。）")


def _cap(text: str) -> str:
    """结论必须短。它要是长篇大论，上下文隔离就白做了 —— 脏东西换了个方式进来。"""
    if len(text) <= EXPLORER_MAX_OUTPUT:
        return text
    return text[:EXPLORER_MAX_OUTPUT] + "\n…[结论过长被截断，说明子 agent 没有收敛到一句话]"
