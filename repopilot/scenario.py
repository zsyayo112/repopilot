"""Scenario：把"复现步骤"变成一份可以重放的数据。

【它解决的是一个很容易被忽略的作弊空间】
修改前，agent 在浏览器里点了几下，说"我复现了 bug"。修改后，它又点了几下，
说"好了"。问题是：**这两次操作真的一样吗？** 模型完全可能第二次少点了一步、
或者换了个更容易成功的路径，然后诚实地告诉你"通过了"。

所以复现步骤不能留在模型的对话历史里，必须固化成结构化数据：

    修改前：跑 Scenario → 断言失败（证明 issue 存在）
    修改后：跑【同一个】Scenario → 断言通过（证明 issue 被修好）

同一份 steps、同一份 assertions、同一个 viewport。不给"这次操作得随意一点"
留任何空间。这和"护栏写在代码里而不是提示词里"是同一个思路：
**不要求它别投机，而是让投机在结构上不可能。**

【为什么断言要分这么细】
因为"看起来对"和"真的对"是两回事。功能问题必须用 URL / DOM / 网络 / 控制台
断言，不能靠截图 —— 按钮画得再漂亮，点不动就是坏的。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# 允许的动作和断言类型。白名单：不认识的一律报错，不猜。
ACTIONS = ("open", "click", "fill", "select", "reload", "viewport", "screenshot")
ASSERTIONS = ("url", "url_not", "text_present", "text_absent",
              "element_exists", "element_absent", "element_enabled",
              "no_console_errors", "no_failed_requests")


@dataclass
class Step:
    action: str
    # 各动作按需取用，用一个宽松的字典比十个可选字段好读
    params: dict = field(default_factory=dict)

    def render(self) -> str:
        detail = ", ".join(f"{k}={v!r}" for k, v in self.params.items())
        return f"{self.action}({detail})"


@dataclass
class Assertion:
    type: str
    value: str = ""

    def render(self) -> str:
        return f"{self.type}: {self.value}" if self.value else self.type


@dataclass
class Scenario:
    name: str
    steps: list[Step] = field(default_factory=list)
    assertions: list[Assertion] = field(default_factory=list)
    viewport: dict | None = None
    expectation: str = ""      # 人话描述"应该发生什么"，进报告用

    # -- 序列化 -------------------------------------------------------------
    @classmethod
    def from_dict(cls, data: dict) -> Scenario:
        steps = []
        for raw in data.get("steps", []):
            action = raw.get("action", "")
            if action not in ACTIONS:
                raise ValueError(f"不支持的 action：{action!r}，只能是 {ACTIONS}")
            steps.append(Step(action, {k: v for k, v in raw.items() if k != "action"}))

        assertions = []
        for raw in data.get("assertions", []):
            kind = raw.get("type", "")
            if kind not in ASSERTIONS:
                raise ValueError(f"不支持的断言：{kind!r}，只能是 {ASSERTIONS}")
            assertions.append(Assertion(kind, str(raw.get("value", raw.get("matches", "")))))

        if not steps:
            raise ValueError("scenario 至少要有一个 step")
        if not assertions:
            raise ValueError(
                "scenario 至少要有一个断言 —— 没有断言的复现脚本证明不了任何事")
        return cls(data.get("name", "scenario"), steps, assertions,
                   data.get("viewport"), data.get("expectation", ""))

    @classmethod
    def load(cls, path: str | Path) -> Scenario:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "expectation": self.expectation,
            "viewport": self.viewport,
            "steps": [{"action": s.action, **s.params} for s in self.steps],
            "assertions": [{"type": a.type, "value": a.value} for a in self.assertions],
        }

    def render(self) -> str:
        lines = [f"Scenario: {self.name}"]
        if self.expectation:
            lines.append(f"  预期：{self.expectation}")
        if self.viewport:
            lines.append(f"  viewport：{self.viewport['width']}×{self.viewport['height']}")
        lines.append("  步骤：")
        lines += [f"    {i}. {s.render()}" for i, s in enumerate(self.steps, 1)]
        lines.append("  断言：")
        lines += [f"    - {a.render()}" for a in self.assertions]
        return "\n".join(lines)


@dataclass
class AssertionResult:
    assertion: Assertion
    passed: bool
    detail: str = ""

    def render(self) -> str:
        mark = "✓" if self.passed else "✗"
        return f"  {mark} {self.assertion.render()}" + (f" → {self.detail}" if self.detail else "")


@dataclass
class ScenarioResult:
    scenario: Scenario
    step_log: list[str] = field(default_factory=list)
    assertions: list[AssertionResult] = field(default_factory=list)
    error: str = ""

    @property
    def passed(self) -> bool:
        return not self.error and bool(self.assertions) and all(a.passed for a in self.assertions)

    def render(self) -> str:
        head = f"Scenario「{self.scenario.name}」：{'通过' if self.passed else '未通过'}"
        lines = [head, "步骤执行："]
        lines += [f"  {i}. {s}" for i, s in enumerate(self.step_log, 1)]
        if self.error:
            lines.append(f"执行中断：{self.error}")
        lines.append("断言结果：")
        lines += [a.render() for a in self.assertions] or ["  (没有执行到断言)"]
        return "\n".join(lines)


def run(scenario: Scenario, browser) -> ScenarioResult:
    """在给定浏览器会话里跑一遍 scenario。

    步骤出错【不抛异常】—— 记下来，然后照样去跑断言。为什么：
    步骤失败本身常常就是 issue 的表现（"点不动那个按钮"），
    这时候断言结果才是我们要的信息，不该被一个异常打断。
    """
    result = ScenarioResult(scenario)

    viewport = scenario.viewport
    if viewport:
        try:
            browser.set_viewport(int(viewport["width"]), int(viewport["height"]))
            result.step_log.append(f"viewport → {viewport['width']}×{viewport['height']}")
        except Exception as e:
            result.error = f"设置 viewport 失败：{e}"

    for step in scenario.steps:
        try:
            _run_step(step, browser)
            result.step_log.append(f"{step.render()} ✓")
        except Exception as e:
            result.step_log.append(f"{step.render()} ✗ {type(e).__name__}: {e}")
            result.error = f"步骤 {step.render()} 失败：{e}"
            break

    for assertion in scenario.assertions:
        result.assertions.append(_check(assertion, browser))
    return result


def _run_step(step: Step, browser) -> None:
    p = step.params
    if step.action == "open":
        browser.goto(p.get("url", "/"))
    elif step.action == "click":
        browser.click(ref=p.get("ref"), role=p.get("role"), name=p.get("name"))
    elif step.action == "fill":
        browser.fill(p.get("text", ""), ref=p.get("ref"), label=p.get("label"),
                     submit=bool(p.get("submit")))
    elif step.action == "select":
        browser.select(p.get("value", ""), ref=p.get("ref"), label=p.get("label"))
    elif step.action == "reload":
        browser.reload()
    elif step.action == "viewport":
        browser.set_viewport(int(p.get("width", 1440)), int(p.get("height", 900)))
    elif step.action == "screenshot":
        browser.screenshot(p.get("name", step.action))
    else:
        raise ValueError(f"不支持的 action：{step.action}")


def _check(assertion: Assertion, browser) -> AssertionResult:
    """每个断言都基于【客观可观测量】：URL、DOM 文本、元素状态、控制台、网络。"""
    kind, value = assertion.type, assertion.value
    try:
        if kind in ("url", "url_not"):
            url = browser.current_url()
            hit = bool(re.search(value, url))
            want = (kind == "url")
            return AssertionResult(assertion, hit == want, f"当前 url={url}")

        if kind in ("text_present", "text_absent"):
            body = browser.page.inner_text("body")[:20000]
            hit = value in body
            want = (kind == "text_present")
            return AssertionResult(assertion, hit == want,
                                   "找到了" if hit else "没找到")

        if kind in ("element_exists", "element_absent", "element_enabled"):
            locator = browser.page.get_by_text(value, exact=False) \
                if not value.startswith("role=") else _by_role(browser, value)
            count = locator.count()
            if kind == "element_absent":
                return AssertionResult(assertion, count == 0, f"匹配 {count} 个")
            if count == 0:
                return AssertionResult(assertion, False, "一个都没匹配到")
            if kind == "element_exists":
                return AssertionResult(assertion, True, f"匹配 {count} 个")
            enabled = locator.first.is_enabled()
            return AssertionResult(assertion, enabled,
                                   "可用" if enabled else "存在但被禁用")

        if kind == "no_console_errors":
            errs = [c for c in browser.evidence.console if c.level == "error"]
            return AssertionResult(assertion, not errs, f"{len(errs)} 条控制台错误")

        if kind == "no_failed_requests":
            bad = browser.evidence.network
            return AssertionResult(assertion, not bad, f"{len(bad)} 条失败请求")

    except Exception as e:
        return AssertionResult(assertion, False, f"断言执行出错：{type(e).__name__}: {e}")

    return AssertionResult(assertion, False, f"未实现的断言类型：{kind}")


def _by_role(browser, value: str):
    """支持 `role=button:立即预订` 这种写法 —— 语义定位比 CSS 稳。"""
    body = value.removeprefix("role=")
    role, _, name = body.partition(":")
    return browser.page.get_by_role(role.strip(), name=name.strip()) if name \
        else browser.page.get_by_role(role.strip())
