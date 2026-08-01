"""命令行入口。薄壳：解析参数、拼出 issue 文本、把一切交给 orchestrator。

用法示例：
    repo-pilot detect --repo ../targets/tinydb              # 免费：看探测结果
    repo-pilot doctor --repo ../targets/my-next-app         # 免费：环境体检
    repo-pilot solve  --repo ../targets/tinydb --issue-file examples/tinydb_issue.md
    repo-pilot solve  --repo ../targets/tinydb --issue "描述文字" --plan-only
    repo-pilot solve  --repo owner/name --issue-gh owner/name#37   # 需要 gh CLI

    # 需要页面渲染才能验证的问题：起服务 + 浏览器
    repo-pilot solve  --repo ../targets/shop --issue-file issue.md --with-runtime
    repo-pilot solve  --repo ../targets/shop --issue-file issue.md \\
                      --scenario examples/scenario_mobile_booking.json
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
    p_solve.add_argument("--with-runtime", action="store_true",
                         help="开启运行时验证：启动应用 + 浏览器工具（处理必须渲染才能发现的问题）")
    p_solve.add_argument("--scenario",
                         help="结构化复现脚本（JSON）。修改前后跑同一份，隐含开启 --with-runtime")
    p_solve.add_argument("--ignore-env", action="store_true",
                         help="环境体检不通过也强行继续（不建议：会分不清代码问题和环境问题）")
    p_solve.add_argument("--review", action="store_true",
                         help="开启独立审查（合并闸门）。给'补丁将被自动合并、无人复查'"
                              "的场景用：实测它不增加修复数,但它说'修好了'从没错过。"
                              "人类反正要看补丁的话,这道闸是重复劳动,默认关")

    p_detect = sub.add_parser("detect", help="只探测项目类型和测试命令（不花一分钱）")
    p_detect.add_argument("--repo", required=True)
    p_detect.add_argument("--test-cmd", help="看看覆盖测试命令之后的画像")

    p_doctor = sub.add_parser("doctor", help="环境体检：依赖装了吗、端口空着吗（不花一分钱）")
    p_doctor.add_argument("--repo", required=True)
    p_doctor.add_argument("--test-cmd")
    p_doctor.add_argument("--with-runtime", action="store_true",
                         help="连浏览器依赖一起检查")

    args = parser.parse_args()

    if args.command == "detect":
        from .workspace import Workspace
        ws = Workspace.prepare(args.repo, CLONES_DIR)
        print(detect(ws.root, args.test_cmd).describe())
        return

    if args.command == "doctor":
        from .doctor import diagnose
        from .workspace import Workspace
        ws = Workspace.prepare(args.repo, CLONES_DIR)
        profile = detect(ws.root, args.test_cmd)
        print(profile.describe())
        print()
        report = diagnose(ws, profile, want_browser=args.with_runtime)
        print(report.render())
        sys.exit(0 if report.ok else 1)

    # ---- solve ----
    if args.issue:
        issue = args.issue
    elif args.issue_file:
        issue = Path(args.issue_file).read_text(encoding="utf-8")
    else:
        from .github import fetch_issue
        issue = fetch_issue(args.issue_gh)

    from .budget import Budget
    from .config import MAX_FIX_ATTEMPTS, MAX_MODIFIED_FILES
    from .orchestrator import solve
    # --review 开合并闸门。其余字段与 solve() 内部的默认预算保持一致 ——
    # 这个 flag 只该改一件事。
    budget = (Budget(max_modified_files=MAX_MODIFIED_FILES,
                     max_fix_attempts=MAX_FIX_ATTEMPTS, allow_reviewer=True)
              if args.review else None)
    try:
        code = solve(
            args.repo, issue,
            test_cmd=args.test_cmd, yes=args.yes, plan_only=args.plan_only,
            max_attempts=args.max_attempts or MAX_FIX_ATTEMPTS,
            with_runtime=args.with_runtime, scenario_path=args.scenario,
            ignore_env=args.ignore_env, budget=budget,
        )
    except (RuntimeError, ValueError) as e:
        print(f"错误：{e}", file=sys.stderr)
        code = 2
    sys.exit(code)
