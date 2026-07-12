"""Repository Adapter：整个代码库里【唯一允许认识具体技术栈】的地方。

"核心框架无关"落到实处就是这个文件：核心 agent 只会问 adapter 两个问题 ——
  1) 这是什么类型的项目？（kind）
  2) 用什么命令跑测试？（test_cmd）
核心把 kind 只【记录】不【分叉】，把 test_cmd 当【不透明字符串】照着跑——
所以支持一种新技术栈 = 在这里写一个 _detect_xxx 并登记进 _DETECTORS，核心一行不用改。

结构：detect() 按优先级依次尝试 _DETECTORS 里的每个 detector，第一个认领（返回非
None）的胜出；全不认领则 unknown。语言专属的标志文件优先，Makefile 兜底放最后。

支持矩阵（深度不一，探测都是零成本的标志文件检查，不读代码、不调模型）：
  python / rust / go / java-maven / java-gradle / ruby / node / nestjs / make
探测不准或不支持时，永远可以用 `--test-cmd` 强制指定，一票覆盖所有探测。
"""

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# pytest 参数说明：
#   -q         精简输出（省 token）
#   -ra        失败用例在结尾集中列出（verifier 靠解析 "FAILED xxx" 行拿失败名单）
#   --color=no 去掉 ANSI 颜色码，方便解析
#
# 用 sys.executable 而不是裸 "python"：子进程继承的 PATH 里未必有 python
# （WSL 常见只有 python3），而 sys.executable 永远指向当前 venv 的解释器。
# 【MVP 假设】目标仓库的依赖装在运行 RepoPilot 的同一个 venv 里
# （pip install -e targets/xxx）。每个目标仓库独立 venv 是 Phase 4 的课题。
PYTEST_CMD = f"{sys.executable} -m pytest -q -ra --color=no"


@dataclass
class RepoProfile:
    kind: str                 # python / rust / go / java-maven / ... / unknown
    test_cmd: str | None      # 跑测试的命令；None 表示识别失败
    notes: list[str] = field(default_factory=list)

    def describe(self) -> str:
        lines = [f"项目类型：{self.kind}", f"测试命令：{self.test_cmd or '（未识别）'}"]
        lines += [f"  - {n}" for n in self.notes]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# detector 们：每个认领一种技术栈，认不出就返回 None。加一种语言 = 加一个这样的函数。
# 每个函数只做两件事：看标志文件在不在、给出该技术栈的标准测试命令。
# ---------------------------------------------------------------------------
def _detect_python(root: Path) -> RepoProfile | None:
    markers = [f for f in ("pyproject.toml", "setup.py", "setup.cfg",
                           "pytest.ini", "tox.ini") if (root / f).exists()]
    if not markers:
        return None
    notes = [f"发现 {', '.join(markers)}"]
    if (root / "tests").is_dir():
        notes.append("发现 tests/ 目录")
    return RepoProfile("python", PYTEST_CMD, notes)


def _detect_rust(root: Path) -> RepoProfile | None:
    if (root / "Cargo.toml").exists():
        return RepoProfile("rust", "cargo test", ["发现 Cargo.toml"])
    return None


def _detect_go(root: Path) -> RepoProfile | None:
    if (root / "go.mod").exists():
        return RepoProfile("go", "go test ./...", ["发现 go.mod"])
    return None


def _detect_maven(root: Path) -> RepoProfile | None:
    if (root / "pom.xml").exists():
        return RepoProfile("java-maven", "mvn -q -B test", ["发现 pom.xml"])
    return None


def _detect_gradle(root: Path) -> RepoProfile | None:
    if (root / "build.gradle").exists() or (root / "build.gradle.kts").exists():
        # 优先用项目自带的 wrapper（版本可复现），没有再退回全局 gradle
        cmd = "./gradlew test" if (root / "gradlew").exists() else "gradle test"
        return RepoProfile("java-gradle", cmd, ["发现 build.gradle"])
    return None


def _detect_ruby(root: Path) -> RepoProfile | None:
    if (root / ".rspec").exists() or (root / "spec").is_dir():
        return RepoProfile("ruby", "bundle exec rspec",
                           ["发现 .rspec/spec，Ruby 测试命令差异大，必要时用 --test-cmd 覆盖"])
    if (root / "Gemfile").exists():
        return RepoProfile("ruby", "bundle exec rake test",
                           ["发现 Gemfile，Ruby 测试命令差异大，必要时用 --test-cmd 覆盖"])
    return None


def _detect_node(root: Path) -> RepoProfile | None:
    """Node/NestJS 需要解析 package.json，比其他语言多一点逻辑。"""
    pkg = root / "package.json"
    if not pkg.exists():
        return None
    try:
        data = json.loads(pkg.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        data = {}
    deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
    kind = "nestjs" if "@nestjs/core" in deps else "node"
    test_cmd = "npm test --silent" if "test" in data.get("scripts", {}) else None
    notes = [f"发现 package.json（{kind}）"]
    if test_cmd is None:
        notes.append("package.json 里没有 test 脚本，请用 --test-cmd 指定")
    return RepoProfile(kind, test_cmd, notes)


def _detect_make(root: Path) -> RepoProfile | None:
    """兜底：没有语言专属标志、但 Makefile 里有 test 目标，就用 make test。"""
    mk = root / "Makefile"
    if not mk.exists():
        return None
    try:
        text = mk.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    if re.search(r"(?m)^test:", text):
        return RepoProfile("make", "make test", ["发现 Makefile 里的 test 目标"])
    return None


# 顺序即优先级：语言专属标志优先，Makefile 兜底垫底。加新语言就往这里加一项。
_DETECTORS = [
    _detect_python, _detect_rust, _detect_go, _detect_maven,
    _detect_gradle, _detect_ruby, _detect_node, _detect_make,
]


def detect(root: str | Path, test_cmd_override: str | None = None) -> RepoProfile:
    root = Path(root)

    if test_cmd_override:
        return RepoProfile("custom", test_cmd_override, ["测试命令来自 --test-cmd 参数"])

    for detector in _DETECTORS:
        profile = detector(root)
        if profile is not None:
            return profile

    return RepoProfile("unknown", None, ["无法识别项目类型，请用 --test-cmd 指定测试命令"])
