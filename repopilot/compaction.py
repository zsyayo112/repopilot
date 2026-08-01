"""上下文压缩：让历史不再只增不减。

【为什么这个文件是被数据逼出来的】
dev 划分首轮评测的结论很刺眼：**13 条实例里 6 条，agent 从头到尾一行代码
都没改过**，预算烧光时还停在读文件阶段。完整版平均 54.5 万 token、9 条里
6 条撞穿上限，而一个朴素 ReAct 循环用 27 万 token 解出了**同样的 4 条**。

翻轨迹看得很清楚：上下文的大头不是模型的思考，是**工具返回的内容** ——
十几次 read_file、几十次 search_code、大段 pytest 输出，每一份都永久留在
消息数组里，此后【每一轮请求都要重发一遍】。40 轮下来，重发的成本远远
超过内容本身的价值。

【为什么是"驱逐"而不是"摘要"】
一个自然的想法是让模型总结前面的对话。但那要多花一次模型调用、结果不确定、
而且会把"我读过什么"压成一句可能失真的话。

这里选的是更笨也更可靠的办法：**把陈旧的工具返回值替换成一句占位符，
其余消息一字不动。**

    {"role": "tool", "content": "<4200 字符的文件内容>"}
      ↓
    {"role": "tool", "content": "[已省略 read_file 的 4200 字符输出。需要请重读]"}

它的好处是三条实在的：
  1) **确定性**：同样的输入永远得到同样的结果，不引入新的不确定性
  2) **不破坏消息配对**：只改 content，assistant 的 tool_calls 和 tool 消息
     仍然一一对应 —— 这是最容易踩的坑，删掉一条 tool 消息 API 会直接报错
  3) **保住推理链**：assistant 自己说过的话全部保留。模型的结论比它读过的
     原始内容重要得多 —— "问题在 config.py 第 48 行"这句话值得留，
     而为了得到这句话读过的那四千字符不值得。

【什么不压缩】
  · system 提示词
  · 第一条 user 消息（issue + 计划 + 基线）—— 任务本身，丢了就跑偏
  · 最近 KEEP_RECENT 次工具返回 —— 当前正在处理的证据
  · 短的返回值 —— 压缩它们省不下什么，还平白丢信息

【和 Explorer 的关系】
explorer.py 解决的是同一个问题的另一半：让定位阶段的脏上下文**根本不进来**。
但首轮评测里 `explore` 被调用了 **0 次** —— 一个需要模型主动选择的机制，
在模型不选它的时候等于不存在。压缩是兜底的那一层：它不需要模型配合。
"""

from __future__ import annotations

# 触发线：一次请求的输入 token 超过它就压缩。
#
# 【这个数第一次设成 28_000，是照着"别撑爆 64k 窗口"定的，结果一次都没触发。】
# 实测 pylint-4551：峰值上下文 26,905 tokens —— 对窗口来说很宽裕，
# 但那一轮跑了 40 圈，累计计费 62.7 万 token。
#
# 也就是说我一开始想错了问题：**压缩要解决的不是"单次请求装不下"，
# 是"同一段历史被重发四十遍"。** 每一圈都要把之前所有工具返回值再发一次，
# 于是成本是 (上下文大小 × 圈数)，而不是上下文大小。
# 按这个目标，门槛必须远低于窗口 —— 它该在"历史开始变重"的时候就介入，
# 而不是在"快装不下了"的时候。
COMPACT_AT_TOKENS = 12_000
# 最近这么多次工具返回一字不动 —— 它们是当前正在处理的证据。
KEEP_RECENT = 6
# 短于此的返回值不压缩：省不下什么，还平白丢信息。
MIN_EVICT_CHARS = 400

_PLACEHOLDER = "[已省略 {name} 的 {n} 字符输出以节省上下文。需要的话重新调用工具获取。]"


def should_compact(prompt_tokens: int, threshold: int = COMPACT_AT_TOKENS) -> bool:
    return prompt_tokens >= threshold


def compact(messages: list, keep_recent: int = KEEP_RECENT,
            min_chars: int = MIN_EVICT_CHARS) -> tuple[int, int]:
    """就地压缩消息数组，返回 (被压缩的条数, 省下的字符数)。

    只动 role=="tool" 的 content，其余一律不碰 —— 见文件头第 2 条。
    """
    tool_idx = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    if len(tool_idx) <= keep_recent:
        return 0, 0

    names = _tool_names(messages)
    evicted = saved = 0
    for i in tool_idx[:-keep_recent] if keep_recent else tool_idx:
        content = messages[i].get("content") or ""
        if len(content) < min_chars or content.startswith("[已省略"):
            continue
        messages[i]["content"] = _PLACEHOLDER.format(
            name=names.get(messages[i].get("tool_call_id"), "工具"), n=len(content))
        saved += len(content) - len(messages[i]["content"])
        evicted += 1
    return evicted, saved


def _tool_names(messages: list) -> dict[str, str]:
    """tool_call_id → 工具名。占位符里带上名字，模型才知道"重新调用什么"。"""
    names = {}
    for m in messages:
        for tc in (m.get("tool_calls") or []):
            fn = tc.get("function") or {}
            if tc.get("id") and fn.get("name"):
                names[tc["id"]] = fn["name"]
    return names


def context_chars(messages: list) -> int:
    """当前消息数组的字符总量。用于日志和测试，不是精确 token 数。"""
    return sum(len(m.get("content") or "") for m in messages)
