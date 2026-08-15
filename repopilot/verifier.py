"""Verifier：让"修好了"成为可测量的事实，而不是模型的一句话。

这是整个 RepoPilot 的灵魂。方法只有一个：

    改之前跑一遍（基线 baseline）→ 改之后再跑一遍 → 比较差值

比较的是事实，不是叙述。

本轮升级（多语言化）动了三处，每处都是被同一个问题逼出来的：
**旧版的解析器只认识 pytest，但它对别的语言并不承认自己看不懂。**

  1) 解析下沉给 adapter
     旧版在这里用正则抠 `(\\d+) passed`。对 npm/go/cargo 抠不到 → 全 0 →
     "0 failed" 和 "我没看懂" 长得一模一样。现在解析归各 adapter，
     并且带一个 parsed 标志说明数字可不可信。

  2) 修掉一个真 bug：绿 → 红被判成 no_change
     旧版判据里，"没有新增的失败用例名单"是 regressed 的唯一入口。可是
     失败名单本来就只有 pytest 解析得出来 —— 对 Node/Go 项目，基线全绿、
     改完全红，new_failures 是空的、base.exit_code==0 不满足 fixed、
     after.failed(0) < base.failed(0) 不成立 → 落到 no_change。
     **把测试搞挂了，报告说"这轮白干"。** 现在 exit_code 绿→红一律判 regressed。

  3) 新增 run_validation：测试过了 ≠ 没问题
     TS 项目最典型的假通过是"单测绿、tsc 红"。验证流水线按 adapter 给的顺序
     跑 lint → typecheck → test → build，便宜的先跑，早失败早省钱。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .adapters.base import TestReport, ValidationStep, tail
from .config import CMD_TIMEOUT, TEST_TIMEOUT

__all__ = ["StepResult", "TestReport", "ValidationReport", "compare",
           "run_tests", "run_tests_in", "run_validation"]


def run_tests(ws, profile) -> TestReport:
    """跑 profile 给定的测试命令，用 profile 的 adapter 解析输出。"""
    return run_tests_in(ws, profile, profile.test_cmd, cwd=None)


def scoped_command(command: str, root, scope: str) -> str:
    """把测试命令的范围换成 scope（一个文件或目录）。

    【为什么需要它】评测里的测试命令长这样：

        …/python -m pytest -q -ra --color=no tests

    末尾那个 `tests` 是整个测试目录，跑一遍要 65~480 秒。agent 改完一处只想
    验证一个文件，于是它绕过 run_tests、自己用 run_bash 拼命令 —— 实测
    147 次 vs 13 次。绕过去的代价是：结构化失败证据、测试次数预算、
    失败指纹（停机策略靠它判"又是同一个错"）**三样一起失效**。

    做法是把命令末尾那些【确实存在的路径】摘掉，换成 scope。为什么按
    "存在性"判断而不是按位置：命令里还有 `-q`、`--color=no` 这类参数，
    它们不是路径；而路径参数一定能在仓库里找到对应的文件或目录。

    【这不是重新引入泄露】泄露的定义是【我们】告诉 agent 隐藏测试在哪。
    agent 根据自己的探索决定跑哪个文件，是它自己的推理 —— 和它 ls 一下
    目录性质相同。评测线束仍然只给出目录级的默认范围，见
    eval/harness/environment.py。
    """
    tokens = command.split()
    while len(tokens) > 1 and not tokens[-1].startswith("-"):
        candidate = (root / tokens[-1])
        if candidate.exists():
            tokens.pop()
        else:
            break
    return " ".join([*tokens, scope])


def run_tests_in(ws, profile, command: str | None, cwd: str | None = None,
                 adapter=None) -> TestReport:
    """在仓库的某个子目录里跑指定测试命令 —— monorepo 的"改哪块跑哪块"靠它。

    cwd=None 表示仓库根；cwd="web" 表示只跑 web 这个项目单元的测试。

    adapter 用来指定【由谁解析输出】。这个参数不是可选的锦上添花：monorepo 里
    根目录可能是个 turbo/workspace 管理者（框架 generic，什么都解析不出来），
    而 apps/web 用的是 vitest。拿根的解析器去读 vitest 的输出，结果是
    "未解析成功" —— 明明有一份能读懂它的 adapter 就在手边。
    """
    if not command:
        return TestReport(exit_code=-1, tail="没有可用的测试命令", parsed=False)

    workdir = ws.root if cwd in (None, ".") else ws.root / cwd
    r = ws.exec.run(command, cwd=workdir, timeout=TEST_TIMEOUT)
    if r.timed_out:
        # 超时也要把已有输出交出去：超时前的输出往往正是卡住的原因
        return TestReport(exit_code=-1, errors=1, parsed=False,
                          tail=f"测试超时（>{TEST_TIMEOUT}s）\n{tail(r.output)}")
    if r.code == -1:
        return TestReport(exit_code=-1, errors=1, parsed=False,
                          tail=f"测试{r.output}")

    return (adapter or profile).parse_test_output(r.output, r.code)


def compare(base: TestReport, after: TestReport) -> dict:
    """基线对比 —— 每个状态都是一个明确的事实判断：

      fixed         基线红、现在绿：目标达成
      still_green   基线绿、现在也绿：没破坏任何东西（改进类任务的通过态）
      improved      基线红的一部分转绿、没有新增失败：也是通过态 ——
                    它只可能在红基线下出现（绿基线下任何失败都是 regressed），
                    剩下的红是 no_regression 说的那批环境预置失败的子集
      no_regression 基线就是红的、失败集合没变大：与 still_green 同义 ——
                    那些红是【环境预置】的失败，不是这次改动造成的
      regressed     出现基线里没有的新失败，**或者绿变红**：改出回归了
      no_change     红的还是红：这轮白干

    【no_regression 为什么必须存在】官方 SWE-bench 镜像的环境里,基线本来就
    可能带着失败（实测 flask-5014 的容器里 8 个测试因 DNS 解析失败天生是红
    的）。官方判分不受影响 —— 它只看 P2P 白名单,同一批红测试 gold 也过不了,
    两边公平。但 agent 的循环里"红基线"让 fixed/still_green 都不可达,一份
    正确的补丁会被 NO_PROGRESS 误杀然后回滚。语义对齐是:still_green 从来
    只证明"没破坏任何东西",从不证明"修好了议题"（那是 Reviewer 和判分的
    事）—— no_regression 说的是同一句话,只是基线的底色不同。

    结果里额外带一个 confidence：两份报告都解析成功才是 high。
    low 的意思是"我只能靠 exit_code 判断，别把 improved/no_change 当真"。
    """
    new_failures = sorted(set(after.failed_names) - set(base.failed_names))
    resolved = sorted(set(base.failed_names) - set(after.failed_names))
    trusted = base.parsed and after.parsed

    if new_failures:
        status = "regressed"
    elif base.green and not after.green:
        # 见文件头第 2 条：这一支旧版是缺的，绿变红会被误判成 no_change
        status = "regressed"
    elif not base.green and after.green:
        status = "fixed"
    elif base.green and after.green:
        status = "still_green"
    elif trusted and (after.failed + after.errors) < (base.failed + base.errors):
        status = "improved"
    elif trusted and (after.failed + after.errors) <= (base.failed + base.errors):
        # 走到这里必然 base 红、after 红、没有新增失败名单、数量没涨
        status = "no_regression"
    else:
        status = "no_change"

    return {
        "status": status,
        "new_failures": new_failures,
        "resolved_failures": resolved,
        "confidence": "high" if trusted else "low",
        "baseline": base.summary(),
        "after": after.summary(),
    }


# ---------------------------------------------------------------------------
# 验证流水线：lint / typecheck / test / build
# ---------------------------------------------------------------------------
@dataclass
class StepResult:
    name: str
    command: str
    exit_code: int
    output: str = ""
    skipped: bool = False
    skip_reason: str = ""

    @property
    def ok(self) -> bool:
        return self.skipped or self.exit_code == 0

    def render(self) -> str:
        if self.skipped:
            return f"  ○ {self.name:<10} 跳过（{self.skip_reason}）"
        mark = "✓" if self.exit_code == 0 else "✗"
        return f"  {mark} {self.name:<10} exit={self.exit_code}  ({self.command})"


@dataclass
class ValidationReport:
    steps: list[StepResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(s.ok for s in self.steps)

    @property
    def failed_step(self) -> StepResult | None:
        return next((s for s in self.steps if not s.ok), None)

    def render(self) -> str:
        if not self.steps:
            return "（这个项目没有可执行的验证步骤）"
        lines = [s.render() for s in self.steps]
        bad = self.failed_step
        if bad is not None:
            lines += [f"\n--- {bad.name} 失败输出末尾 ---", tail(bad.output, 1500)]
        return "\n".join(lines)


# 缺工具的典型信号。区分"工具没装"和"检查真的没通过"很重要：
# 前者不该让 agent 去改代码（它改不好一个没装的 linter）。
_MISSING_TOOL = ("command not found", "not found", "No module named",
                 "is not recognized", "cannot be loaded", "Cannot find module",
                 "Unknown command", "executable not found")


def run_validation(ws, profile, only: list[str] | None = None,
                   cwd: str | None = None) -> ValidationReport:
    """按 adapter 给定的顺序跑验证流水线，遇到 required 步骤失败就停。

    为什么失败就停（fail fast）：后面的步骤大概率也过不了，接着跑纯烧时间。
    典型顺序是 lint(秒) → typecheck(十几秒) → test(分钟) → build(几分钟)，
    早失败省下的正是最贵的那几步。
    """
    steps = profile.validation_steps()
    if only:
        steps = [s for s in steps if s.name in only]

    workdir = ws.root if cwd in (None, ".") else ws.root / cwd
    report = ValidationReport()

    for step in steps:
        result = _run_step(ws.exec, step, workdir)
        report.steps.append(result)
        if not result.ok:
            break
    return report


def _run_step(execu, step: ValidationStep, workdir) -> StepResult:
    limit = TEST_TIMEOUT if step.name == "test" else CMD_TIMEOUT
    r = execu.run(step.command, cwd=workdir, timeout=limit)
    if r.timed_out:
        return StepResult(step.name, step.command, -1, f"超时（>{limit}s）")
    if r.code == -1:
        return StepResult(step.name, step.command, -1, r.output)

    missing = r.code == 127 or any(sig in r.output for sig in _MISSING_TOOL)
    if not step.required and r.code != 0 and missing:
        return StepResult(step.name, step.command, r.code, r.output,
                          skipped=True, skip_reason="工具未安装")
    return StepResult(step.name, step.command, r.code, r.output)
