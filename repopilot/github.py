"""GitHub 集成：可选的外壳（Phase 4 的正主，这里先给最薄的一层）。

设计选择：用 gh CLI 而不是 Octokit/REST —— 登录态复用 `gh auth login`，
零配置、零密钥管理。壳越薄越好，核心价值不在这里。
"""

import json
import subprocess


def fetch_issue(ref: str) -> str:
    """ref 形如 'owner/repo#37'，返回拼好的 issue 全文（标题+正文+前 5 条评论）。"""
    if "#" not in ref:
        raise ValueError("格式应为 owner/repo#编号，例如 pallets/click#123")
    repo, num = ref.rsplit("#", 1)

    proc = subprocess.run(
        ["gh", "issue", "view", num, "--repo", repo,
         "--json", "title,body,comments"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gh 调用失败（需要先 gh auth login）：{proc.stderr.strip()}")

    data = json.loads(proc.stdout)
    parts = [f"# {data['title']}", "", data.get("body") or ""]
    for c in (data.get("comments") or [])[:5]:
        parts += ["", f"[评论] {c.get('body', '')}"]
    return "\n".join(parts)


def create_draft_pr(*args, **kwargs):
    raise NotImplementedError(
        "Phase 4 课题：创建修复分支 → git commit → git push → "
        "gh pr create --draft --body <报告>。注意：做这步之前要先解除 "
        "policy.py 里对 git commit/push 的封锁，并想清楚解除的边界在哪。"
    )
