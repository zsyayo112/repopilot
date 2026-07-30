"""环境体检：区分"代码有问题"和"环境没装好"。

【为什么这值得一个独立模块】
这两种失败长得很像，但处理方式完全相反：

    代码有问题 → 让 agent 去改代码（它擅长）
    环境没装好 → 让【人】去装环境（agent 改一万遍代码也修不好）

不做区分的后果在评测里非常具体：8 条实例里 5 条其实是环境伪影（Python 3.12
跑不动 2022 年的测试套件），而 agent 对着它们疯狂重试 —— 失败的那几条花了
54~120 次工具调用、最长 901 秒；真正修对的三条只花了 8/15/25 次、都在 64 秒内。
**均值差 5.3 倍。烧掉的每一分钱都花在一个它不可能修好的问题上。**

所以在动手之前先体检一遍，缺什么直接告诉人 —— 这是最便宜的止损。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .config import GREEN, RED, RESET, YELLOW


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""
    hint: str = ""
    optional: bool = False

    def render(self, color: bool = True) -> str:
        if self.ok:
            mark, c = "✓", GREEN
        elif self.optional:
            mark, c = "○", YELLOW
        else:
            mark, c = "✗", RED
        head = f"  {mark} {self.name:<16} {self.detail}"
        if color:
            head = f"{c}{head}{RESET}"
        if not self.ok and self.hint:
            head += f"\n      → {self.hint}"
        return head


@dataclass
class DoctorReport:
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """只有【必需项】全过才算通过。可选项缺失只是能力打折，不阻塞。"""
        return all(c.ok for c in self.checks if not c.optional)

    def blockers(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.ok and not c.optional]

    def render(self, color: bool = True) -> str:
        lines = ["环境体检："] + [c.render(color) for c in self.checks]
        bad = self.blockers()
        if bad:
            lines.append(f"\n有 {len(bad)} 项必需依赖缺失 —— "
                         "先把它们装好，否则测试失败可能只是环境问题，不是代码问题。")
        return "\n".join(lines)


def diagnose(ws, profile, *, want_browser: bool = False) -> DoctorReport:
    report = DoctorReport()

    # 1) git —— 这是"改砸了能回滚"的唯一保障
    report.checks.append(CheckResult(
        "git 仓库", (Path(ws.root) / ".git").exists(),
        str(ws.root), "目标必须在 git 管理下：cd 进去跑 git init"))

    # 2) adapter 认出项目了吗
    report.checks.append(CheckResult(
        "项目识别", profile.test_cmd is not None,
        f"{profile.kind}：{profile.test_cmd or '未识别到测试命令'}",
        "用 --test-cmd \"<你的测试命令>\" 手动指定"))

    # 3) 技术栈自己声明的依赖
    for req in profile.env_requirements():
        ok, detail = req.check()
        report.checks.append(CheckResult(req.name, ok, detail, req.hint, req.optional))

    # 4) 要跑应用就得有启动方式
    services = profile.services()
    if services:
        report.checks.append(CheckResult(
            "可启动服务", True,
            "，".join(f"{s.name}@{s.port}" for s in services), optional=True))
        for svc in services:
            if svc.port:
                from .runtime import port_in_use
                busy = port_in_use(svc.port)
                report.checks.append(CheckResult(
                    f"端口 {svc.port}", not busy,
                    "已被占用" if busy else "空闲",
                    f"先关掉占用 {svc.port} 的进程，否则健康检查会连到别人的服务上"))

    # 5) 浏览器（只在要用的时候检查）。
    #    标 optional：没有它照样能起服务、跑 E2E、跑测试 —— 只是少了手动
    #    操作页面的能力。**能力打折不等于任务不能开始**，别拿它当拦路石。
    if want_browser:
        try:
            import playwright  # noqa: F401
            ok, detail, hint = True, "已安装", ""
        except ImportError:
            ok, detail = False, "未安装（浏览器工具会被关闭，其余功能不受影响）"
            hint = 'pip install -e ".[browser]" && playwright install chromium'
        report.checks.append(CheckResult("playwright", ok, detail, hint, optional=True))

    # 6) monorepo 提示：不是问题，但影响"该跑哪个测试"
    repo = profile.repository
    if repo is not None and repo.is_monorepo:
        report.checks.append(CheckResult(
            "monorepo", True,
            f"{len(repo.units)} 个项目单元（改哪块跑哪块）", optional=True))
    return report
