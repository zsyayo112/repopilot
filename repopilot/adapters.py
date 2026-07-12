"""Repository Adapter：整个代码库里【唯一允许认识具体技术栈】的地方。

"核心框架无关"落到实处就是这个文件：核心 agent 只会问 adapter 两个问题 ——
  1) 这是什么类型的项目？
  2) 用什么命令跑测试？
支持一种新技术栈 = 在 detect() 里加一个探测分支。核心一行不用改。

第一版：Python (pytest) 深支持；Node/NestJS 浅探测（Phase 4 再深化）。
"""

import json
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
    kind: str                 # python / node / nestjs / custom / unknown
    test_cmd: str | None      # 跑测试的命令；None 表示识别失败
    notes: list[str] = field(default_factory=list)

    def describe(self) -> str:
        lines = [f"项目类型：{self.kind}", f"测试命令：{self.test_cmd or '（未识别）'}"]
        lines += [f"  - {n}" for n in self.notes]
        return "\n".join(lines)


def detect(root: str | Path, test_cmd_override: str | None = None) -> RepoProfile:
    root = Path(root)

    if test_cmd_override:
        return RepoProfile("custom", test_cmd_override, ["测试命令来自 --test-cmd 参数"])

    # ---- Python ----
    markers = [f for f in ("pyproject.toml", "setup.py", "setup.cfg",
                           "pytest.ini", "tox.ini") if (root / f).exists()]
    if markers:
        notes = [f"发现 {', '.join(markers)}"]
        if (root / "tests").is_dir():
            notes.append("发现 tests/ 目录")
        return RepoProfile("python", PYTEST_CMD, notes)

    # ---- Node / NestJS（浅支持：能认出来、能跑 npm test，仅此而已）----
    pkg = root / "package.json"
    if pkg.exists():
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

    return RepoProfile("unknown", None, ["无法识别项目类型，请用 --test-cmd 指定测试命令"])
