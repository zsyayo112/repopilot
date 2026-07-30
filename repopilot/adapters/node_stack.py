"""Node / TypeScript 技术栈 —— 也就是"第二门语言"。

为什么第二门语言必须是 TS/Node：它和 Python 差异足够大（包管理器有四种、
测试框架有三家、还多出 typecheck 和 build 两道关），能真正检验 Adapter
这层抽象是不是骗人的。如果加一门语言需要改核心，那抽象就是假的。

这个 adapter 比 Python 多做三件事：
  1) 认包管理器（npm/pnpm/yarn/bun）—— 命令前缀不一样
  2) 认测试框架（jest/vitest/mocha）—— 输出格式完全不一样
  3) 认应用框架（next/vite/nest）—— 起服务的命令和默认端口不一样
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .base import (
    EnvRequirement,
    RepoAdapter,
    ServiceSpec,
    TestReport,
    ValidationStep,
    grab_int,
    tail,
)

# 锁文件 → 包管理器。锁文件比 package.json 里的声明可信：它是实际用过的证据。
_LOCKFILES = [
    ("pnpm-lock.yaml", "pnpm"),
    ("yarn.lock", "yarn"),
    ("bun.lockb", "bun"),
    ("package-lock.json", "npm"),
]

# 应用框架 → (默认端口, 就绪日志正则)。端口只是默认值，实际以健康检查为准。
_APP_FRAMEWORKS = {
    "next": (3000, r"(?:ready|Ready|started server on|Local:)"),
    "vite": (5173, r"(?:ready in|Local:)"),
    "@nestjs/core": (3000, r"Nest application successfully started"),
    "@angular/core": (4200, r"Compiled successfully"),
    "nuxt": (3000, r"Listening on"),
    "@remix-run/dev": (3000, r"Remix App Server started"),
}


class NodeAdapter(RepoAdapter):
    kind = "node"

    def __init__(self, root: Path, notes: list[str] | None = None,
                 pkg: dict | None = None):
        super().__init__(root, notes)
        self.pkg = pkg or {}
        self.scripts: dict[str, str] = self.pkg.get("scripts", {}) or {}
        self.deps: dict[str, str] = {
            **(self.pkg.get("dependencies") or {}),
            **(self.pkg.get("devDependencies") or {}),
        }
        self.pm = self._package_manager()
        self.kind = self._kind()
        self.framework = self._test_framework()

    @classmethod
    def detect(cls, root: Path) -> RepoAdapter | None:
        pkg_file = root / "package.json"
        if not pkg_file.exists():
            return None
        try:
            pkg = json.loads(pkg_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pkg = {}
        adapter = cls(root, [], pkg)
        adapter.notes = [f"发现 package.json（{adapter.kind}，包管理器 {adapter.pm}，"
                         f"测试框架 {adapter.framework}）"]
        if adapter.test_command() is None:
            adapter.notes.append("package.json 里没有 test 脚本，请用 --test-cmd 指定")
        return adapter

    # -- 身份识别 -----------------------------------------------------------
    def _package_manager(self) -> str:
        # packageManager 字段（corepack 约定）最权威："pnpm@8.6.0"
        declared = str(self.pkg.get("packageManager", ""))
        for _, pm in _LOCKFILES:
            if declared.startswith(pm):
                return pm

        # 锁文件次之，它是"实际用过什么"的证据。
        # 【必须往上找】：pnpm/yarn workspace 的锁文件只在【仓库根】有一份，
        # apps/web/ 里是空的。只看当前目录会得出 npm，然后在一个 pnpm 装的
        # node_modules 上跑 npm test —— 大概率报模块找不到，而且看起来像代码问题。
        for directory in [self.root, *self.root.parents]:
            for filename, pm in _LOCKFILES:
                if (directory / filename).exists():
                    return pm
            if (directory / "pnpm-workspace.yaml").exists():
                return "pnpm"
            if (directory / ".git").exists():
                break              # 到仓库根就停，别爬到用户主目录去
        return "npm"

    def _kind(self) -> str:
        if "@nestjs/core" in self.deps:
            return "nestjs"
        if "next" in self.deps:
            return "nextjs"
        if "nuxt" in self.deps:
            return "nuxt"
        if "@angular/core" in self.deps:
            return "angular"
        if "vite" in self.deps:
            return "vite"
        return "node"

    def _test_framework(self) -> str:
        for name in ("vitest", "jest", "mocha", "ava", "@japa/runner"):
            if name in self.deps:
                return name.lstrip("@").split("/")[0]
        # 没声明依赖，退而看 test 脚本里写了什么
        script = self.scripts.get("test", "")
        for name in ("vitest", "jest", "mocha", "ava", "tap", "node --test"):
            if name in script:
                return name.split()[0]
        return "generic"

    # -- 测试 ---------------------------------------------------------------
    def _run(self, script: str) -> str:
        """把"跑某个 npm script"翻译成当前包管理器的命令。

        npm 需要 `npm test`（内置命令）或 `npm run xxx`；pnpm/yarn/bun 统一 `pm xxx`。
        --silent 只对 npm 加：它默认会打一堆 npm 自己的噪音行。
        """
        if self.pm == "npm":
            return "npm test --silent" if script == "test" else f"npm run {script} --silent"
        return f"{self.pm} {script}"

    def test_command(self) -> str | None:
        if "test" in self.scripts:
            return self._run("test")
        return None

    def parse_test_output(self, output: str, exit_code: int) -> TestReport:
        """按测试框架分派。

        每个解析器都【先试 JSON、再退回文本】—— 这样如果哪天用
        `--test-cmd "npx jest --json"` 跑，解析自动变准，不用改任何代码。
        """
        report = _parse_json_report(output, exit_code)
        if report is not None:
            report.framework = f"{self.framework}(json)"
            return report

        if self.framework == "jest":
            return _parse_jest(output, exit_code)
        if self.framework == "vitest":
            return _parse_vitest(output, exit_code)
        if self.framework == "mocha":
            return _parse_mocha(output, exit_code)
        return super().parse_test_output(output, exit_code)

    # -- 验证流水线：Node 项目的真正验收标准远不止 test ------------------------
    def validation_steps(self) -> list[ValidationStep]:
        steps: list[ValidationStep] = []
        if "lint" in self.scripts:
            steps.append(ValidationStep("lint", self._run("lint"), required=False))

        # typecheck：优先用项目自己的脚本；没有但有 tsconfig.json 就直接 tsc --noEmit。
        # 这一步专治"单测全绿但 tsc 红"—— TS 项目最典型的假通过。
        if "typecheck" in self.scripts:
            steps.append(ValidationStep("typecheck", self._run("typecheck")))
        elif "type-check" in self.scripts:
            steps.append(ValidationStep("type-check", self._run("type-check")))
        elif (self.root / "tsconfig.json").exists():
            steps.append(ValidationStep("typecheck", f"{self._exec()} tsc --noEmit"))

        cmd = self.test_command()
        if cmd:
            steps.append(ValidationStep("test", cmd))
        if "build" in self.scripts:
            # build 最慢，放最后：前面任何一步失败都不用白等它
            steps.append(ValidationStep("build", self._run("build")))
        return steps

    def _exec(self) -> str:
        """执行 node_modules 里的二进制：npm/bun 用 npx，pnpm/yarn 有自己的。"""
        return {"pnpm": "pnpm exec", "yarn": "yarn", "bun": "bunx"}.get(self.pm, "npx")

    # -- 跑起来 -------------------------------------------------------------
    def services(self) -> list[ServiceSpec]:
        port, ready = next(
            ((p, r) for dep, (p, r) in _APP_FRAMEWORKS.items() if dep in self.deps),
            (None, None),
        )
        # 挑启动脚本：dev 优先（带热重载、启动快），其次 nest 的 start:dev，最后 start
        script = next((s for s in ("dev", "start:dev", "serve", "start")
                       if s in self.scripts), None)
        if script is None or port is None:
            return []
        return [ServiceSpec(
            name="web" if self.kind != "nestjs" else "api",
            command=self._run(script), port=port,
            health_path="/", ready_pattern=ready,
            # 框架 dev server 冷启动要编译，比 Python 慢得多
            startup_timeout=180,
            # 固定端口：不让框架自己找空端口，否则健康检查不知道该敲哪个门
            env={"PORT": str(port), "BROWSER": "none", "CI": "1"},
        )]

    def e2e_command(self) -> str | None:
        if any(self.root.glob("playwright.config.*")):
            return f"{self._exec()} playwright test"
        if any(self.root.glob("cypress.config.*")):
            return f"{self._exec()} cypress run"
        return None

    # -- 环境体检 -----------------------------------------------------------
    def env_requirements(self) -> list[EnvRequirement]:
        reqs = [
            EnvRequirement("node", ["node", "--version"], "安装 Node.js 18+"),
            EnvRequirement(self.pm, [self.pm, "--version"],
                           f"这个仓库的锁文件对应 {self.pm}，用别的包管理器装依赖会不一致"),
        ]
        # node_modules 缺失是 Node 项目最常见的"看起来像代码问题的环境问题"：
        # 报错是一行 Cannot find module，和真的写错 import 长得一模一样。
        reqs.append(EnvRequirement(
            "node_modules",
            hint=f"依赖没装：先在 {self.root.name}/ 里跑 `{self.pm} install`"
                 "（RepoPilot 的安全策略禁止 agent 自己装包）",
            must_exist=self.root / "node_modules"))
        return reqs


# ---------------------------------------------------------------------------
# 各家测试框架的输出解析。三家格式毫无共同点 —— 这正是"只换测试命令不换解析器"
# 行不通的原因：命令能跑通，但你不知道哪几个用例挂了、失败数有没有变少。
# ---------------------------------------------------------------------------
def _parse_json_report(output: str, exit_code: int) -> TestReport | None:
    """试着把输出当 jest/vitest 的 --json 结果解析。不像 JSON 就返回 None。"""
    start = output.find('{"')
    if start < 0:
        return None
    try:
        data = json.loads(output[start:output.rindex("}") + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or "numTotalTests" not in data:
        return None

    names: list[str] = []
    for suite in data.get("testResults", []) or []:
        for case in suite.get("assertionResults", []) or []:
            if case.get("status") == "failed":
                full = " > ".join(filter(None, [*(case.get("ancestorTitles") or []),
                                                case.get("title") or ""]))
                names.append(full or suite.get("name", "?"))
    return TestReport(
        exit_code=exit_code,
        passed=int(data.get("numPassedTests", 0)),
        failed=int(data.get("numFailedTests", 0)),
        errors=int(data.get("numRuntimeErrorTestSuites", 0)),
        skipped=int(data.get("numPendingTests", 0)),
        failed_names=names, tail=tail(output), parsed=True,
    )


def _parse_jest(output: str, exit_code: int) -> TestReport:
    """jest 的统计在 `Tests:` 那一行：
           Tests:       2 failed, 15 passed, 17 total
    """
    m = re.search(r"^Tests:\s+(?P<body>.+)$", output, re.M)
    body = m.group("body") if m else ""
    # "Test suite failed to run"（import 挂了）不产生任何 Tests: 行的失败计数，
    # 但它是最严重的一种失败 —— 单独从 Test Suites: 行捞出来记作 errors。
    suites = re.search(r"^Test Suites:\s+(?P<body>.+)$", output, re.M)
    suite_failed = grab_int(r"(\d+) failed", suites.group("body")) if suites else None
    return TestReport(
        exit_code=exit_code,
        passed=grab_int(r"(\d+) passed", body) or 0,
        failed=grab_int(r"(\d+) failed", body) or 0,
        errors=suite_failed or 0,
        skipped=(grab_int(r"(\d+) skipped", body) or 0) + (grab_int(r"(\d+) todo", body) or 0),
        failed_names=_failed_names_from_marks(output, "✕", "×"),
        tail=tail(output), parsed=bool(body) or suite_failed is not None,
        framework="jest",
    )


def _parse_vitest(output: str, exit_code: int) -> TestReport:
    """vitest 用竖线分隔：
           Tests  2 failed | 15 passed (17)
    """
    m = re.search(r"^\s*Tests\s+(?P<body>.+)$", output, re.M)
    body = m.group("body") if m else ""
    return TestReport(
        exit_code=exit_code,
        passed=grab_int(r"(\d+) passed", body) or 0,
        failed=grab_int(r"(\d+) failed", body) or 0,
        skipped=grab_int(r"(\d+) skipped", body) or 0,
        failed_names=_failed_names_from_marks(output, "×", "✕", "FAIL"),
        tail=tail(output), parsed=bool(body), framework="vitest",
    )


def _parse_mocha(output: str, exit_code: int) -> TestReport:
    """mocha 是两行分开的：`15 passing (2s)` / `2 failing`。"""
    passed = grab_int(r"(\d+) passing", output)
    failed = grab_int(r"(\d+) failing", output)
    return TestReport(
        exit_code=exit_code, passed=passed or 0, failed=failed or 0,
        skipped=grab_int(r"(\d+) pending", output) or 0,
        failed_names=[], tail=tail(output),
        parsed=passed is not None or failed is not None, framework="mocha",
    )


def _failed_names_from_marks(output: str, *marks: str) -> list[str]:
    """从 `✕ should do the thing (5 ms)` 这类行里抠用例名。

    去掉尾部的耗时括号 —— 带耗时的名字每次跑都不一样，
    verifier 会把"同一个用例"当成"新出现的失败"，直接误报回归。
    """
    names: list[str] = []
    for line in output.splitlines():
        s = line.strip()
        for mark in marks:
            if not s.startswith(mark):
                continue
            name = re.sub(r"\s*\(\d+\s*m?s\)\s*$", "", s[len(mark):]).strip()
            if name and name not in names:
                names.append(name)
            break
    return names
