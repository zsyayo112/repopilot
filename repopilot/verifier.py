"""Verifier：让"修好了"成为可测量的事实，而不是模型的一句话。

这是整个 RepoPilot 的灵魂。playground 时代，模型说"改好了"你就信了；
真实仓库里，"看起来对"和"测试变绿"是两回事。方法只有一个：

    改之前跑一遍测试（基线 baseline）→ 改之后再跑一遍 → 比较差值

比较的是事实，不是叙述。四种结论都有明确定义，见 compare()。
"""

import re
import subprocess
from dataclasses import dataclass, field

from .config import CMD_TIMEOUT


@dataclass
class TestReport:
    exit_code: int
    passed: int
    failed: int
    errors: int
    failed_names: list[str] = field(default_factory=list)
    tail: str = ""

    def summary(self) -> str:
        s = f"exit={self.exit_code} | {self.passed} passed, {self.failed} failed, {self.errors} errors"
        if self.failed_names:
            s += "\n失败用例：\n" + "\n".join(f"  {n}" for n in self.failed_names[:10])
        return s

    def render(self) -> str:
        """给 executor（模型）看的版本：摘要 + 输出尾巴（报错细节在尾巴里）。"""
        return f"{self.summary()}\n\n--- 输出末尾 ---\n{self.tail}"


def run_tests(ws, profile) -> TestReport:
    """跑测试并解析结果。解析基于 pytest 的 -q -ra 输出格式。

    对 npm test 等其他命令：数字解析可能失败（全 0），但 exit_code 永远可靠 ——
    所以 compare() 的主判据是 exit_code，数字只是辅助信息。
    """
    try:
        proc = subprocess.run(
            profile.test_cmd, shell=True, cwd=ws.root,
            capture_output=True, text=True, timeout=CMD_TIMEOUT * 2,
        )
        out = proc.stdout + proc.stderr
        code = proc.returncode
    except subprocess.TimeoutExpired:
        return TestReport(-1, 0, 0, 1, [], f"测试超时（>{CMD_TIMEOUT * 2}s）")

    def count(word: str) -> int:
        m = re.search(rf"(\d+) {word}", out)
        return int(m.group(1)) if m else 0

    failed_names = [line.split()[1] for line in out.splitlines()
                    if line.startswith("FAILED ") and len(line.split()) > 1]

    return TestReport(code, count("passed"), count("failed"),
                      count("error"), failed_names, out[-2500:].strip())


def compare(base: TestReport, after: TestReport) -> dict:
    """基线对比 —— 每个状态都是一个明确的事实判断：

      fixed       基线红、现在绿：目标达成
      still_green 基线绿、现在也绿：没破坏任何东西（改进类任务的通过态）
      improved    失败数变少但还有红：方向对，继续修
      regressed   出现了基线里没有的新失败：改出回归了！
      no_change   红的还是红：这轮白干
    """
    new_failures = sorted(set(after.failed_names) - set(base.failed_names))

    if new_failures:
        status = "regressed"
    elif base.exit_code != 0 and after.exit_code == 0:
        status = "fixed"
    elif base.exit_code == 0 and after.exit_code == 0:
        status = "still_green"
    elif after.failed < base.failed:
        status = "improved"
    else:
        status = "no_change"

    return {
        "status": status,
        "new_failures": new_failures,
        "baseline": base.summary(),
        "after": after.summary(),
    }
