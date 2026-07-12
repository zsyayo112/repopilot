"""Tool Runtime：agent 对目标仓库能做的所有动作。

与 agent/tools.py 一脉相承，三点进化：

  1) 沙盒根不再是写死的 playground，而是运行时传入的仓库根
     → 模块级函数升级成 ToolKit 类（一个仓库一个实例）。
  2) 新增真实仓库必需的三把刀：
     search_code   ripgrep 检索 —— 仓库读不完，必须先搜再读
     list_symbols  ast 符号清单 —— 千行大文件先看骨架再读细节
     edit_file     精确片段替换 —— 整文件覆盖在大文件上必翻车
  3) run_tests 单列成工具：测试命令来自 adapter，模型不用（也不许）自己猜。

职责分离不变：工具只知道"怎么做"，"该不该做"归 permissions，在 executor 里串。
"""

import ast
import re
import shutil
import subprocess

from .adapters import RepoProfile
from .config import CMD_TIMEOUT, MAX_TOOL_OUTPUT
from .policy import check_command, jail
from .workspace import Workspace
from . import verifier


class ToolKit:
    def __init__(self, ws: Workspace, profile: RepoProfile | None = None):
        self.ws = ws
        self.root = ws.root
        self.profile = profile
        self._impl = {
            "read_file": self.read_file,
            "list_files": self.list_files,
            "search_code": self.search_code,
            "list_symbols": self.list_symbols,
            "edit_file": self.edit_file,
            "write_file": self.write_file,
            "run_bash": self.run_bash,
            "run_tests": self.run_tests,
            "git_diff": self.git_diff,
        }

    # ------------------------------------------------------------------ 只读
    def read_file(self, path: str, start_line: int = 0, end_line: int = 0) -> str:
        """支持行号范围：真实仓库里的大文件不该整读（上下文预算！）。"""
        text = jail(path, self.root).read_text(encoding="utf-8")
        if start_line or end_line:
            lines = text.splitlines()
            start = max(start_line - 1, 0)
            end = end_line or len(lines)
            text = "\n".join(lines[start:end])
        return text

    def list_files(self, directory: str = ".") -> str:
        d = jail(directory, self.root)
        if not d.exists():
            return f"目录不存在：{directory}"
        names = [f"{p.name}/" if p.is_dir() else p.name
                 for p in sorted(d.iterdir()) if p.name != ".git"]
        return "\n".join(names) or "(空目录)"

    def search_code(self, pattern: str, glob: str = "") -> str:
        """ripgrep 优先，没有就退化成 grep。这是真实仓库的第一入口：先搜再读。"""
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
        """用 Python 自带的 ast 列出类/函数骨架 —— 这就是"符号索引"的朴素版。
        （TS 版要靠 ts-morph，那是 Phase 5 的课题；先用 30 行代码拿到 80% 价值。）"""
        p = jail(path, self.root)
        if p.suffix != ".py":
            return "目前只支持 .py 文件的符号提取"
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError as e:
            return f"语法错误，无法解析：{e}"
        rows = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                rows.append((node.lineno, f"class {node.name}", node.col_offset))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                sig = ", ".join(a.arg for a in node.args.args)
                rows.append((node.lineno, f"def {node.name}({sig})", node.col_offset))
        rows.sort()
        return "\n".join(f"L{ln:<5}{' ' * col}{sig}" for ln, sig, col in rows) \
            or "(没有类或函数定义)"

    def git_diff(self) -> str:
        stat, diff = self.ws.diff_stat(), self.ws.diff()
        return f"{stat}\n\n{diff}" if diff else "(暂无改动)"

    # ------------------------------------------------------------------ 写
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

    # ------------------------------------------------------------------ 执行
    def run_bash(self, command: str) -> str:
        ok, reason = check_command(command)
        if not ok:
            return f"错误：{reason}"
        proc = subprocess.run(command, shell=True, cwd=self.root,
                              capture_output=True, text=True, timeout=CMD_TIMEOUT)
        output = (proc.stdout + proc.stderr).strip()
        return f"exit_code={proc.returncode}\n{output or '(无输出)'}"

    def run_tests(self) -> str:
        """跑 adapter 给定的测试命令。模型不许自己猜测试命令 —— 猜错一次烧一轮 token。"""
        if self.profile is None or not self.profile.test_cmd:
            return "错误：没有可用的测试命令（adapter 未识别，需要 --test-cmd）"
        report = verifier.run_tests(self.ws, self.profile)
        return report.render()

    # ------------------------------------------------------------------ 分发
    def execute(self, name: str, args: dict) -> str:
        """永远返回字符串、永远不抛异常、超长自动截断（原样继承 agent/tools.py）。"""
        fn = self._impl.get(name)
        if fn is None:
            return f"错误：不存在名为 {name} 的工具"
        try:
            result = str(fn(**args))
        except subprocess.TimeoutExpired:
            return "错误：命令执行超时"
        except Exception as e:
            return f"错误：{type(e).__name__}: {e}"
        if len(result) > MAX_TOOL_OUTPUT:
            omitted = len(result) - MAX_TOOL_OUTPUT
            result = result[:MAX_TOOL_OUTPUT] + f"\n\n[输出被截断，省略了 {omitted} 个字符]"
        return result


# 只读（含跑测试）自动放行；会改动世界的要过权限闸门
SAFE_TOOLS = {"read_file", "list_files", "search_code", "list_symbols",
              "git_diff", "run_tests"}


def _fn(name: str, desc: str, props: dict, required: list[str]) -> dict:
    """少写点样板：把一条工具说明书折叠成一行调用。"""
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required},
    }}


TOOLS = [
    _fn("read_file",
        "读取文件内容。大文件请配合 start_line/end_line 分段读，别整读。路径相对仓库根。",
        {"path": {"type": "string"},
         "start_line": {"type": "integer", "description": "起始行号（含，从 1 开始）"},
         "end_line": {"type": "integer", "description": "结束行号（含）"}},
        ["path"]),
    _fn("list_files", "列出目录内容，子目录名后带 /。",
        {"directory": {"type": "string"}}, []),
    _fn("search_code",
        "在整个仓库里用正则搜索代码，返回 文件:行号:内容。这是定位代码的首选工具，"
        "先搜索缩小范围，再 read_file 细看。",
        {"pattern": {"type": "string", "description": "正则表达式"},
         "glob": {"type": "string", "description": "可选的文件过滤，如 *.py"}},
        ["pattern"]),
    _fn("list_symbols",
        "列出一个 .py 文件里所有类和函数的名字与行号。读大文件前先用它看骨架。",
        {"path": {"type": "string"}}, ["path"]),
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
        "运行本项目的测试套件（命令由系统配置，你不用关心）。每次实质修改后都应调用。",
        {}, []),
    _fn("git_diff", "查看当前所有未提交的改动。收尾前用它自查改动是否最小、有无误伤。",
        {}, []),
]
