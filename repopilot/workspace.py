"""工作区：agent 与目标仓库之间的 git 层。

从 playground 到真实仓库，最重要的认知跳变就在这里 ——
【git 变成了撤销键】：
  · 开工前必须工作区干净（否则 agent 的改动和人的改动混在一起，无法回滚）
  · 收工后 git diff 就是全部改动，一目了然、可审核
  · 改砸了 reset_hard() 一键回到起点

所以 MVP 的硬性要求：目标必须是 git 仓库。这不是挑剔，是唯一的安全网。
"""

import subprocess
from pathlib import Path


class Workspace:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        if not (self.root / ".git").exists():
            raise ValueError(
                f"{self.root} 不是 git 仓库。MVP 要求目标在 git 管理下 —— "
                "这是改砸后能回滚的唯一保障。"
            )

    # -- 构造 ---------------------------------------------------------------
    @classmethod
    def prepare(cls, repo: str, clones_dir: Path) -> "Workspace":
        """repo 是本地路径就直接用；是 URL 或 owner/name 就克隆下来。"""
        p = Path(repo).expanduser()
        if p.exists():
            return cls(p)

        url = repo if repo.startswith(("http://", "https://", "git@")) \
            else f"https://github.com/{repo}.git"
        name = repo.rstrip("/").split("/")[-1].removesuffix(".git")
        dest = clones_dir / name
        if not dest.exists():
            clones_dir.mkdir(parents=True, exist_ok=True)
            print(f"正在克隆 {url} → {dest} …")
            subprocess.run(["git", "clone", url, str(dest)], check=True)
        return cls(dest)

    # -- git 封装 -----------------------------------------------------------
    def _git(self, *args: str) -> str:
        proc = subprocess.run(
            ["git", *args], cwd=self.root, capture_output=True, text=True
        )
        return (proc.stdout + proc.stderr).strip()

    def ensure_clean(self) -> None:
        """开工前的门禁：工作区必须干净，否则拒绝启动。"""
        dirty = self._git("status", "--porcelain")
        if dirty:
            raise RuntimeError(
                "目标仓库工作区不干净，先 commit / stash / 还原后再来：\n" + dirty
            )

    def head(self) -> str:
        return self._git("rev-parse", "--short", "HEAD")

    def diff(self) -> str:
        return self._git("diff")

    def diff_stat(self) -> str:
        return self._git("diff", "--stat")

    def modified_files(self) -> list[str]:
        lines = self._git("status", "--porcelain").splitlines()
        return [line[3:].strip() for line in lines if line.strip()]

    def reset_hard(self) -> None:
        """兜底回滚：已跟踪文件还原 + 新增的未跟踪文件清掉。"""
        self._git("checkout", "--", ".")
        self._git("clean", "-fd")

    # -- 给 Planner 的仓库鸟瞰图 ---------------------------------------------
    def tree_summary(self, max_files: int = 300) -> str:
        """用 git ls-files 而不是 os.walk：天然跳过 .git、遵守 .gitignore。"""
        files = self._git("ls-files").splitlines()
        shown = "\n".join(files[:max_files])
        if len(files) > max_files:
            shown += f"\n… 以及另外 {len(files) - max_files} 个文件"
        return shown
