"""结构化输出 + 模型调用的可靠性层。

结构化输出就三件事，不需要框架：response_format 强制 JSON + 解析 +
解析失败把报错喂回去重试。Planner 和 Reviewer 都靠 json_call 这一个函数。

这里还有一层容易被忽视的可靠性：【瞬态 API 错误】。503 过载、429 限流、
超时——这些和"模型答错了"完全是两类问题：
  - 模型答错（JSON 不合法）→ 把错误喂回去让它改（内容问题，喂回有用）
  - 服务器过载（503）     → 等一等再原样重发（基础设施问题，喂回没用，
                            立刻重发只会火上浇油）
所以瞬态错误的唯一正确姿势是【指数退避重试】——create_with_retry。
executor 的流式调用也从这里拿同一份重试逻辑（2026-07-14 实测：DeepSeek
高峰期 503，没有退避的版本让整批评测瞬间全崩）。
"""

import json
import re
import time

from openai import (
    APIConnectionError,
    APITimeoutError,
    BadRequestError,
    InternalServerError,
    RateLimitError,
)

from .config import MODEL, get_client

# 这些是"等一等就好"的瞬态错误；BadRequestError(400) 不在其列——那是请求本身有问题
_TRANSIENT = (InternalServerError, RateLimitError, APIConnectionError, APITimeoutError)
_BACKOFF = [10, 30, 60, 120]   # 秒；总计最多等 ~3.5 分钟


def create_with_retry(**kwargs):
    """chat.completions.create 的可靠版：瞬态错误指数退避，最多重试 4 次。"""
    for attempt, delay in enumerate([*_BACKOFF, None]):
        try:
            return get_client().chat.completions.create(**kwargs)
        except _TRANSIENT as e:
            if delay is None:
                raise
            print(f"[API 瞬态错误 {type(e).__name__}，{delay}s 后重试 "
                  f"({attempt + 1}/{len(_BACKOFF)})]", flush=True)
            time.sleep(delay)


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
            resp = create_with_retry(
                model=MODEL, messages=messages,
                response_format={"type": "json_object"},
            )
        except BadRequestError:
            # 只有 400（网关不支持 response_format）才走这条降级路；
            # 503/429 已在 create_with_retry 里退避处理，不再被一把抓吞掉
            resp = create_with_retry(model=MODEL, messages=messages)

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
