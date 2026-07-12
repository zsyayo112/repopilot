"""结构化输出：让模型交回的是【数据】而不是【散文】。

这就是文档里那句 "LLM Structured Output" 的全部真相 —— 不需要框架，
就三件事：response_format 强制 JSON + 解析 + 解析失败把报错喂回去重试。
Planner 和 Reviewer 都靠这一个函数。

（executor 的流式调用不在这里，见 executor.py —— 那边要边收边打印，
形态完全不同，硬揉进一个抽象反而谁都看不懂。）
"""

import json
import re

from .config import MODEL, client


def json_call(system: str, user: str, retries: int = 2) -> dict:
    """一次性调用 + 强制 JSON。解析失败会把错误信息喂回模型重试。

    注意：DeepSeek 的 json_object 模式要求提示词里出现 "JSON" 字样，
    所以 system prompt 必须写明"只输出 JSON"。
    """
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    last_err = ""
    for _ in range(retries + 1):
        try:
            resp = client.chat.completions.create(
                model=MODEL, messages=messages,
                response_format={"type": "json_object"},
            )
        except Exception:
            # 个别网关不支持 response_format，退化成普通调用再靠解析兜底
            resp = client.chat.completions.create(model=MODEL, messages=messages)

        text = resp.choices[0].message.content or ""
        try:
            return _parse(text)
        except ValueError as e:
            last_err = str(e)
            messages.append({"role": "assistant", "content": text})
            messages.append({
                "role": "user",
                "content": f"你的输出不是合法 JSON（{e}）。只输出修正后的 JSON 对象，不要任何其他文字。",
            })
    raise RuntimeError(f"重试后仍未得到合法 JSON：{last_err}")


def _parse(text: str) -> dict:
    """容错解析：模型有时会包一层 ```json 围栏，剥掉再解析。"""
    m = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if m:
        text = m.group(1)
    try:
        obj = json.loads(text.strip())
    except json.JSONDecodeError as e:
        raise ValueError(str(e)) from None
    if not isinstance(obj, dict):
        raise ValueError("顶层必须是 JSON 对象")
    return obj
