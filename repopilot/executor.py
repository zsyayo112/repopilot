"""Executor：真正动手干活的主循环。骨架与 agent/loop.py 完全同源 ——

    问模型 → 有 tool_calls 就执行、喂回 → 再问，直到它不再要工具。

流式拼装（_stream_once）原样继承。不同点：
  - 工具来自 ToolKit 实例（绑定具体仓库），不再是模块级函数
  - 每个工具调用都写进 trace（黑匣子）
  - 支持"续跑"：验证失败后，orchestrator 把失败报告作为新 user 消息
    追加进同一份 messages 再来一轮 —— 上下文连续，模型记得自己上轮改了什么
"""

import json

from .compaction import compact, should_compact
from .config import BOLD, CYAN, DIM, MAX_TURNS, MODEL, RED, RESET, YELLOW
from .llm import create_with_retry
from .permissions import Permissions
from .tools import ToolKit, is_safe
from .trace import Trace

EXECUTOR_SYSTEM = """你是一个在真实 git 仓库里解决 issue 的编程 agent。当前工作目录就是仓库根目录。

工作方法：
- 给你的计划只是假设：动手改之前，必须先用 search_code / read_file 核实它是否成立。
- 大文件先 list_symbols 看骨架，再用 read_file 的行号范围分段读，不要整读。
- 要翻很多文件才能定位时，用 explore 派子 agent 去查（它读的文件不占你的上下文）。
- 修改一律用 edit_file（精确片段替换）；只有创建全新文件才用 write_file。
- 每次实质修改后立刻 run_tests 验证；失败就读输出、分析、修正、再跑。
- 跑测试只用 run_tests 工具（run_bash 里的测试命令会被策略拒绝）。
  迭代时【一定要带 scope】把范围缩到相关的测试文件，例如
  run_tests(scope="tests/test_config.py") —— 全量套件可能要好几分钟，
  而且会把大量无关输出灌进你的上下文。收尾前再不带 scope 跑一次确认没改坏别处。
- 测试莫名失败（比如报模块找不到）时，先 check_environment —— 环境问题你改代码修不好。
- 严禁改动与 issue 无关的文件；严禁 cd；所有路径相对仓库根目录。
- 收尾前跑一次 run_validation：测试绿不代表 lint/类型检查/构建也绿。
- 最后用 git_diff 自查改动是否最小、有无误伤，然后停止调用工具，
  用一段话总结：改了什么、为什么、测试和验证结果如何。"""

# 只有开了 runtime/browser 能力时才把这段拼进 system prompt。
# 不开的时候连提都不提 —— 免得模型去想它根本没有的工具。
RUNTIME_SYSTEM = """
这个任务开启了【运行时验证】能力，用来处理"必须页面真的渲染出来才能发现"的问题
（按钮点不动、跳转错误、接口成功但页面没更新、移动端错位、元素被遮挡）。

运行时工作方法：
- 先 start_services 把应用起起来（它会等到健康检查真正通过才返回）。
- 有 E2E 套件就先 run_e2e，比自己开浏览器点省事得多。
- 用 browser_open / browser_snapshot 观察页面，用快照里的 ref 去 click / fill。
  不要写 CSS 选择器 —— ref 和 role+name 才稳。
- 页面行为不对时先看 browser_errors 和 read_service_logs，别靠猜。
- 判断功能是否修好，用 URL / DOM / 网络 / 控制台断言，不要靠截图；
  截图只用来看视觉问题（错位、遮挡、响应式）。
- 复现步骤请固化成 run_scenario 的 JSON：修改前跑一次（应该失败），
  改完跑【同一份】再跑一次（应该通过）。这是"真修好了"的唯一客观证据。"""


def system_prompt(groups: tuple[str, ...]) -> str:
    if "runtime" in groups or "browser" in groups:
        return EXECUTOR_SYSTEM + "\n" + RUNTIME_SYSTEM
    return EXECUTOR_SYSTEM


