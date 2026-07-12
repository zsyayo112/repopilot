"""安全策略：所有硬约束集中在这里，一眼看全。

三道闸：
  1) jail()          所有文件路径必须落在仓库内；.git 禁止写入
  2) check_command() 危险命令黑名单
  3) MAX_MODIFIED_FILES（在 orchestrator 里检查）改动规模上限

黑名单防的是【事故】，不是防高手 —— 真正的强隔离要靠 Docker 沙箱，
那是 Phase 5 的独立课题。MVP 的现实防线是：黑名单 + git 可回滚 + 权限确认。
"""

from pathlib import Path

DANGEROUS = [
    "rm -rf /", "sudo ", "mkfs", "dd if=", ":(){", "> /dev/", "chmod 777 /",
    # 网络与外发：MVP 阶段 agent 不需要装包/上网，出现就是跑偏了
    "curl ", "wget ", "pip install", "npm install",
    # 提交与推送是【人】的决定：agent 只负责产出 diff，最后一步永远留给用户
    "git commit", "git push", "git reset",
]


def check_command(command: str) -> tuple[bool, str]:
    """返回 (是否放行, 拒绝理由)。"""
    for pat in DANGEROUS:
        if pat in command:
            return False, f"命令被安全策略拒绝（匹配到 {pat!r}）"
    return True, ""


def jail(path: str, root: Path, writing: bool = False) -> Path:
    """把模型给的路径落进仓库内，挡住越狱。

    继承自 agent/tools.py 的 _resolve()，两处进化：
      - 剥"仓库名前缀"改成【先信字面路径，剥完不存在才剥】：
        agent/tools.py 原版无条件剥前缀，这在 playground 沙盒里成立
        （沙盒不可能有同名子目录），但真实仓库大量 Python 项目
        顶层包目录名恰好等于仓库名（tinydb/tinydb、django/django），
        无脑剥掉会把合法路径剥成不存在的路径。改成"存在性验证优先"：
        字面路径本来就存在 → 直接用；只有字面路径不存在、且剥掉前缀后
        的路径存在，才认定是模型画蛇添足加的前缀。
      - 新增：写操作禁止碰 .git（改坏引用整个仓库就废了）
    """
    path = path.strip().lstrip("/")

    p = (root / path).resolve()
    prefix = f"{root.name}/"
    if not p.exists() and path.startswith(prefix):
        stripped = (root / path[len(prefix):]).resolve()
        if stripped.exists():
            p = stripped

    if not p.is_relative_to(root):
        raise PermissionError(f"拒绝访问仓库之外的路径：{path}")

    rel = p.relative_to(root)
    if writing and rel.parts and rel.parts[0] == ".git":
        raise PermissionError("拒绝写入 .git 目录")
    return p
