"""Python 技术栈。这是深度最全的一个 adapter，可以当"实现一门新语言"的样板。

它回答了契约里的全部问题：
  探测什么标志文件 / 测试命令 / 怎么解析 pytest 输出 / lint+typecheck 怎么跑 /
  Django·Flask·FastAPI 怎么起服务 / 缺什么环境算装不上
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from .base import (
    EnvRequirement,
    RepoAdapter,
    ServiceSpec,
    TestFailure,
    TestReport,
    ValidationStep,
    grab_int,
    tail,
)

# pytest 参数说明：
#   -q         精简输出（省 token）
#   -ra        失败用例在结尾集中列出（解析失败名单靠它）
#   --color=no 去掉 ANSI 颜色码，方便解析
#
# 用 sys.executable 而不是裸 "python"：子进程继承的 PATH 里未必有 python
# （WSL 常见只有 python3），而 sys.executable 永远指向当前 venv 的解释器。
PYTEST_CMD = f"{sys.executable} -m pytest -q -ra --color=no"

_MARKERS = ("pyproject.toml", "setup.py", "setup.cfg", "pytest.ini", "tox.ini")

# pytest 结尾那行有【两种】长相，取决于有没有 -q：
#     ==== 2 failed, 15 passed, 1 skipped in 3.21s ====     默认
#     462 passed, 15 skipped in 2.06s                       -q（我们自己用的就是它）
#
# 【只认第一种曾经是个真 bug】我们的测试命令是 `pytest -q -ra`，于是每一次
# 【全绿】的运行都解析失败 → parsed=False → confidence 判 low → Reviewer 被
# 告知"没有新增失败这个结论不可靠"→ 驳回一个其实完全正确的补丁。
# 一个正则漏了一种格式，代价是整条判定链的可信度塌掉。
#
# 认第二种时必须要求结尾的 ` in 1.23s`：少了它，正则会命中被测代码自己打印的
# 任何一句 "3 passed"（原版用 `=` 包围来防的就是这个）。
_SUMMARY_LINE = re.compile(
    r"^=+\s*(?P<body>[^=]*?(?:passed|failed|error|skipped|no tests ran)[^=]*?)\s*=+\s*$",
    re.M | re.I,
)
_BARE_SUMMARY = re.compile(
    # 结尾允许 pytest 给长跑套件加的人类可读时长，如 `in 300.83s (0:05:00)`。
    # 少了它，所有跑超过一分钟的套件（也就是一切大型仓库）都解析失败，
    # Verifier 退化成 exit-code-only —— sqlfluff#8230 实战翻的车。
    r"^(?P<body>(?:\d+ \w+(?:, )?)+|no tests ran)\s+in\s+[\d.]+m?s(?:\s*\([\d:]+\))?\s*$",
    re.M | re.I,
)


class PythonAdapter(RepoAdapter):
    kind = "python"
    framework = "pytest"

    @classmethod
    def detect(cls, root: Path) -> RepoAdapter | None:
        markers = [f for f in _MARKERS if (root / f).exists()]
        if not markers:
            # requirements.txt 单独兜底：很多老项目只有它 + 一个 tests/ 目录
            if (root / "requirements.txt").exists() and (root / "tests").is_dir():
                return cls(root, ["发现 requirements.txt + tests/"])
            return None
        notes = [f"发现 {', '.join(markers)}"]
        if (root / "tests").is_dir():
            notes.append("发现 tests/ 目录")
        return cls(root, notes)

    # -- 测试 ---------------------------------------------------------------
    def test_command(self) -> str | None:
        return PYTEST_CMD

    def parse_test_output(self, output: str, exit_code: int) -> TestReport:
        """解析 pytest 输出。

        两条独立的信息来源，谁有用用谁：
          1) 结尾那行 `=== 2 failed, 15 passed in 3.2s ===` → 数字
          2) `FAILED xxx` / `ERROR xxx` 行 → 失败用例名单

        为什么要先定位"结尾那行"再抠数字，而不是全文搜 `(\\d+) passed`：
        全文搜会命中测试输出里任何一处出现的 "3 passed"（比如被测代码自己
        打印的日志、或者 -v 模式下的中间统计），抠到的数字来自错误的地方。
        """
        # 两种格式都扫，取【出现位置最靠后】的那个 —— 它才是真正的收尾统计。
        matches = [*_SUMMARY_LINE.finditer(output), *_BARE_SUMMARY.finditer(output)]
        body = max(matches, key=lambda m: m.start()).group("body") if matches else ""

        parsed = bool(body)
        passed = grab_int(r"(\d+) passed", body) or 0
        failed = grab_int(r"(\d+) failed", body) or 0
        errors = grab_int(r"(\d+) error", body) or 0
        skipped = grab_int(r"(\d+) skipped", body) or 0

        # 收集阶段就炸了（import 失败、语法错误）：一个用例都没跑起来。
        # 这种情况必须能识别 —— 它长得像"0 failed"，但其实是"全军覆没"。
        if not parsed:
            if re.search(r"error during collection|ERROR collecting", output, re.I):
                parsed, errors = True, max(errors, 1)
            elif re.search(r"^\s*no tests ran", output, re.M | re.I):
                parsed = True

        failures = _pytest_failures(output)
        failed_names = [f.test for f in failures]
        # 名单比数字更可信（数字可能被"结尾那行"缺失拖累）
        if failed_names and not failed:
            failed = len(failed_names)
            parsed = True

        return TestReport(
            exit_code=exit_code, passed=passed, failed=failed, errors=errors,
            skipped=skipped, failed_names=failed_names, failures=failures,
            tail=tail(output), parsed=parsed, framework="pytest",
        )

    # -- 验证流水线 ----------------------------------------------------------
    def validation_steps(self) -> list[ValidationStep]:
        steps: list[ValidationStep] = []
        if self._configured("ruff", ".ruff.toml", "ruff.toml"):
            steps.append(ValidationStep("lint", f"{sys.executable} -m ruff check .",
                                        required=False))
        if self._configured("mypy", "mypy.ini", ".mypy.ini"):
            steps.append(ValidationStep("typecheck", f"{sys.executable} -m mypy .",
                                        required=False))
        steps.append(ValidationStep("test", PYTEST_CMD))
        return steps

    def _configured(self, tool: str, *files: str) -> bool:
        """工具是否被这个项目配置过。没配就不该跑 —— 对没配 mypy 的项目跑 mypy
        会喷出几百条无关错误，纯纯的噪音。"""
        if any((self.root / f).exists() for f in files):
            return True
        pyproject = self.root / "pyproject.toml"
        if pyproject.exists():
            try:
                return f"[tool.{tool}" in pyproject.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                return False
        return False

    # -- 跑起来 -------------------------------------------------------------
    def services(self) -> list[ServiceSpec]:
        """Python Web 三大框架的启动方式。探测靠标志文件 + 依赖声明，零成本。"""
        deps = self._declared_deps()

        if (self.root / "manage.py").exists():
            return [ServiceSpec(
                name="web", command=f"{sys.executable} manage.py runserver 127.0.0.1:8000 --noreload",
                port=8000, health_path="/",
                # Django 就绪日志：Starting development server at http://...
                ready_pattern=r"Starting development server",
            )]

        module = self._asgi_module()
        if module and ("fastapi" in deps or "uvicorn" in deps or "starlette" in deps):
            return [ServiceSpec(
                name="api", command=f"{sys.executable} -m uvicorn {module} --port 8000",
                port=8000, health_path="/", ready_pattern=r"Uvicorn running on",
            )]
        if module and "flask" in deps:
            return [ServiceSpec(
                name="web", command=f"{sys.executable} -m flask --app {module.split(':')[0]} run --port 8000",
                port=8000, health_path="/", ready_pattern=r"Running on http",
            )]
        return []

    def _declared_deps(self) -> str:
        """把依赖声明拼成一个小写字符串，用 `in` 判断就够了 —— 不解析 TOML
        （标准库直到 3.11 才有 tomllib，而本项目支持 3.10）。"""
        blob = []
        for name in ("pyproject.toml", "requirements.txt", "setup.py", "Pipfile"):
            f = self.root / name
            if f.exists():
                try:
                    blob.append(f.read_text(encoding="utf-8", errors="ignore"))
                except OSError:
                    pass
        return "\n".join(blob).lower()

    def _asgi_module(self) -> str | None:
        """找 ASGI/WSGI 入口。只看几个约定俗成的位置，找不到就返回 None ——
        猜错一个入口 = 起不来 = 白等一个启动超时，宁可老实说不知道。"""
        for rel in ("main.py", "app.py", "asgi.py", "wsgi.py",
                    "app/main.py", "src/main.py", "api/main.py"):
            p = self.root / rel
            if not p.exists():
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            m = re.search(r"^(\w+)\s*=\s*(?:FastAPI|Flask|Starlette)\(", text, re.M)
            if m:
                dotted = rel.removesuffix(".py").replace("/", ".")
                return f"{dotted}:{m.group(1)}"
        return None

    # -- 环境体检 -----------------------------------------------------------
    def env_requirements(self) -> list[EnvRequirement]:
        return [
            EnvRequirement("python", [sys.executable, "--version"],
                           "Python 解释器不可用（不该发生）"),
            EnvRequirement("pytest", [sys.executable, "-m", "pytest", "--version"],
                           "pip install pytest（很多仓库还需要 pytest-cov）"),
        ]


# `-ra` 摘要行：FAILED tests/x.py::test_y - TypeError: 说明
_SUMMARY_FAIL = re.compile(r"^(?:FAILED|ERROR)\s+(?P<id>\S.*?)(?:\s+-\s+(?P<msg>.*))?$")
# 回溯里定位那一行：src/config.py:48: TypeError
_TRACE_LOC = re.compile(r"^(?P<file>[\w./\\-]+\.py):(?P<line>\d+):\s*(?P<exc>\w+)?\s*$")
# 失败小节标题：_______ test_name _______
_FAIL_HEADER = re.compile(r"^_{3,}\s+(?P<name>.+?)\s+_{3,}$")


def _pytest_failed_names(output: str) -> list[str]:
    """兼容入口：只要用例 id 名单（老调用方和测试仍在用）。"""
    return [f.test for f in _pytest_failures(output)]


def _pytest_failures(output: str) -> list[TestFailure]:
    """把 pytest 输出解析成结构化失败证据：用例 id + 异常类型 + 位置 + 首行说明。

    【这里藏着一个真实踩过的坑】按空格切分会把带空格的参数化用例 id 切碎：
        FAILED tests/t.py::test_x[a b] - assert ...
    按空格切 → `tests/t.py::test_x[a` —— 一个跑不起来的碎片。
    所以要用正则一路吃到 " - " 或行尾，而不是 split()[1]。

    位置（哪个源文件第几行炸的）来自 FAILURES 小节的回溯，摘要行里没有它 ——
    而它恰恰是最有指导性的一个字段：它直接告诉 agent 该去改哪个文件。
    取小节里【最后一个】符合 `file.py:行号:` 的行：pytest 的回溯自外向内，
    最后一行才是真正抛异常的地方，前面几行是调用链。
    """
    locations = _failure_locations(output)
    failures: list[TestFailure] = []
    seen: set[str] = set()

    for line in output.splitlines():
        m = _SUMMARY_FAIL.match(line.strip())
        if not m:
            continue
        test = (m.group("id") or "").strip()
        if not test or test in seen:
            continue
        seen.add(test)

        msg = (m.group("msg") or "").strip()
        # "TypeError: 说明" → 异常类型 + 说明；没有冒号就整句当说明
        exc = ""
        if msg:
            head = msg.split(":", 1)[0].strip()
            if re.fullmatch(r"[A-Za-z_][\w.]*(Error|Exception|Warning|Exit)?", head) \
                    and head[:1].isupper():
                exc = head
        loc, trace_exc = locations.get(_short_name(test), ("", ""))
        failures.append(TestFailure(test=test, exception=exc or trace_exc,
                                    location=loc, message=msg))
    return failures


def _short_name(test_id: str) -> str:
    """归一到用例短名，让两种写法能对上号。

        摘要行  tests/t.py::TestX::test_y[a b]   → test_y
        小节头  TestX.test_y[a b]                → test_y

    两边格式不同（一个用 `::`，一个用 `.`），所以两种分隔符都要吃掉；
    参数化后缀也要去掉，否则 `[a b]` 里的空格会让两边再次对不上。
    """
    last = test_id.split("::")[-1].split("[")[0]
    return last.split(".")[-1].strip()


def _failure_locations(output: str) -> dict[str, tuple[str, str]]:
    """扫 `=== FAILURES ===` 小节，得到 用例短名 → (源码位置, 异常类型)。"""
    found: dict[str, tuple[str, str]] = {}
    current: str | None = None
    for line in output.splitlines():
        header = _FAIL_HEADER.match(line.strip())
        if header:
            current = _short_name(header.group("name"))
            continue
        if current is None:
            continue
        if line.startswith("=") or line.startswith("- "):   # 小节结束
            current = None
            continue
        loc = _TRACE_LOC.match(line.strip())
        if loc:
            # 一路覆盖：回溯最后一条才是真正的抛出点
            found[current] = (f"{loc.group('file')}:{loc.group('line')}",
                              loc.group("exc") or "")
    return found