def _stream_once(messages: list, tools: list, budget=None,
                 ledger=None) -> tuple[str, list, int]:
    """一次流式请求：边收边打印文本，拼装 tool_calls 碎片。原样继承 agent/loop.py。

    tools 现在由调用方传入（来自 toolkit.specs()）而不是写死的全局 TOOLS ——
    因为工具按能力分组暴露，不同任务拿到的工具列表不一样。

    budget / ledger 可选：不传就是老行为（全局 MODEL、不记账、不限输出长度）。
    """
    # create_with_retry：503 过载/限流/超时自动退避重试（见 llm.py 顶部说明）。
    # 只保护"发起请求"这一步；流已经开始后中断的情况极少，MVP 不做续流。
    extra = {}
    if budget is not None:
        extra = {"temperature": budget.temperature,
                 "max_tokens": budget.max_output_tokens}
    stream = create_with_retry(
        model=budget.model if budget else MODEL, messages=messages, tools=tools,
        stream=True, stream_options={"include_usage": True}, **extra,
    )

    content_parts: list[str] = []
    tool_buf: dict[int, dict] = {}
    prompt_tokens = 0
    reasoning_chars = 0
    printed_header = False

    for chunk in stream:
        if chunk.usage:
            prompt_tokens = chunk.usage.prompt_tokens
            if ledger is not None:
                ledger.record_usage(chunk.usage, role="executor")
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta

        # 推理模型（deepseek-reasoner 等）的思考流走 reasoning_content 字段。
        # 不收集进上下文（那是它的草稿），但要计量并让人看见 —— R1 实测：
        # 思考把 max_tokens 烧穿后 content 为空，外面看就是"一声不吭地结束"。
        rc = getattr(delta, "reasoning_content", None)
        if rc:
            reasoning_chars += len(rc)

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
    if reasoning_chars:
        print(f"{DIM}[思考 {reasoning_chars:,} 字符]{RESET}")
    return "".join(content_parts), [tool_buf[i] for i in sorted(tool_buf)], prompt_tokens


SPIN_MIN_PARA = 60      # 只统计有实质内容的段落 —— "OK."这类短句天然会重复
SPIN_REPEATS = 3        # 同一段落第三次出现即判打转
SPIN_MAX_STRIKES = 2    # 干预两次仍打转就放弃这一轮，别陪它烧完预算

EMPTY_FEEDBACK = (
    "你上一条回复是空的 —— 很可能是思考长度超出了输出上限，结论没能写出来。"
    "不要重新展开长篇分析：基于你已经想清楚的部分，直接给出下一步的【工具调用】"
    "（读代码 / 改代码 / 跑测试），把结论落成动作。")

SPIN_FEEDBACK = (
    "检测到你在逐字重复同一段分析 —— 你在原地打转，继续想只会再重复一遍。"
    "立刻停止输出分析文字，动手做下面两件事之一：\n"
    "1) 把 issue 写成一个能运行的复现测试（edit_file 加进对应测试文件），"
    "然后 run_tests 看它挂在哪里 —— 让事实替你区分假设；\n"
    "2) 如果几个假设无法靠读代码区分，选可观测量最强的那个直接改代码验证，错了再撤。")


def detect_spin(content: str) -> str | None:
    """检测退化的自我重复输出。打转返回截断后的文本，正常返回 None。

    T=0 下模型有一种典型病理：同一段分析一字不差地重复几十遍（实测
    testify#1785 一次 completion 里重复了几十遍，8k 输出额度全烧在原地）。
    停机策略的规则对它全部免疫 —— 不调工具、不改文件、失败指纹不变，
    它烧的只是 token。所以断路器必须装在 executor 这一层，判据同样只看
    可观测量：白空格归一后的同一段落第 SPIN_REPEATS 次出现即打转。
    截断到复发点为止：保留"它开始绕圈"的证据，丢掉后面的空转 ——
    喂回上下文的重复文本本身就是下一轮继续重复的诱因。
    """
    seen: dict[str, int] = {}
    kept: list[str] = []
    for para in content.split("\n\n"):
        key = " ".join(para.split())
        if len(key) >= SPIN_MIN_PARA:
            seen[key] = seen.get(key, 0) + 1
            if seen[key] >= SPIN_REPEATS:
                return "\n\n".join(kept)
        kept.append(para)
    return None


def _msg_to_dict(content: str, tool_calls: list) -> dict:
    d = {"role": "assistant", "content": content or ""}
    if tool_calls:
        d["tool_calls"] = [
            {"id": tc["id"], "type": "function",
             "function": {"name": tc["name"], "arguments": tc["arguments"]}}
            for tc in tool_calls
        ]
    return d


def build_initial_messages(issue: str, plan_text: str, baseline_summary: str,
                           groups: tuple[str, ...] = ("core",),
                           extra_context: str = "") -> list:
    return [
        {"role": "system", "content": system_prompt(groups)},
        {"role": "user", "content": (
            f"## Issue\n{issue}\n\n"
            f"## 修复计划（Planner 产出，动手前先核实）\n{plan_text}\n\n"
            f"## 基线测试结果（修改前）\n{baseline_summary}\n"
            + (f"\n{extra_context}\n" if extra_context else "")
            + "\n开始吧。"
        )},
    ]


