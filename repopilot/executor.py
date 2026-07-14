"""Executor：真正动手干活的主循环。骨架与 agent/loop.py 完全同源 ——

    问模型 → 有 tool_calls 就执行、喂回 → 再问，直到它不再要工具。

流式拼装（_stream_once）原样继承。不同点：
  - 工具来自 ToolKit 实例（绑定具体仓库），不再是模块级函数
  - 每个工具调用都写进 trace（黑匣子）
  - 支持"续跑"：验证失败后，orchestrator 把失败报告作为新 user 消息
    追加进同一份 messages 再来一轮 —— 上下文连续，模型记得自己上轮改了什么
"""

import json

from .config import BOLD, CYAN, DIM, MAX_TURNS, MODEL, RED, RESET, YELLOW
from .llm import create_with_retry
from .permissions import Permissions
from .tools import SAFE_TOOLS, TOOLS, ToolKit
from .trace import Trace

EXECUTOR_SYSTEM = """你是一个在真实 git 仓库里解决 issue 的编程 agent。当前工作目录就是仓库根目录。

工作方法：
- 给你的计划只是假设：动手改之前，必须先用 search_code / read_file 核实它是否成立。
- 大文件先 list_symbols 看骨架，再用 read_file 的行号范围分段读，不要整读。
- 修改一律用 edit_file（精确片段替换）；只有创建全新文件才用 write_file。
- 每次实质修改后立刻 run_tests 验证；失败就读输出、分析、修正、再跑。
- 跑测试只用 run_tests 工具，不要用 run_bash 自己拼测试命令。
- 严禁改动与 issue 无关的文件；严禁 cd；所有路径相对仓库根目录。
- 测试全部通过后：用 git_diff 自查改动是否最小、有无误伤，然后停止调用工具，
  用一段话总结：改了什么、为什么、测试结果如何。"""


def _stream_once(messages: list) -> tuple[str, list, int]:
    """一次流式请求：边收边打印文本，拼装 tool_calls 碎片。原样继承 agent/loop.py。"""
    # create_with_retry：503 过载/限流/超时自动退避重试（见 llm.py 顶部说明）。
    # 只保护"发起请求"这一步；流已经开始后中断的情况极少，MVP 不做续流。
    stream = create_with_retry(
        model=MODEL, messages=messages, tools=TOOLS,
        stream=True, stream_options={"include_usage": True},
    )

    content_parts: list[str] = []
    tool_buf: dict[int, dict] = {}
    prompt_tokens = 0
    printed_header = False

    for chunk in stream:
        if chunk.usage:
            prompt_tokens = chunk.usage.prompt_tokens
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta

        if delta.content:
            if not printed_header:
                print(f"\n{BOLD}executor >{RESET} ", end="", flush=True)
                printed_header = True
            print(delta.content, end="", flush=True)
            content_parts.append(delta.content)

        if delta.tool_calls:
            for tcd in delta.tool_calls:
                slot = tool_buf.setdefault(
                    tcd.index, {"id": None, "name": "", "arguments": ""})
                if tcd.id:
                    slot["id"] = tcd.id
                if tcd.function and tcd.function.name:
                    slot["name"] += tcd.function.name
                if tcd.function and tcd.function.arguments:
                    slot["arguments"] += tcd.function.arguments

    if printed_header:
        print()
    return "".join(content_parts), [tool_buf[i] for i in sorted(tool_buf)], prompt_tokens


def _msg_to_dict(content: str, tool_calls: list) -> dict:
    d = {"role": "assistant", "content": content or ""}
    if tool_calls:
        d["tool_calls"] = [
            {"id": tc["id"], "type": "function",
             "function": {"name": tc["name"], "arguments": tc["arguments"]}}
            for tc in tool_calls
        ]
    return d


def build_initial_messages(issue: str, plan_text: str, baseline_summary: str) -> list:
    return [
        {"role": "system", "content": EXECUTOR_SYSTEM},
        {"role": "user", "content": (
            f"## Issue\n{issue}\n\n"
            f"## 修复计划（Planner 产出，动手前先核实）\n{plan_text}\n\n"
            f"## 基线测试结果（修改前）\n{baseline_summary}\n\n"
            "开始吧。"
        )},
    ]


def run_executor(toolkit: ToolKit, perms: Permissions, messages: list,
                 trace: Trace) -> tuple[str, int]:
    """跑一轮 executor（就地修改 messages），返回 (最终总结文本, 峰值上下文 tokens)。"""
    peak_tokens = 0

    for _ in range(MAX_TURNS):
        content, tool_calls, prompt_tokens = _stream_once(messages)
        peak_tokens = max(peak_tokens, prompt_tokens)
        messages.append(_msg_to_dict(content, tool_calls))

        if not tool_calls:
            trace.event("executor_done", peak_tokens=peak_tokens)
            return content, peak_tokens

        for tc in tool_calls:
            name, raw_args = tc["name"], tc["arguments"]
            try:
                args = json.loads(raw_args) if raw_args.strip() else {}
            except json.JSONDecodeError as e:
                result = f"错误：参数不是合法 JSON（{e}）"
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
                continue

            needs_confirm = name not in SAFE_TOOLS and not perms.trust_all
            tag = f" {YELLOW}(需确认){RESET}" if needs_confirm else ""
            shown = raw_args if len(raw_args) <= 90 else raw_args[:90] + "…"
            print(f"  {CYAN}🔧 {name}({shown}){RESET}{tag}")

            allowed, reason = perms.check(name, args)
            if not allowed:
                print(f"  {RED}✗ 已拒绝{RESET}")
                result = (f"操作被用户拒绝。用户说：{reason}\n"
                          "不要重试这个操作，换个方式或说明你的困难。")
            else:
                result = toolkit.execute(name, args)

            trace.event("tool", name=name, args=args,
                        result_preview=result[:200], allowed=allowed)
            preview = result.replace("\n", "⏎")[:90]
            print(f"  {DIM}↳ {preview}{'…' if len(result) > 90 else ''}{RESET}")
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})

    print(f"\n{RED}[executor 达到最大轮数 {MAX_TURNS}，强制停止]{RESET}")
    trace.event("executor_max_turns", peak_tokens=peak_tokens)
    return "（达到最大轮数被强制停止，工作可能未完成）", peak_tokens
