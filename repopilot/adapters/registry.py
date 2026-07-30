"""探测登记处：把"这是什么项目"这件事收敛到一个地方。

两个入口，回答两种不同的问题：

    detect(root)  →  RepoProfile      "这个仓库主要是什么项目？"（核心一直在用的）
    scan(root)    →  RepositoryProfile "这个仓库里【一共】有几个项目？"（monorepo）

【为什么需要 scan】
旧版的假设是"一个仓库一种技术栈，找到第一个就停"。真实项目经常长这样：

    shop/
    ├── web/       Next.js
    ├── api/       NestJS
    ├── worker/    Python
    └── search/    Go

这时候"找到第一个就停"会做出一个错误的承诺：拿 web 的测试命令去验证
worker 的改动。scan() 把仓库看成【多个项目单元】，再由 unit_for(改动的文件)
决定该跑谁的测试 —— 改哪块跑哪块，最后才跑全仓库回归。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

from .base import (
    CustomAdapter,
    EnvRequirement,
    RepoAdapter,
    ServiceSpec,
    Symbol,
    TestReport,
    ValidationStep,
    tail,
)
from .go_stack import GoAdapter
from .java_stack import GradleAdapter, MavenAdapter
from .make_stack import MakeAdapter
from .node_stack import NodeAdapter
from .python_stack import PythonAdapter
from .ruby_stack import RubyAdapter
from .rust_stack import RustAdapter

# 顺序即优先级：语言专属标志优先，Makefile 兜底垫底。
# 加一门新语言 = 在这里加一项（外加一个 *_stack.py），核心一行不改。
ADAPTERS: list[type[RepoAdapter]] = [
    PythonAdapter,
    RustAdapter,
    GoAdapter,
    MavenAdapter,
    GradleAdapter,
    RubyAdapter,
    NodeAdapter,
    MakeAdapter,
]

# 扫 monorepo 时绝对不进的目录：又大又不可能是"项目单元"
_SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".pytest_cache",
    ".ruff_cache", ".mypy_cache", "venv", ".venv", "env", "dist", "build",
    "target", "out", ".next", ".nuxt", "coverage", "vendor", ".idea", ".vscode",
    "site-packages", ".tox", ".gradle", "bin", "obj",
}

# 这些标志文件说明"这个目录是个工作区管理者，子目录里还有真项目"
_WORKSPACE_MARKERS = ("pnpm-workspace.yaml", "go.work", "lerna.json", "turbo.json",
                      "nx.json", "rush.json")


@dataclass
class ProjectUnit:
    """仓库里的一个可独立测试的项目。"""
    path: str                  # 相对仓库根，根本身是 "."
    adapter: RepoAdapter

    @property
    def kind(self) -> str:
        return self.adapter.kind

    @property
    def test_cmd(self) -> str | None:
        return self.adapter.test_command()

    @property
    def depth(self) -> int:
        return 0 if self.path == "." else self.path.count("/") + 1

    def render(self) -> str:
        return f"{self.path:<24} {self.kind:<12} {self.test_cmd or '(无测试命令)'}"


@dataclass
class RepositoryProfile:
    """整个仓库的画像：可能有一个单元，也可能有十个。"""
    root: Path
    units: list[ProjectUnit] = field(default_factory=list)

    @property
    def is_monorepo(self) -> bool:
        return len(self.units) > 1

    def primary(self) -> ProjectUnit | None:
        """主单元：优先根目录、优先能跑测试的。核心在"没告诉我改了哪个模块"时用它。"""
        runnable = [u for u in self.units if u.test_cmd]
        for pool in (runnable, self.units):
            for u in pool:
                if u.path == ".":
                    return u
            if pool:
                return min(pool, key=lambda u: (u.depth, u.path))
        return None

    def unit_for(self, rel_path: str) -> ProjectUnit | None:
        """一个文件属于哪个单元 —— 取【最长前缀匹配】。

        为什么是最长：`web/components/Button.tsx` 同时落在 "." 和 "web" 名下，
        显然 web 更精确。这就是"改哪块跑哪块"的实现。
        """
        rel = rel_path.replace("\\", "/").lstrip("./")
        best: ProjectUnit | None = None
        for u in self.units:
            if u.path == ".":
                if best is None:
                    best = u
                continue
            if rel == u.path or rel.startswith(u.path + "/"):
                if best is None or u.depth > best.depth:
                    best = u
        return best

    def units_for(self, rel_paths: list[str]) -> list[ProjectUnit]:
        """一批改动涉及哪些单元（去重，保持稳定顺序）。"""
        seen: dict[str, ProjectUnit] = {}
        for p in rel_paths:
            u = self.unit_for(p)
            if u is not None:
                seen.setdefault(u.path, u)
        return sorted(seen.values(), key=lambda u: (u.depth, u.path))

    def render(self) -> str:
        if not self.units:
            return "（没有识别到任何项目单元）"
        head = f"识别到 {len(self.units)} 个项目单元" + ("（monorepo）" if self.is_monorepo else "")
        return head + "\n" + "\n".join("  " + u.render() for u in self.units)


@dataclass
class RepoProfile:
    """核心手里那份"项目说明书"。

    核心只读 kind / test_cmd / describe()，**永远不对 kind 做分支判断**；
    需要具体能力时一律走下面的委托方法 —— 委托给谁由探测决定，核心不关心。
    """
    kind: str
    test_cmd: str | None
    notes: list[str] = field(default_factory=list)
    adapter: RepoAdapter | None = None
    root: Path | None = None
    repository: RepositoryProfile | None = None

    @property
    def command_overridden(self) -> bool:
        """测试命令是不是人用 --test-cmd 明确指定的。

        为什么要专门开一个属性，而不是在调用处写 `kind == "custom"`：
        核心一旦开始拿 kind 做字符串比较，"核心不认识任何技术栈"这条保证就出现了
        第一道裂缝 —— 而且是那种会被后人模仿的裂缝。
        `grep -rn 'kind == "' repopilot/orchestrator.py` 必须保持为空。
        """
        return isinstance(self.adapter, CustomAdapter)

    # -- 给人看 -------------------------------------------------------------
    def describe(self) -> str:
        lines = [f"项目类型：{self.kind}", f"测试命令：{self.test_cmd or '（未识别）'}"]
        lines += [f"  - {n}" for n in self.notes]

        steps = self.validation_steps()
        if len(steps) > 1:
            lines.append("验证流水线：" + " → ".join(
                f"{s.name}{'' if s.required else '(可选)'}" for s in steps))
        if svc := self.services():
            lines.append("可启动服务：" + "，".join(
                f"{s.name}@{s.port or '?'}（{s.command}）" for s in svc))
        if e2e := self.e2e_command():
            lines.append(f"E2E 命令：{e2e}")
        if self.repository and self.repository.is_monorepo:
            lines.append(self.repository.render())
        return "\n".join(lines)

    # -- 委托给 adapter（adapter 为 None 时退化成"只信 exit_code"）-------------
    def parse_test_output(self, output: str, exit_code: int) -> TestReport:
        if self.adapter is not None:
            return self.adapter.parse_test_output(output, exit_code)
        return TestReport(exit_code=exit_code, tail=tail(output), parsed=False)

    def validation_steps(self) -> list[ValidationStep]:
        if self.adapter is not None:
            return self.adapter.validation_steps()
        return [ValidationStep("test", self.test_cmd)] if self.test_cmd else []

    def services(self) -> list[ServiceSpec]:
        """收集【整个仓库】能起的服务，不只是主项目的。

        为什么必须跨单元收集：monorepo 里能跑起来的那个 app 往往【不是】主单元。
        `shop/` 根目录只是个 workspace 管理者，真正的 Next 应用在 `apps/web/`。
        只问主单元，会得出"这个项目没法启动"—— 而它明明有前端。
        """
        specs: list[ServiceSpec] = []
        used_names: set[str] = set()
        used_ports: set[int] = set()

        for unit_path, adapter in self._service_sources():
            for spec in adapter.services():
                specs.append(_relocate(spec, unit_path, used_names, used_ports))
        return specs

    def _service_sources(self):
        """(单元路径, adapter) 列表。没有 monorepo 时就是主 adapter 一个。"""
        repo = self.repository
        if repo is None or not repo.units:
            return [(".", self.adapter)] if self.adapter else []
        sources = [(u.path, u.adapter) for u in repo.units]
        # --test-cmd 覆盖时 self.adapter 是 CustomAdapter，它包着主单元；
        # 主单元已经在 units 里了，不用重复加。
        return sources

    def e2e_command(self) -> str | None:
        """整个仓库里第一条可用的 E2E 命令（同样要跨单元找）。"""
        for unit_path, adapter in self._service_sources():
            cmd = adapter.e2e_command()
            if cmd:
                # 命令要在那个单元的目录里跑，所以带上 cd（这是给人看的字符串，
                # 真正执行时 tools.run_e2e 在仓库根跑，因此这里必须显式 cd）
                return cmd if unit_path == "." else f"cd {unit_path} && {cmd}"
        return None

    def env_requirements(self) -> list[EnvRequirement]:
        return self.adapter.env_requirements() if self.adapter else []

    def symbols(self, path: Path) -> list[Symbol] | None:
        from .symbols import extract
        return extract(path)


# ---------------------------------------------------------------------------
# 探测入口
# ---------------------------------------------------------------------------
def detect_one(root: Path) -> RepoAdapter | None:
    """对【单个目录】跑一遍探测链，第一个认领的胜出。零成本：只看标志文件。"""
    for adapter_cls in ADAPTERS:
        try:
            adapter = adapter_cls.detect(root)
        except Exception:
            # 一个 adapter 的探测炸了不该拖垮整条链（比如 package.json 是坏的）
            adapter = None
        if adapter is not None:
            return adapter
    return None


def scan(root: str | Path, max_depth: int = 3) -> RepositoryProfile:
    """扫出仓库里所有项目单元。深度默认 3：够覆盖 packages/web、apps/api/service 这类布局。"""
    root = Path(root)
    units: list[ProjectUnit] = []

    def walk(directory: Path, depth: int) -> None:
        adapter = detect_one(directory)
        rel = "." if directory == root else directory.relative_to(root).as_posix()
        if adapter is not None:
            units.append(ProjectUnit(rel, adapter))
            # 认领了就不再往里钻 —— 除非它只是个"工作区管理者"，
            # 真项目在子目录里（pnpm workspace / go.work / lerna 这类）
            if not _is_workspace_root(directory, adapter):
                return
        if depth >= max_depth:
            return
        try:
            children = sorted(p for p in directory.iterdir() if p.is_dir())
        except OSError:
            return
        for child in children:
            if child.name in _SKIP_DIRS or child.name.startswith("."):
                continue
            walk(child, depth + 1)

    walk(root, 0)
    return RepositoryProfile(root, units)


def _relocate(spec: ServiceSpec, unit_path: str, used_names: set[str],
              used_ports: set[int]) -> ServiceSpec:
    """把子单元声明的服务搬到仓库坐标系里：改 cwd、去重名字、错开端口。

    端口错开这件事值得说一句：两个子项目都想要 3000 是常态（Next 和 Nest 的
    默认端口都是 3000）。不处理的话第二个服务启动时会撞上"端口被占用"，而占用
    它的正是我们自己刚起的第一个服务 —— 一个看起来像环境问题的自伤。
    我们同时通过 env PORT 把新端口告诉框架（主流框架都认这个变量）。
    """
    cwd = unit_path if spec.cwd == "." else f"{unit_path.rstrip('/')}/{spec.cwd}"

    name = spec.name
    if name in used_names:
        suffix = unit_path.rstrip("/").split("/")[-1] or "root"
        name = f"{name}-{suffix}"
    counter = 2
    while name in used_names:
        name, counter = f"{spec.name}-{counter}", counter + 1
    used_names.add(name)

    port = spec.port
    while port is not None and port in used_ports:
        port += 1
    if port is not None:
        used_ports.add(port)

    env = dict(spec.env)
    if port is not None:
        env["PORT"] = str(port)

    return replace(spec, name=name, cwd=cwd or ".", port=port, env=env)


def _is_workspace_root(directory: Path, adapter: RepoAdapter) -> bool:
    if any((directory / m).exists() for m in _WORKSPACE_MARKERS):
        return True
    # npm/yarn workspaces 写在 package.json 里；cargo workspace 写在 Cargo.toml 里
    pkg = getattr(adapter, "pkg", None)
    if isinstance(pkg, dict) and pkg.get("workspaces"):
        return True
    cargo = directory / "Cargo.toml"
    if adapter.kind == "rust" and cargo.exists():
        try:
            return "[workspace]" in cargo.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return False
    return False


def detect(root: str | Path, test_cmd_override: str | None = None) -> RepoProfile:
    """核心唯一调用的探测入口。签名与行为对旧版完全兼容。"""
    root = Path(root)
    repository = scan(root)
    primary = repository.primary()
    inner = primary.adapter if primary else None

    if test_cmd_override:
        # 人只是换了跑测试的方式，不是换了一门语言：解析/服务/验证能力继续复用探测结果
        adapter = CustomAdapter(root, test_cmd_override, inner)
        return RepoProfile("custom", test_cmd_override, list(adapter.notes),
                           adapter, root, repository)

    if inner is None:
        return RepoProfile("unknown", None,
                           ["无法识别项目类型，请用 --test-cmd 指定测试命令"],
                           None, root, repository)

    notes = list(inner.notes)
    if primary.path != ".":
        notes.append(f"主项目不在仓库根，而在 {primary.path}/")
    return RepoProfile(inner.kind, inner.test_command(), notes, inner, root, repository)
