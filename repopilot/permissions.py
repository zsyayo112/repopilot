"""权限闸门：改动性操作执行前，先问用户。几乎原样继承 agent/permissions.py。

变化只有两处：
  - SAFE_TOOLS 从 tools.py 引入（新增的只读工具自动放行）
  - _show() 学会了展示 edit_file 的 old→new 对照
--yes 参数等价于旧版的 /trust：全自动跑，评测时用。
"""

from .config import BOLD, DIM, RED, RESET, YELLOW
from .tools import SAFE_TOOLS


class Permissions:
    def __init__(self, trust_all: bool = False):
        self.trust_all = trust_all

    def check(self, name: str, args: dict) -> tuple[bool, str]:
        if name in SAFE_TOOLS or self.trust_all:
            return True, ""

        self._show(name, args)
        try:
            choice = input("  你的选择 [y/a/n] > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            choice = "n"

        if choice == "a":
            self.trust_all = True
            return True, ""
        if choice in ("y", ""):
            return True, ""

        reason = input("  拒绝理由（可留空）> ").strip()
        return False, reason or "用户拒绝了这次操作，未说明理由。"

    @staticmethod
    def _preview(text: str, limit: int = 300) -> str:
        return text if len(text) <= limit else text[:limit] + f"\n… (共 {len(text)} 字符)"

    @classmethod
    def _show(cls, name: str, args: dict) -> None:
        print(f"\n{YELLOW}┌─ 需要你确认 ───────────────────────────{RESET}")
        print(f"{YELLOW}│{RESET} 工具：{BOLD}{name}{RESET}")
        if name == "edit_file":
            print(f"{YELLOW}│{RESET} 文件：{args.get('path')}")
            print(f"{YELLOW}│{RESET} {RED}- 旧：{RESET}")
            for line in cls._preview(args.get("old_string", "")).splitlines():
                print(f"{YELLOW}│{RESET}   {DIM}{line}{RESET}")
            print(f"{YELLOW}│{RESET} {BOLD}+ 新：{RESET}")
            for line in cls._preview(args.get("new_string", "")).splitlines():
                print(f"{YELLOW}│{RESET}   {line}")
        elif name == "write_file":
            print(f"{YELLOW}│{RESET} 新文件：{args.get('path')}")
            for line in cls._preview(args.get("content", ""), 400).splitlines():
                print(f"{YELLOW}│{RESET}   {DIM}{line}{RESET}")
        elif name == "run_bash":
            print(f"{YELLOW}│{RESET} 命令:{BOLD}{args.get('command')}{RESET}")
        else:
            print(f"{YELLOW}│{RESET} 参数：{args}")
        print(f"{YELLOW}└─ [y]同意  [a]本次运行全放行  [n]拒绝 ──────{RESET}")
