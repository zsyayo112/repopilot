"""命令行入口。薄壳：解析参数、拼出 issue 文本、把一切交给 orchestrator。

用法示例：
    repo-pilot detect --repo ../targets/tinydb
    repo-pilot solve  --repo ../targets/tinydb --issue-file examples/tinydb_issue.md
    repo-pilot solve  --repo ../targets/tinydb --issue "描述文字" --plan-only
    repo-pilot solve  --repo owner/name --issue-gh owner/name#37   # 需要 gh CLI
"""

import argparse
import sys
from pathlib import Path

from .adapters import detect
from .config import CLONES_DIR


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="repo-pilot",
        description="RepoPilot：面向真实代码仓库的 issue resolution agent（手写、无框架）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_solve = sub.add_parser("solve", help="解决一个 issue：计划→修改→验证→审查")
    p_solve.add_argument("--repo", required=True,
                         help="本地仓库路径，或 owner/name（自动克隆到 targets/）")
    group = p_solve.add_mutually_exclusive_group(required=True)
    group.add_argument("--issue", help="issue 文本")
    group.add_argument("--issue-file", help="issue 文本文件路径")
    group.add_argument("--issue-gh", help="GitHub issue 引用，如 owner/repo#37（需要 gh CLI）")
    p_solve.add_argument("--test-cmd", help="覆盖自动探测的测试命令")
    p_solve.add_argument("--yes", action="store_true",
                         help="免确认模式（评测/演示用，交互学习期不建议）")
    p_solve.add_argument("--plan-only", action="store_true",
                         help="只出计划不动手（便宜地检查 Planner 质量）")
    p_solve.add_argument("--max-attempts", type=int, default=None,
                         help="测试失败后的最大重试轮数")

    p_detect = sub.add_parser("detect", help="只探测项目类型和测试命令（不花一分钱）")
    p_detect.add_argument("--repo", required=True)

    args = parser.parse_args()

    if args.command == "detect":
        from .workspace import Workspace
        ws = Workspace.prepare(args.repo, CLONES_DIR)
        print(detect(ws.root).describe())
        return

    # ---- solve ----
    if args.issue:
        issue = args.issue
    elif args.issue_file:
        issue = Path(args.issue_file).read_text(encoding="utf-8")
    else:
        from .github import fetch_issue
        issue = fetch_issue(args.issue_gh)

    from .config import MAX_FIX_ATTEMPTS
    from .orchestrator import solve
    try:
        code = solve(
            args.repo, issue,
            test_cmd=args.test_cmd, yes=args.yes, plan_only=args.plan_only,
            max_attempts=args.max_attempts or MAX_FIX_ATTEMPTS,
        )
    except (RuntimeError, ValueError) as e:
        print(f"错误：{e}", file=sys.stderr)
        code = 2
    sys.exit(code)
