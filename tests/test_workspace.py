"""Workspace 的 diff 必须把【新建的未跟踪文件】也算进去。

cobra#1777 实战翻的车：agent 用 write_file 新建了 repro_test.go，
裸 `git diff` 对未跟踪文件视而不见 —— 检查点存的 diff 是空的、回滚重放
不出这个文件、补丁审计和 NO_PATCH 判定都以为它不存在。
"""

import subprocess

from repopilot.workspace import Workspace


def _repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "a.py").write_text("x = 0\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "init"], check=True)
    return Workspace(tmp_path)


def test_diff_sees_new_untracked_files(tmp_path):
    ws = _repo(tmp_path)
    (tmp_path / "repro_test.py").write_text("def test_repro():\n    assert True\n")

    diff = ws.diff()
    assert "repro_test.py" in diff
    assert "def test_repro" in diff
    assert "repro_test" in ws.diff_stat()


def test_diff_leaves_the_index_untouched(tmp_path):
    """diff 对外必须表现为纯读：不能把 intent-to-add 残留在索引里，
    否则下一次 ensure_clean 会莫名其妙拦人。"""
    ws = _repo(tmp_path)
    (tmp_path / "new.py").write_text("y = 1\n")
    ws.diff()
    staged = subprocess.run(
        ["git", "-C", str(tmp_path), "diff", "--cached", "--name-only"],
        capture_output=True, text=True).stdout.strip()
    assert staged == "", "diff 之后索引里不该有任何残留"


def test_checkpoint_roundtrip_replays_new_files(tmp_path):
    """回滚到最佳检查点 = reset_hard + 重放 diff。新建文件必须在重放后复活。"""
    ws = _repo(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "new_module.py").write_text("z = 42\n")
    snapshot = ws.diff()

    ws.reset_hard()
    assert not (tmp_path / "new_module.py").exists()

    assert ws.apply_diff(snapshot)
    assert (tmp_path / "new_module.py").read_text() == "z = 42\n"
    assert (tmp_path / "a.py").read_text() == "x = 1\n"