def run_executor(toolkit: ToolKit, perms: Permissions, messages: list,
                 trace: Trace, budget=None, ledger=None,
                 narrowed: bool = False) -> tuple[str, int]:
    """跑一轮 executor（就地修改 messages），返回 (最终总结文本, 峰值上下文 tokens)。

    narrowed=True 时收掉 explore 工具：token 压力到八成之后再派探索子 agent，
    是拿收尾的额度去赌一次定位。剩下的额度应该留给"把已经知道的东西改完"。
    """
    peak_tokens = 0
    spin_strikes = empty_strikes = 0
    tools = toolkit.specs()
    if narrowed:
        tools = [t for t in tools if t["function"]["name"] != "explore"]
    max_turns = budget.max_turns if budget else MAX_TURNS

    for _ in range(max_turns):
        # 硬约束：每一圈开头先查账。这是"预算是对象不是提示词"的兑现之处 ——
        # 超支不靠模型自觉，靠这里 return。
        if ledger is not None and (over := ledger.exhausted()):
            print(f"\n{RED}[预算耗尽：{over}，executor 停止]{RESET}")
            trace.event("executor_budget_exhausted", code=over,
                        peak_tokens=peak_tokens, **ledger.to_dict())
            return f"（预算耗尽 {over}，本轮提前停止）", peak_tokens

        content, tool_calls, prompt_tokens = _stream_once(messages, tools, budget, ledger)
        peak_tokens = max(peak_tokens, prompt_tokens)

        truncated = detect_spin(content)
        if truncated is not None:
            content = truncated + "\n\n[输出在此被截断：同一段落已第三次逐字重复]"
        messages.append(_msg_to_dict(content, tool_calls))

        # 上下文压缩。放在【拿到本轮 prompt_tokens 之后】而不是发请求之前，
        # 因为触发判断需要一个真实测得的数字 —— 自己估 token 会估错，
        # 而估错的方向通常是低估（工具返回值里全是代码，token 密度高）。
        if should_compact(prompt_tokens):
            evicted, saved = compact(messages)
            if evicted:
                print(f"{DIM}[压缩] 上下文 {prompt_tokens} tokens 触发压缩："
                      f"折叠了 {evicted} 条陈旧工具返回，省下 {saved:,} 字符{RESET}")
                trace.event("compaction", prompt_tokens=prompt_tokens,
                            evicted=evicted, saved_chars=saved)

        if not tool_calls:
            # 空回复 + 不调工具：不是"没什么可说的"，是回复被截没了。
            # R1 实测（cobra#1777）：思考流烧穿 max_tokens，content 一个字都没有，
            # 旧逻辑把它当成最终总结收工 —— 两轮都"安静地"零交付结束。
            if not content.strip():
                empty_strikes += 1
                trace.event("executor_empty_completion", strikes=empty_strikes,
                            peak_tokens=peak_tokens)
                print(f"\n{YELLOW}[空回复] 疑似思考超出输出上限"
                      f"（第 {empty_strikes}/2 次干预）{RESET}")
                if empty_strikes >= 2:
                    return ("（连续两次空回复，疑似输出上限过小 —— "
                            "考虑用 --max-output-tokens 加大后重跑）"), peak_tokens
                messages.append({"role": "user", "content": EMPTY_FEEDBACK})
                continue
            # 打转 + 不调工具：这不是"总结完收工"，是空转到没话可说。
            # 按正常路径 return 会把这段废话当成最终总结交给 VERIFY ——
            # testify#1785 就是这么以零改动"完成"的。喂回纠偏指令再给一次
            # 机会；连续 SPIN_MAX_STRIKES 次仍打转就放弃，如实说明。
            if truncated is not None:
                spin_strikes += 1
                trace.event("executor_spin", strikes=spin_strikes,
                            peak_tokens=peak_tokens)
                print(f"\n{YELLOW}[断路器] 检测到重复输出"
                      f"（第 {spin_strikes}/{SPIN_MAX_STRIKES} 次干预）{RESET}")
                if spin_strikes >= SPIN_MAX_STRIKES:
                    return ("（检测到持续的重复输出，两次干预无效，"
                            "executor 停止 —— 模型无法在此问题上取得进展）"), peak_tokens
                messages.append({"role": "user", "content": SPIN_FEEDBACK})
                continue
            trace.event("executor_done", peak_tokens=peak_tokens)
            return content, peak_tokens

        for tc in tool_calls:
            if ledger is not None:
                ledger.note_tool_call()
            name, raw_args = tc["name"], tc["arguments"]
            try:
                args = json.loads(raw_args) if raw_args.strip() else {}
            except json.JSONDecodeError as e:
                result = f"错误：参数不是合法 JSON（{e}）"
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
                continue

            needs_confirm = not is_safe(name, args) and not perms.trust_all
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

    print(f"\n{RED}[executor 达到最大轮数 {max_turns}，强制停止]{RESET}")
    trace.event("executor_max_turns", peak_tokens=peak_tokens)
    return "（达到最大轮数被强制停止，工作可能未完成）", peak_tokens
