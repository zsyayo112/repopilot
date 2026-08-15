"""Tool Runtime：agent 对目标仓库能做的所有动作。

【工具分组：为什么不是把 28 个工具一次全给它】
这一版工具从 9 个涨到 29 个。但**默认只暴露 14 个**，剩下的按需开启：

    core     14 个   永远可用
    runtime   6 个   --with-runtime 才开（要起真实服务）
    browser   9 个   --with-runtime 且装了 Playwright 才开

理由不是省几个 token 那么表面。工具列表是每一轮请求都要重发的东西，而且
**选项越多，模型选错的概率越高**：给一个纯 Python 库任务塞九个浏览器工具，
它会真的去试着打开浏览器。能力应该跟着任务的形状走，不是有什么就给什么。
这叫渐进式暴露（progressive disclosure）。

【职责分离没变】
工具只知道"怎么做"，"该不该做"归 permissions / policy，在 executor 里串。
工具永远返回字符串、永远不抛异常 —— 一抛异常主循环就断、消息配对就破。

四个底层组件各自被哪些工具包着（对应笔记里那四层）：
    Adapter          → run_tests / run_validation / list_projects / run_e2e
    RuntimeManager   → start_services / service_status / read_service_logs / …
    BrowserSession   → browser_* / run_scenario
    EvidenceCollector→ browser_errors（以及被 browser_* 自动写入）
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from . import verifier
from .adapters import RepoProfile
from .adapters.symbols import render as render_symbols
from .config import CMD_TIMEOUT, MAX_TOOL_OUTPUT
from .policy import check_command, jail, looks_irreversible
from .workspace import Workspace


class ToolKit:
    def __init__(self, ws: Workspace, profile: RepoProfile | None = None, *,
                 groups: tuple[str, ...] = ("core",),
                 artifacts_dir: Path | None = None,
                 trace=None, budget=None, ledger=None):
        self.ws = ws
        self.root = ws.root
        self.profile = profile
        self.groups = tuple(groups)
        self.artifacts_dir = artifacts_dir
        self.trace = trace
        # 预算/账本可以不传（单元测试、detect 子命令都不需要）。传了的话，
        # 工具层就多一道硬约束：测试执行次数也是预算的一部分。
        self.budget = budget
        self.ledger = ledger

        # 两个重量级组件都是【懒创建】的：不调用相关工具就永远不会起进程、
        # 不会拉浏览器。定义在这里只是登记"我有这个能力"。
        self._runtime = None
        self._browser = None

        # 超长输出落盘的序号：句柄要稳定可引用，所以是递增计数不是随机名。
        self._spill_seq = 0

        self._impl = {
            # --- core ---
            "read_file": self.read_file,
            "read_artifact": self.read_artifact,
            "list_files": self.list_files,
            "search_code": self.search_code,
            "list_symbols": self.list_symbols,
            "edit_file": self.edit_file,
            "write_file": self.write_file,
            "run_bash": self.run_bash,
            "run_tests": self.run_tests,
            "run_validation": self.run_validation,
            "list_projects": self.list_projects,
            "check_environment": self.check_environment,
            "explore": self.explore,
            "git_diff": self.git_diff,
            # --- runtime ---
            "start_services": self.start_services,
            "service_status": self.service_status,
            "read_service_logs": self.read_service_logs,
            "restart_service": self.restart_service,
            "stop_services": self.stop_services,
            "run_e2e": self.run_e2e,
            # --- browser ---
            "browser_open": self.browser_open,
            "browser_snapshot": self.browser_snapshot,
            "browser_click": self.browser_click,
            "browser_fill": self.browser_fill,
            "browser_select": self.browser_select,
            "browser_viewport": self.browser_viewport,
            "browser_screenshot": self.browser_screenshot,
            "browser_errors": self.browser_errors,
            "run_scenario": self.run_scenario,
        }

    # ======================================================== 只读：看代码
    def read_file(self, path: str, start_line: int = 0, end_line: int = 0) -> str:
        """支持行号范围：真实仓库里的大文件不该整读（上下文预算！）。"""
        text = jail(path, self.root).read_text(encoding="utf-8", errors="replace")
        if start_line or end_line:
            lines = text.splitlines()
            start = max(start_line - 1, 0)
            end = end_line or len(lines)
            text = "\n".join(lines[start:end])
        return text

    def read_artifact(self, handle: str, start_line: int = 0, end_line: int = 0) -> str:
        """读取被落盘的超长工具输出。句柄来自那次输出末尾的提示。

        【为什么存在】以前超过 MAX_TOOL_OUTPUT 的输出直接截断 —— 丢掉的部分
        再也拿不回来，模型只能原样重调一次工具、再被截一次。落盘之后，
        截断丢掉的不再是信息，只是把它搬到了句柄后面，按行段取用。
        """
        if self.artifacts_dir is None:
            return "错误：本次运行没有落盘目录，没有可读的句柄"
        safe = Path(handle).name                     # 句柄不是路径，防目录穿越
        path = Path(self.artifacts_dir) / "spill" / f"{safe}.txt"
        if not path.exists():
            return f"错误：句柄不存在：{handle}"
        lines = path.read_text(encoding="utf-8").splitlines()
        total = len(lines)
        start = max(start_line - 1, 0)
        end = min(end_line or start + 150, total)    # 不给范围就从头给一屏
        text = "\n".join(lines[start:end])
        if len(text) > MAX_TOOL_OUTPUT - 200:
            # 防递归落盘：execute() 对本工具豁免 spill，这里自己兜底截断。
            # 留 200 字符余量，否则这条提示会反被 execute() 的通用截断切掉。
            return (text[:MAX_TOOL_OUTPUT - 200]
                    + f"\n\n[范围太宽被截断。共 {total} 行，请缩小 start_line/end_line]")
        return text + f"\n\n[第 {start + 1}-{end} 行 / 共 {total} 行]"

    def _spill(self, name: str, result: str) -> str:
        """超长输出：全文落盘，返回头部 + 句柄。"""
        self._spill_seq += 1
        handle = f"{self._spill_seq:03d}-{name}"
        spill_dir = Path(self.artifacts_dir) / "spill"
        spill_dir.mkdir(parents=True, exist_ok=True)
        (spill_dir / f"{handle}.txt").write_text(result, encoding="utf-8")
        total = result.count("\n") + 1
        head = result[:MAX_TOOL_OUTPUT]
        cut = head.rfind("\n")
        if cut > MAX_TOOL_OUTPUT // 2:               # 尽量在行边界截，别切半行
            head = head[:cut]
        shown = head.count("\n") + 1
        return (f"{head}\n\n[输出共 {total} 行 / {len(result):,} 字符，"
                f"只显示了前 {shown} 行。完整内容已落盘，"
                f'继续读：read_artifact(handle="{handle}", start_line=…, end_line=…)；'
                f"或用更窄的参数重新调用 {name}。]")

    def list_files(self, directory: str = ".") -> str:
        d = jail(directory, self.root)
        if not d.exists():
            return f"目录不存在：{directory}"
        names = [f"{p.name}/" if p.is_dir() else p.name
                 for p in sorted(d.iterdir()) if p.name != ".git"]
        return "\n".join(names) or "(空目录)"

    def search_code(self, pattern: str, glob: str = "") -> str:
        """ripgrep 优先，没有就退化成 grep。这是真实仓库的第一入口：先搜再读。"""
        import shutil
        if shutil.which("rg"):
            cmd = ["rg", "-n", "--no-heading", "-S", "--max-columns", "200"]
            if glob:
                cmd += ["-g", glob]
            cmd += [pattern, "."]
        else:
            cmd = ["grep", "-rn", "-I", "--exclude-dir=.git", pattern, "."]

        proc = subprocess.run(cmd, cwd=self.root, capture_output=True,
                              text=True, timeout=30)
        out = proc.stdout.strip()
        if not out:
            return f"没有匹配：{pattern}"
        lines = out.splitlines()
        if len(lines) > 80:
            out = "\n".join(lines[:80]) + \
                f"\n… 共 {len(lines)} 处，只显示前 80 行。pattern 太宽了，收窄它。"
        return out

    def list_symbols(self, path: str) -> str:
        """列出文件里的类/函数骨架。**多语言**：Python 走真 AST，
        TS/JS/Go/Rust/Java/Ruby/Kotlin 走正则近似（见 adapters/symbols.py）。"""
        p = jail(path, self.root)
        if not p.exists():
            return f"文件不存在：{path}"
        symbols = self.profile.symbols(p) if self.profile else None
        if symbols is None:
            from .adapters.symbols import LANG_BY_SUFFIX, extract
            symbols = extract(p)
        if symbols is None:
            return (f"不支持从 {p.suffix or '无后缀'} 文件提取符号。"
                    f"支持的后缀：{', '.join(sorted(LANG_BY_SUFFIX))}。"
                    "这种文件直接用 read_file 配行号范围读。")
        return render_symbols(symbols, p)

    def git_diff(self) -> str:
        stat, diff = self.ws.diff_stat(), self.ws.diff()
        return f"{stat}\n\n{diff}" if diff else "(暂无改动)"

    def list_projects(self) -> str:
        """列出仓库里的项目单元（monorepo 用）。

        对单一技术栈的仓库它只会返回一行 —— 那也是有信息量的：
        "这个仓库只有一个项目，别去找别的模块了"。
        """
        repo = self.profile.repository if self.profile else None
        if repo is None or not repo.units:
            return "没有识别到项目单元（可用 --test-cmd 指定测试命令）"
        head = repo.render()
        if repo.is_monorepo:
            head += ("\n\n提示：改动集中在某个单元时，用 run_tests(project=\"<路径>\") "
                     "只跑那个单元的测试，比全仓库跑快得多。")
        return head

    def check_environment(self) -> str:
        """体检环境。**测试失败时先跑这个** —— 分清是代码问题还是环境问题。"""
        if self.profile is None:
            return "没有项目画像，无法体检"
        from .doctor import diagnose
        report = diagnose(self.ws, self.profile,
                          want_browser="browser" in self.groups)
        return report.render(color=False)

    # ======================================================== 写
    def edit_file(self, path: str, old_string: str, new_string: str) -> str:
        """精确片段替换。old_string 必须在文件中【恰好出现一次】——
        这个约束逼着模型先 read_file 看清现状，杜绝凭记忆瞎改。"""
        p = jail(path, self.root, writing=True)
        if not p.exists():
            return f"错误：文件不存在：{path}（新文件请用 write_file）"
        text = p.read_text(encoding="utf-8")
        n = text.count(old_string)
        if n == 0:
            return ("错误：old_string 在文件中不存在。先 read_file 看清当前内容，"
                    "注意空格和缩进必须逐字符一致。")
        if n > 1:
            return f"错误：old_string 出现了 {n} 次，无法确定改哪一处。请包含更多上下文使其唯一。"
        p.write_text(text.replace(old_string, new_string, 1), encoding="utf-8")
        return f"已修改 {path}（1 处替换）"

    def write_file(self, path: str, content: str) -> str:
        """只用于创建全新文件（比如补测试）。改已有文件必须走 edit_file。"""
        p = jail(path, self.root, writing=True)
        if p.exists():
            return f"错误：{path} 已存在。修改已有文件请用 edit_file（防止整文件覆盖丢内容）。"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"已创建 {path}（{len(content)} 字符）"

    # ======================================================== 执行 / 验证
    def run_bash(self, command: str) -> str:
        ok, reason = check_command(command)
        if not ok:
            return f"错误：{reason}"
        r = self.ws.exec.run(command, cwd=self.root, timeout=CMD_TIMEOUT)
        if r.timed_out:
            partial = r.output.strip()
            return (f"错误：命令执行超时（>{CMD_TIMEOUT}s）"
                    + (f"\n超时前输出末尾：\n{partial[-1500:]}" if partial else ""))
        return f"exit_code={r.code}\n{r.output.strip() or '(无输出)'}"

    def run_tests(self, project: str = "", scope: str = "") -> str:
        """跑 adapter 给定的测试命令。模型不许自己猜测试命令 —— 猜错一次烧一轮 token。

        project 非空时只跑那个项目单元（monorepo 的"改哪块跑哪块"）。

        返回的是【结构化失败证据】而不是整段 pytest 日志（见 base.TestReport.evidence）：
        同样的信息量通常只要 1/5 的字符，而且模型不会再被一堆无关 warning 带偏。
        解析不出来的技术栈会自动退回原始日志尾巴 —— 不确定要显式，不能伪装成确定。
        """
        if self.profile is None or not self.profile.test_cmd:
            return "错误：没有可用的测试命令（adapter 未识别，需要 --test-cmd）"

        if self.ledger is not None:
            self.ledger.note_test_run()
            if self.ledger.test_runs > self.ledger.budget.max_test_runs:
                return (f"错误：本次任务的测试执行次数已达上限 "
                        f"{self.ledger.budget.max_test_runs}。"
                        "先想清楚再改，不要用反复跑测试代替分析。")

        if project:
            repo = self.profile.repository
            unit = next((u for u in (repo.units if repo else []) if u.path == project), None)
            if unit is None:
                known = ", ".join(u.path for u in (repo.units if repo else [])) or "(无)"
                return f"错误：没有名为 {project!r} 的项目单元。已知：{known}"
            if not unit.test_cmd:
                return f"错误：项目单元 {project} 没有可用的测试命令"
            # 用【这个单元自己的】adapter 解析：根目录可能是 workspace 管理者，
            # 它的解析器读不懂子项目用的 vitest/jest
            cmd = unit.test_cmd
            if scope:
                cmd = verifier.scoped_command(cmd, self.root / unit.path, scope)
            report = verifier.run_tests_in(self.ws, self.profile, cmd,
                                           unit.path, adapter=unit.adapter)
            return f"[project={project}]\n{report.evidence()}"

        if scope:
            try:
                target = jail(scope, self.root)
            except PermissionError as e:
                return f"错误：{e}"
            if not target.exists():
                return f"错误：{scope} 不存在。scope 必须是仓库里真实存在的测试文件或目录。"
            rel = target.relative_to(self.root).as_posix()
            cmd = verifier.scoped_command(self.profile.test_cmd, self.root, rel)
            report = verifier.run_tests_in(self.ws, self.profile, cmd)
            return f"[scope={rel}]\n{report.evidence()}"

        report = verifier.run_tests(self.ws, self.profile)
        return report.evidence()

    def run_validation(self, only: str = "") -> str:
        """跑完整验证流水线：lint → typecheck → test → build。

        **测试过了不等于没问题**：TS 项目最典型的假通过就是单测绿、tsc 红。
        only 可以逗号分隔指定几步，比如 "typecheck" 或 "lint,typecheck"。
        """
        if self.profile is None:
            return "错误：没有项目画像"
        names = [s.strip() for s in only.split(",") if s.strip()] or None
        report = verifier.run_validation(self.ws, self.profile, only=names)
        return report.render()

    def explore(self, question: str) -> str:
        """派一个【只读的探索子 agent】去定位问题，它只把结论带回来。

        什么时候用它：要读很多文件才能确定问题在哪的时候。它读的文件内容不会
        进入你的上下文，只有结论会 —— 所以定位大仓库时它比你自己读便宜得多。
        什么时候别用：你已经知道该看哪个文件了（那就直接 read_file，别绕一圈）。
        """
        from .explorer import explore as run_explore
        if self.budget is not None and not self.budget.allow_explorer:
            return ("错误：本次运行关闭了探索子 agent（消融实验配置）。"
                    "请直接用 search_code / list_symbols / read_file 自己定位。")
        return run_explore(self, question, trace=self.trace,
                           budget=self.budget, ledger=self.ledger)

    # ======================================================== 运行时
    @property
    def runtime(self):
        """懒创建 RuntimeManager。"""
        if self._runtime is None:
            from .runtime import RuntimeManager
            specs = self.profile.services() if self.profile else []
            self._runtime = RuntimeManager(self.root, specs)
        return self._runtime

    def start_services(self) -> str:
        """按依赖顺序启动应用，并【等到真正可用】（健康检查通过）才返回。"""
        rt = self.runtime
        if not rt.available:
            return ("这个项目没有识别出可启动的服务。adapter 只认得几种常见框架"
                    "（Next/Vite/Nest/Angular/Django/Flask/FastAPI/Rails/Spring Boot）。"
                    "如果确实是 Web 项目，说明启动方式不常规 —— 用 run_bash 也起不来，"
                    "因为长驻进程需要托管。把这个情况写进你的总结里。")
        ok, detail = rt.start_all()
        return f"{'启动成功' if ok else '启动失败'}\n{detail}\n\n{rt.status()}"

    def service_status(self) -> str:
        return self.runtime.status()

    def read_service_logs(self, service: str = "", lines: int = 50) -> str:
        return self.runtime.logs(service or None, max(1, min(lines, 200)))

    def restart_service(self, service: str) -> str:
        """改完代码要重启才生效 —— 除非 dev server 自带热重载。"""
        return self.runtime.restart(service)

    def stop_services(self) -> str:
        return self.runtime.stop_all()

    def run_e2e(self) -> str:
        """跑项目【已有的】端到端测试（Playwright / Cypress）。

        这是抓运行时 bug 最省力的一招：现成的 E2E 套件往往已经覆盖了关键路径，
        自己开浏览器点之前，先看看它们过不过。
        """
        if self.profile is None:
            return "错误：没有项目画像"
        cmd = self.profile.e2e_command()
        if not cmd:
            return ("这个项目没有检测到 E2E 配置（playwright.config.* / cypress.config.*）。"
                    "如果要验证页面行为，用 browser_* 工具手动复现。")
        r = self.ws.exec.run(cmd, cwd=self.root, timeout=CMD_TIMEOUT * 2)
        return f"$ {cmd}\nexit_code={r.code}\n{r.output[-4000:]}"

    # ======================================================== 浏览器
    @property
    def browser(self):
        """懒创建 BrowserSession。base_url 从 runtime 拿 —— 服务没起就没有地址。"""
        if self._browser is None:
            from .browser import BrowserSession
            base = self._runtime.base_url() if self._runtime else None
            self._browser = BrowserSession(
                base_url=base,
                artifacts_dir=(self.artifacts_dir / "screenshots")
                if self.artifacts_dir else None)
        elif self._browser.base_url is None and self._runtime is not None:
            # 服务比浏览器后起的情况：补上地址
            self._browser.base_url = self._runtime.base_url()
        return self._browser

    def browser_open(self, url: str) -> str:
        return self.browser.goto(url)

    def browser_snapshot(self) -> str:
        return self.browser.snapshot()

    def browser_click(self, ref: str = "", role: str = "", name: str = "") -> str:
        return self.browser.click(ref=ref or None, role=role or None, name=name or None)

    def browser_fill(self, text: str, ref: str = "", label: str = "",
                     submit: bool = False) -> str:
        return self.browser.fill(text, ref=ref or None, label=label or None, submit=submit)

    def browser_select(self, value: str, ref: str = "", label: str = "") -> str:
        return self.browser.select(value, ref=ref or None, label=label or None)

    def browser_viewport(self, width: int, height: int) -> str:
        return self.browser.set_viewport(width, height)

    def browser_screenshot(self, name: str = "") -> str:
        return self.browser.screenshot(name)

    def browser_errors(self) -> str:
        """读运行证据：控制台错误、未捕获异常、失败请求。

        **功能问题优先看这个，不要靠截图。** 按钮画得再漂亮，点不动就是坏的；
        反过来页面看着乱可能只是缺了张图。
        """
        return self.browser.errors()

    def run_scenario(self, scenario_json: str) -> str:
        """跑一个结构化复现场景（步骤 + 断言），返回每条断言的通过情况。

        修改前后必须跑【同一份】scenario：这是"我真的修好了"和
        "我这次点得比较顺"之间唯一的区别。
        """
        from . import scenario as scenario_mod
        try:
            data = json.loads(scenario_json)
        except json.JSONDecodeError as e:
            return f"错误：scenario 不是合法 JSON（{e}）"
        try:
            sc = scenario_mod.Scenario.from_dict(data)
        except ValueError as e:
            return f"错误：scenario 格式不对 —— {e}"
        result = scenario_mod.run(sc, self.browser)
        if self.artifacts_dir:
            (self.artifacts_dir / f"scenario-{sc.name}.json").write_text(
                json.dumps(sc.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return result.render()

    # ======================================================== 分发
    def specs(self) -> list[dict]:
        """当前开启的工具说明书。**只发开启的那些** —— 见文件头的分组说明。"""
        names = [n for g in self.groups for n in TOOL_GROUPS.get(g, ())]
        return [TOOLS_BY_NAME[n] for n in names if n in TOOLS_BY_NAME]

    def enabled(self, name: str) -> bool:
        return any(name in TOOL_GROUPS.get(g, ()) for g in self.groups)

    def execute(self, name: str, args: dict) -> str:
        """永远返回字符串、永远不抛异常、超长自动截断。"""
        fn = self._impl.get(name)
        if fn is None:
            return f"错误：不存在名为 {name} 的工具"
        if not self.enabled(name):
            return (f"错误：工具 {name} 未启用（它属于 "
                    f"{_group_of(name)} 组，需要在启动时开启对应能力）")
        try:
            result = str(fn(**args))
        except subprocess.TimeoutExpired:
            return "错误：命令执行超时"
        except TypeError as e:
            # 参数名/个数不对：明确告诉它签名，别让它反复试
            return f"错误：参数不对（{e}）"
        except Exception as e:
            # BrowserUnavailable 这类"环境没装"的错误，消息本身就是安装指引
            return f"错误：{type(e).__name__}: {e}"
        if len(result) > MAX_TOOL_OUTPUT:
            if self.artifacts_dir is not None and name != "read_artifact":
                result = self._spill(name, result)
            else:
                omitted = len(result) - MAX_TOOL_OUTPUT
                result = result[:MAX_TOOL_OUTPUT] + f"\n\n[输出被截断，省略了 {omitted} 个字符]"
        return result

    def cleanup(self) -> str:
        """收尾：关服务、关浏览器。**必须幂等**（它会被 finally 调用）。

        为什么这件事必须有人负责：留下的 dev server 会一直占着端口，下一次任务
        起不来；留下的 chromium 进程会一直吃内存。**起了东西就得有人负责关。**
        """
        notes = []
        if self._browser is not None and self._browser.active:
            self._browser.close()
            notes.append("浏览器已关闭")
        if self._runtime is not None and self._runtime.available:
            notes.append(self._runtime.stop_all())
        return "；".join(notes) or "没有需要清理的资源"


# ---------------------------------------------------------------------------
# 权限：只读自动放行，会改动世界的要过闸门
# ---------------------------------------------------------------------------
SAFE_TOOLS = {
    # 只读
    "read_file", "read_artifact", "list_files", "search_code", "list_symbols",
    "git_diff", "list_projects", "check_environment", "explore",
    # 跑测试/验证：不改代码，只是执行项目自己的命令
    "run_tests", "run_validation", "run_e2e",
    # 运行时里的只读部分
    "service_status", "read_service_logs", "stop_services",
    # 浏览器：导航和观察一律放行
    "browser_open", "browser_snapshot", "browser_viewport",
    "browser_screenshot", "browser_errors",
    # 交互：默认放行，但下面 is_safe() 会拦住不可逆意图
    "browser_click", "browser_fill", "browser_select", "run_scenario",
}


def is_safe(name: str, args: dict) -> bool:
    """是否可以自动放行。

    比"名字在白名单里"多一层：**浏览器交互要看点的是什么按钮**。
    在页面上乱点通常无害，但"确认支付""删除账户"点下去是不可逆的 ——
    这类必须回到人手上。判断依据是元素的可见名称，因为那正是用户会读到的字。
    """
    if name not in SAFE_TOOLS:
        return False
    if name in ("browser_click", "browser_fill", "browser_select", "run_scenario"):
        blob = " ".join(str(v) for v in args.values())
        return not looks_irreversible(blob)
    return True


def _fn(name: str, desc: str, props: dict, required: list[str]) -> dict:
    """少写点样板：把一条工具说明书折叠成一行调用。"""
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required},
    }}


CORE_TOOLS = [
    _fn("read_file",
        "读取文件内容。大文件请配合 start_line/end_line 分段读，别整读。路径相对仓库根。",
        {"path": {"type": "string"},
         "start_line": {"type": "integer", "description": "起始行号（含，从 1 开始）"},
         "end_line": {"type": "integer", "description": "结束行号（含）"}},
        ["path"]),
    _fn("read_artifact",
        "读取之前某次工具调用被落盘的超长输出（句柄写在那次输出末尾的提示里）。"
        "配合 start_line/end_line 按行段读，别一次全读。",
        {"handle": {"type": "string", "description": "如 003-run_bash"},
         "start_line": {"type": "integer", "description": "起始行号（含，从 1 开始）"},
         "end_line": {"type": "integer", "description": "结束行号（含）"}},
        ["handle"]),
    _fn("list_files", "列出目录内容，子目录名后带 /。",
        {"directory": {"type": "string"}}, []),
    _fn("search_code",
        "在整个仓库里用正则搜索代码，返回 文件:行号:内容。这是定位代码的首选工具，"
        "先搜索缩小范围，再 read_file 细看。",
        {"pattern": {"type": "string", "description": "正则表达式"},
         "glob": {"type": "string", "description": "可选的文件过滤，如 *.py、*.ts"}},
        ["pattern"]),
    _fn("list_symbols",
        "列出一个源文件里所有类/函数/接口的名字与行号，读大文件前先用它看骨架。"
        "支持 Python(精确) 和 TS/JS/Go/Rust/Java/Ruby/Kotlin(正则近似，行号以 read_file 为准)。",
        {"path": {"type": "string"}}, ["path"]),
    _fn("list_projects",
        "列出仓库里的项目单元。monorepo（前端+后端+worker 在一个仓库）必须先看这个，"
        "才知道 issue 属于哪个模块、该跑哪个测试。",
        {}, []),
    _fn("explore",
        "派一个只读的探索子 agent 去定位问题，它读的文件不会占用你的上下文，只回一句结论。"
        "适合『要翻很多文件才能确定问题在哪』的情况；已经知道看哪个文件就别用它。",
        {"question": {"type": "string",
                      "description": "一个具体的定位问题，例如『哪个函数负责解析 <= 查询条件』"}},
        ["question"]),
    _fn("edit_file",
        "修改已有文件：把 old_string 精确替换成 new_string。old_string 必须在文件中"
        "恰好出现一次（含缩进空格，逐字符一致），否则会报错。改之前先 read_file。",
        {"path": {"type": "string"},
         "old_string": {"type": "string"},
         "new_string": {"type": "string"}},
        ["path", "old_string", "new_string"]),
    _fn("write_file",
        "创建全新文件（如新增测试文件）。文件已存在会报错 —— 修改请用 edit_file。",
        {"path": {"type": "string"}, "content": {"type": "string"}},
        ["path", "content"]),
    _fn("run_bash",
        "在仓库根目录执行一条 shell 命令。不要 cd，不要跑测试（跑测试用 run_tests）。",
        {"command": {"type": "string"}}, ["command"]),
    _fn("run_tests",
        "运行项目测试套件（命令由系统配置，你不用关心）。每次实质修改后都应调用。"
        "**改完一处代码要快速验证时，一定要用 scope 把范围缩到相关的测试文件** —— "
        "跑全量套件可能要好几分钟，而且会把大量无关输出灌进你的上下文。"
        "收尾前再不带 scope 跑一次全量，确认没有改坏别处。"
        "monorepo 里可以用 project 只跑一个单元的测试。",
        {"project": {"type": "string",
                     "description": "可选：项目单元路径（来自 list_projects），留空=主项目"},
         "scope": {"type": "string",
                   "description": "可选：只跑这个测试文件或目录，例如 "
                                  "'tests/test_config.py' 或 'tests/api'。留空=全量"}},
        []),
    _fn("run_validation",
        "跑完整验证流水线：lint → typecheck → test → build。"
        "测试通过不代表没问题（典型：单测绿但类型检查/构建失败），收尾前应该跑一次。",
        {"only": {"type": "string",
                  "description": "可选：逗号分隔只跑某几步，如 'typecheck' 或 'lint,typecheck'"}},
        []),
    _fn("check_environment",
        "体检运行环境（依赖是否装好、端口是否被占）。"
        "**测试莫名失败时先跑这个** —— 分清是代码问题还是环境问题，环境问题你改代码修不好。",
        {}, []),
    _fn("git_diff", "查看当前所有未提交的改动。收尾前用它自查改动是否最小、有无误伤。",
        {}, []),
]

RUNTIME_TOOLS = [
    _fn("start_services",
        "启动这个项目的应用（前端/后端），并等到健康检查真正通过才返回。"
        "注意：进程起来了不等于服务可用，这个工具已经帮你等好了。",
        {}, []),
    _fn("service_status", "查看托管服务的状态和可访问地址。", {}, []),
    _fn("read_service_logs",
        "读服务端日志。页面报错但看不出原因时，后端日志往往是答案。",
        {"service": {"type": "string", "description": "服务名，留空=全部"},
         "lines": {"type": "integer", "description": "读最后多少行，默认 50"}},
        []),
    _fn("restart_service",
        "重启一个服务。改完后端代码后如果 dev server 没有热重载，必须重启才生效。",
        {"service": {"type": "string"}}, ["service"]),
    _fn("stop_services", "停止所有托管服务。", {}, []),
    _fn("run_e2e",
        "跑项目已有的端到端测试（Playwright/Cypress）。自己开浏览器点之前先试这个。",
        {}, []),
]

BROWSER_TOOLS = [
    _fn("browser_open",
        "在浏览器里打开页面，返回 URL、标题、带 ref 编号的可交互元素表和页面文本。"
        "可以用相对路径（如 /packages/123），会自动拼上服务地址。",
        {"url": {"type": "string"}}, ["url"]),
    _fn("browser_snapshot",
        "重新读取当前页面的可交互元素表。ref 编号只在最近一次快照后有效 ——"
        "页面变化后要先重新快照再点。",
        {}, []),
    _fn("browser_click",
        "点击页面元素。优先用快照里的 ref（最稳），也可以用 role+name 语义定位。"
        "不要写 CSS 选择器。",
        {"ref": {"type": "string", "description": "来自最近一次快照，如 e12"},
         "role": {"type": "string", "description": "无障碍角色，如 button/link/checkbox"},
         "name": {"type": "string", "description": "元素的可见名称"}},
        []),
    _fn("browser_fill",
        "往输入框填文字。用 ref 或表单 label 定位。submit=true 会填完按回车。",
        {"text": {"type": "string"},
         "ref": {"type": "string"},
         "label": {"type": "string", "description": "表单标签文字"},
         "submit": {"type": "boolean"}},
        ["text"]),
    _fn("browser_select", "选择下拉框的一个选项。",
        {"value": {"type": "string"}, "ref": {"type": "string"},
         "label": {"type": "string"}}, ["value"]),
    _fn("browser_viewport",
        "设置窗口尺寸。复现移动端问题必须先设（iPhone 常用 390×844）。",
        {"width": {"type": "integer"}, "height": {"type": "integer"}},
        ["width", "height"]),
    _fn("browser_screenshot",
        "保存整页截图。只对【视觉问题】（错位、遮挡、响应式）有用；"
        "功能问题请用 browser_errors 和断言，别靠看图。",
        {"name": {"type": "string", "description": "文件名，如 before-click"}}, []),
    _fn("browser_errors",
        "读运行证据：控制台错误、未捕获异常、失败的网络请求。"
        "页面行为不对时这里往往直接给出原因。",
        {}, []),
    _fn("run_scenario",
        "跑一个结构化复现场景：固定的步骤 + 明确的断言。修改前后必须跑同一份，"
        "这样『修好了』才是可比较的事实。"
        'JSON 格式：{"name":"...","viewport":{"width":390,"height":844},'
        '"steps":[{"action":"open","url":"/x"},{"action":"click","role":"button","name":"立即预订"}],'
        '"assertions":[{"type":"url","value":"/orders/new"},{"type":"no_console_errors"}]}。'
        "action 可选 open/click/fill/select/reload/viewport/screenshot；"
        "assertion 可选 url/url_not/text_present/text_absent/element_exists/element_absent/"
        "element_enabled/no_console_errors/no_failed_requests。",
        {"scenario_json": {"type": "string"}}, ["scenario_json"]),
]

TOOLS = [*CORE_TOOLS, *RUNTIME_TOOLS, *BROWSER_TOOLS]
TOOLS_BY_NAME = {t["function"]["name"]: t for t in TOOLS}

TOOL_GROUPS: dict[str, tuple[str, ...]] = {
    "core": tuple(t["function"]["name"] for t in CORE_TOOLS),
    "runtime": tuple(t["function"]["name"] for t in RUNTIME_TOOLS),
    "browser": tuple(t["function"]["name"] for t in BROWSER_TOOLS),
}


def _group_of(name: str) -> str:
    return next((g for g, names in TOOL_GROUPS.items() if name in names), "未知")
