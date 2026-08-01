"""Adapter 契约层：核心与"具体技术栈"之间唯一的一道翻译。

一句话记住这个文件在干什么：

    核心流程只用【统一的名词】说话 —— 测试报告、验证步骤、要起的服务、
    符号清单、环境依赖；每种技术栈负责把自己的方言翻译成这几个名词。

所以核心永远不需要写 `if kind == "node"`。支持一门新语言 =
在 adapters/ 里加一个类 + 登记进 registry.py，**核心一行不改**。

本文件只放两样东西：
  1) 名词（数据类）：TestReport / ValidationStep / ServiceSpec / EnvRequirement / Symbol
  2) 契约（RepoAdapter 抽象基类）：每种技术栈必须回答的那几个问题

任何一种语言的具体知识都不在这里，在同目录的 *_stack.py 里。

【为什么 TestReport 里有个 parsed 字段】
旧版用正则从 pytest 输出里抠数字，抠不到就返回 0。问题是"真的 0 个失败"和
"我没看懂这个输出"在数据上长得一模一样 —— 于是对 npm/go/cargo，verifier 会
拿着一堆假的 0 去比较，还以为自己看得懂。parsed=False 是让 verifier 知道
"这次我是瞎的，只能信 exit_code"。**不确定要显式，不能伪装成确定。**
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

TAIL_CHARS = 2500          # 回填给模型的输出尾巴长度：报错细节通常都在末尾


def tail(text: str, limit: int = TAIL_CHARS) -> str:
    return text[-limit:].strip()


def grab_int(pattern: str, text: str) -> int | None:
    """抠一个整数，抠不到返回 None（而不是 0）—— 见文件头关于 parsed 的说明。"""
    m = re.search(pattern, text)
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# 名词一之半：一条结构化的失败证据。
#
# 【为什么不能只把 pytest 输出整段丢给模型】
# 一段 2500 字符的日志里，真正有用的是四个字段：哪个用例、什么异常、在哪一行、
# 说了什么。剩下的是横线、collect 统计、无关 warning。整段丢进去有三个代价：
#   1) 烧上下文（多轮之后它会被重发几十次）
#   2) 淹没信号（模型在噪声里挑错重点，去修一个 DeprecationWarning）
#   3) 无法比较（"这轮的失败和上轮一样吗"没法用字符串相等判断，
#      因为耗时、临时路径每次都变 —— 见 signature()）
# 结构化之后这三个问题一起消失。
# ---------------------------------------------------------------------------
@dataclass
class TestFailure:
    test: str                  # tests/test_config.py::test_x
    exception: str = ""        # TypeError
    location: str = ""         # src/config.py:48
    message: str = ""          # 异常首行

    def render(self) -> str:
        head = self.test
        if self.exception:
            head += f"  [{self.exception}]"
        if self.location:
            head += f"  @ {self.location}"
        return head + (f"\n      {self.message[:200]}" if self.message else "")


# ---------------------------------------------------------------------------
# 名词一：测试报告。所有技术栈的测试输出，最终都必须变成这一个形状。
# ---------------------------------------------------------------------------
@dataclass
class TestReport:
    __test__ = False           # 类名以 Test 开头，明确告诉 pytest 别把它当测试类收集

    exit_code: int
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    failed_names: list[str] = field(default_factory=list)
    failures: list[TestFailure] = field(default_factory=list)  # 结构化版，有则优先用
    tail: str = ""
    parsed: bool = False       # 数字是不是真的解析出来了（False = 只有 exit_code 可信）
    framework: str = "generic"  # pytest / jest / vitest / go / cargo / junit / rspec / generic

    @property
    def green(self) -> bool:
        return self.exit_code == 0

    def signature(self) -> str:
        """这次失败"长什么样"的稳定指纹 —— 停机策略判断"又是同一个错"就靠它。

        故意【只取用例 id + 异常类型】，不含耗时、临时目录、内存地址：
        那些每次都变，用整段输出做相等判断永远不相等，"连续两次一模一样"
        这条规则就永远不会触发。
        """
        if self.failures:
            items = sorted(f"{f.test}|{f.exception}" for f in self.failures)
        elif self.failed_names:
            items = sorted(self.failed_names)
        else:
            items = [f"exit={self.exit_code}", f"failed={self.failed}", f"errors={self.errors}"]
        return hashlib.sha256("\n".join(items).encode()).hexdigest()[:16]

    def evidence(self, limit: int = 6) -> str:
        """给模型看的【结构化】失败证据。解析不出来时才退回原始日志尾巴。

        这是 render() 的省 token 版本：同样的信息量，通常只要 1/5 的字符。
        """
        if not self.failures:
            return self.render()
        lines = [self.summary().splitlines()[0], "", "失败明细："]
        for f in self.failures[:limit]:
            lines.append("  - " + f.render())
        if len(self.failures) > limit:
            lines.append(f"  … 另有 {len(self.failures) - limit} 条同类失败")
        return "\n".join(lines)

    def summary(self) -> str:
        s = f"exit={self.exit_code} | "
        if self.parsed:
            s += f"{self.passed} passed, {self.failed} failed, {self.errors} errors"
            if self.skipped:
                s += f", {self.skipped} skipped"
        else:
            s += "测试数字未解析成功（本次只有 exit_code 可信）"
        s += f"  [{self.framework}]"
        if self.failed_names:
            s += "\n失败用例：\n" + "\n".join(f"  {n}" for n in self.failed_names[:10])
            if len(self.failed_names) > 10:
                s += f"\n  … 另有 {len(self.failed_names) - 10} 条"
        return s

    def render(self) -> str:
        """给模型看的版本：摘要 + 输出尾巴（报错细节在尾巴里）。"""
        return f"{self.summary()}\n\n--- 输出末尾 ---\n{self.tail}"


# ---------------------------------------------------------------------------
# 名词二：验证步骤。"测试过了"不等于"没问题" —— TS 项目常见单测绿、tsc 红。
# 顺序即执行顺序：便宜的先跑（lint 几秒，build 几分钟），早失败早省钱。
# ---------------------------------------------------------------------------
@dataclass
class ValidationStep:
    name: str                  # lint / typecheck / test / build
    command: str
    required: bool = True      # False = 缺工具就跳过，不算失败（例如项目没配 mypy）


# ---------------------------------------------------------------------------
# 名词三：要起的服务。跑得起来的应用，才有"渲染后才能发现的 bug"可言。
# ---------------------------------------------------------------------------
@dataclass
class ServiceSpec:
    name: str                        # web / api / db …
    command: str                     # 启动命令，在 cwd 下执行
    cwd: str = "."                   # 相对仓库根
    port: int | None = None          # 用于健康检查和端口占用检测
    health_path: str = "/"           # HTTP 健康检查路径
    ready_pattern: str | None = None  # 日志里出现这个正则也算就绪（无 HTTP 端口时用）
    env: dict[str, str] = field(default_factory=dict)
    startup_timeout: int = 120       # 就绪等待上限（秒）；框架冷启动很慢
    depends_on: list[str] = field(default_factory=list)  # 启动顺序：db → api → web

    @property
    def base_url(self) -> str | None:
        return f"http://127.0.0.1:{self.port}" if self.port else None

    @property
    def health_url(self) -> str | None:
        base = self.base_url
        return f"{base}{self.health_path}" if base else None


# ---------------------------------------------------------------------------
# 名词四：环境依赖。必须能区分"代码有问题"和"环境没装好" —— 这是两种完全
# 不同的失败，混在一起 agent 会疯狂重试一个它永远修不好的东西。
# ---------------------------------------------------------------------------
@dataclass
class EnvRequirement:
    name: str                  # node / go / mvn …
    probe: list[str] = field(default_factory=list)   # 探测命令，如 ["node", "--version"]
    hint: str = ""             # 缺失时给人看的安装建议
    optional: bool = False
    must_exist: Path | None = None   # 换成"这个路径必须存在"（如 node_modules/）

    def check(self) -> tuple[bool, str]:
        """返回 (是否可用, 版本或错误说明)。永不抛异常。"""
        if self.must_exist is not None:
            ok = Path(self.must_exist).exists()
            return ok, "已就位" if ok else "不存在"

        if not self.probe:
            return True, "无需检查"
        if not shutil.which(self.probe[0]):
            return False, "未安装（PATH 里找不到）"
        try:
            proc = subprocess.run(self.probe, capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError) as e:
            return False, f"探测失败：{type(e).__name__}"
        out = (proc.stdout + proc.stderr).strip().splitlines()
        detail = out[0][:80] if out else ""
        # 【必须看退出码】：命令存在不等于可用。工具装了一半、版本不兼容、
        # license 没接受，都会表现为"能找到二进制但一跑就非零退出"。
        if proc.returncode != 0:
            return False, f"存在但执行失败（exit={proc.returncode}）{(' ' + detail) if detail else ''}"
        return True, detail or "已安装"


# ---------------------------------------------------------------------------
# 名词五：符号。大文件先看骨架再读细节，靠的就是这个。
# ---------------------------------------------------------------------------
@dataclass
class Symbol:
    line: int
    kind: str                  # class / def / interface / type / const …
    name: str
    detail: str = ""           # 签名等附加信息
    col: int = 0

    def render(self) -> str:
        text = f"{self.kind} {self.name}{self.detail}"
        return f"L{self.line:<5}{' ' * self.col}{text}"


# ---------------------------------------------------------------------------
# 契约本体：每种技术栈必须回答的问题
# ---------------------------------------------------------------------------
class RepoAdapter:
    """一种技术栈的全部知识。

    子类【必须】实现：kind / detect / test_command
    子类【可选】覆盖：parse_test_output / validation_steps / services /
                     env_requirements —— 不覆盖就退化成通用兜底（够用但很浅）。

    注意 detect() 是 classmethod 且**零成本**：只看标志文件在不在，
    不读代码、不调模型、不起进程。探测必须便宜到可以对整个 monorepo 扫一遍。
    """

    kind: str = "unknown"
    framework: str = "generic"     # 测试框架名，给 TestReport 打标

    def __init__(self, root: Path, notes: list[str] | None = None):
        self.root = Path(root)
        self.notes = notes or []

    # -- 探测 ---------------------------------------------------------------
    @classmethod
    def detect(cls, root: Path) -> RepoAdapter | None:
        """认领这个目录就返回实例，认不出返回 None。"""
        raise NotImplementedError

    # -- 测试 ---------------------------------------------------------------
    def test_command(self) -> str | None:
        raise NotImplementedError

    def parse_test_output(self, output: str, exit_code: int) -> TestReport:
        """通用兜底：只信 exit_code，并诚实地把 parsed 标成 False。"""
        return TestReport(exit_code=exit_code, tail=tail(output),
                          parsed=False, framework=self.framework)

    # -- 验证流水线（lint / typecheck / build）--------------------------------
    def validation_steps(self) -> list[ValidationStep]:
        cmd = self.test_command()
        return [ValidationStep("test", cmd)] if cmd else []

    # -- 跑起来（大型框架应用）------------------------------------------------
    def services(self) -> list[ServiceSpec]:
        """这个项目要起哪些服务才算"跑起来了"。默认：起不了。"""
        return []

    def e2e_command(self) -> str | None:
        """项目已有的端到端测试命令（Playwright / Cypress）。"""
        return None

    # -- 环境体检 -----------------------------------------------------------
    def env_requirements(self) -> list[EnvRequirement]:
        return []

    # -- 符号提取 -----------------------------------------------------------
    def symbols(self, path: Path) -> list[Symbol] | None:
        """交给 symbols.py 按后缀分派；返回 None 表示这种文件不支持。"""
        from .symbols import extract
        return extract(path)

    # -- 自我介绍 -----------------------------------------------------------
    def describe(self) -> str:
        return f"{self.kind}（测试：{self.test_command() or '未知'}）"


class CustomAdapter(RepoAdapter):
    """`--test-cmd` 一票覆盖时用的 adapter。

    有意思的细节：命令被人覆盖了，但**解析能力不该跟着丢**。所以它会把探测到
    的真实 adapter 抱在怀里（inner），解析、验证、起服务全都转发给它 ——
    人只是换了跑测试的方式，不是换了一门语言。
    """

    kind = "custom"

    def __init__(self, root: Path, command: str, inner: RepoAdapter | None = None):
        super().__init__(root, ["测试命令来自 --test-cmd 参数"])
        self.command = command
        self.inner = inner
        if inner is not None:
            self.framework = inner.framework
            self.notes.append(f"底层探测为 {inner.kind}，解析/服务能力仍然复用它")

    @classmethod
    def detect(cls, root: Path) -> RepoAdapter | None:
        return None            # 只能被显式构造，永不参与自动探测

    def test_command(self) -> str | None:
        return self.command

    def parse_test_output(self, output: str, exit_code: int) -> TestReport:
        if self.inner is not None:
            return self.inner.parse_test_output(output, exit_code)
        return super().parse_test_output(output, exit_code)

    def validation_steps(self) -> list[ValidationStep]:
        # 人指定的测试命令优先；其余步骤（lint/typecheck/build）继续用 inner 的
        steps = [ValidationStep("test", self.command)]
        if self.inner is not None:
            steps += [s for s in self.inner.validation_steps() if s.name != "test"]
        return steps

    def services(self) -> list[ServiceSpec]:
        return self.inner.services() if self.inner else []

    def e2e_command(self) -> str | None:
        return self.inner.e2e_command() if self.inner else None

    def env_requirements(self) -> list[EnvRequirement]:
        return self.inner.env_requirements() if self.inner else []
