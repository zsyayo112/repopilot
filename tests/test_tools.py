"""工具层单元测试：硬约束必须有测试守着（软约束才靠祈祷）。"""

import subprocess

import pytest

from repopilot.tools import ToolKit
from repopilot.workspace import Workspace


@pytest.fixture
def repo(tmp_path):
    """造一个最小的真实 git 仓库当靶子。"""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "hello.py").write_text("def greet():\n    return 'hi'\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "init"], check=True)
    return tmp_path


@pytest.fixture
def kit(repo):
    return ToolKit(Workspace(repo))


def test_jail_blocks_escape(kit):
    assert "错误" in kit.execute("read_file", {"path": "../../etc/passwd"})


def test_jail_blocks_git_dir_write(kit):
    result = kit.execute("edit_file", {
        "path": ".git/config", "old_string": "x", "new_string": "y"})
    assert "错误" in result


def test_edit_requires_unique_match(kit, repo):
    (repo / "dup.py").write_text("x = 1\nx = 1\n")
    result = kit.execute("edit_file", {
        "path": "dup.py", "old_string": "x = 1", "new_string": "x = 2"})
    assert "2 次" in result  # 出现两次 → 必须拒绝


def test_edit_happy_path(kit, repo):
    result = kit.execute("edit_file", {
        "path": "hello.py", "old_string": "return 'hi'", "new_string": "return 'hello'"})
    assert "已修改" in result
    assert "return 'hello'" in (repo / "hello.py").read_text()


def test_write_refuses_overwrite(kit):
    result = kit.execute("write_file", {"path": "hello.py", "content": "boom"})
    assert "已存在" in result


def test_dangerous_command_blocked(kit):
    assert "安全策略" in kit.execute("run_bash", {"command": "sudo rm -rf /"})
    assert "安全策略" in kit.execute("run_bash", {"command": "git commit -m hack"})


def test_symbols(kit):
    out = kit.execute("list_symbols", {"path": "hello.py"})
    assert "def greet()" in out


@pytest.fixture
def same_name_repo(tmp_path):
    """仓库根目录名 == 内部包目录名（tinydb/tinydb、django/django 这种常见布局）。

    复现 2026-07-12 tinydb 实跑时炸掉的真实 bug：旧版 jail() 无脑剥"仓库名前缀"，
    把合法路径 "tinydb/queries.py" 剥成不存在的 "queries.py"。
    """
    root = tmp_path / "tinydb"
    pkg = root / "tinydb"
    pkg.mkdir(parents=True)
    (pkg / "queries.py").write_text("def q():\n    return 1\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "init"], check=True)
    return root


def test_jail_does_not_strip_legitimate_same_name_subdir(same_name_repo):
    kit = ToolKit(Workspace(same_name_repo))
    result = kit.execute("read_file", {"path": "tinydb/queries.py"})
    assert "def q()" in result

    result = kit.execute("edit_file", {
        "path": "tinydb/queries.py", "old_string": "return 1", "new_string": "return 2"})
    assert "已修改" in result


# ---------------------------------------------------------------------------
# 超长输出落盘 + read_artifact：截断不再销毁信息
# ---------------------------------------------------------------------------
@pytest.fixture
def kit_with_artifacts(repo, tmp_path_factory):
    art = tmp_path_factory.mktemp("artifacts")
    return ToolKit(Workspace(repo), artifacts_dir=art), art


def test_long_output_spills_to_handle_instead_of_truncating(kit_with_artifacts, repo):
    """以前超长输出直接截断 —— 丢掉的部分再也拿不回来，模型只能原样重调
    再被截一次。现在全文落盘，返回头部 + 句柄。"""
    kit, art = kit_with_artifacts
    (repo / "big.py").write_text("\n".join(f"line {i}" for i in range(3000)))
    out = kit.execute("read_file", {"path": "big.py"})

    assert "read_artifact" in out and "001-read_file" in out, "尾巴必须给出句柄和用法"
    assert out.index("line 0") < out.index("[输出共"), "头部内容要保留"
    assert (art / "spill" / "001-read_file.txt").read_text().endswith("line 2999")


def test_read_artifact_serves_line_ranges(kit_with_artifacts, repo):
    kit, _ = kit_with_artifacts
    (repo / "big.py").write_text("\n".join(f"line {i}" for i in range(3000)))
    kit.execute("read_file", {"path": "big.py"})

    out = kit.execute("read_artifact",
                      {"handle": "001-read_file", "start_line": 2001, "end_line": 2003})
    assert "line 2000\nline 2001\nline 2002" in out
    assert "共 3000 行" in out


def test_read_artifact_rejects_traversal_and_unknown_handles(kit_with_artifacts):
    kit, _ = kit_with_artifacts
    assert "错误" in kit.execute("read_artifact", {"handle": "../../etc/passwd"})
    assert "错误" in kit.execute("read_artifact", {"handle": "999-nothing"})


def test_read_artifact_caps_overly_wide_ranges_without_respilling(
        kit_with_artifacts, repo):
    """防递归：read_artifact 自己的超长结果不能再落盘成新句柄。"""
    kit, art = kit_with_artifacts
    (repo / "big.py").write_text("x" * 200 + "\n" + ("y" * 200 + "\n") * 200)
    kit.execute("read_file", {"path": "big.py"})

    out = kit.execute("read_artifact", {"handle": "001-read_file", "end_line": 200})
    assert "请缩小 start_line/end_line" in out
    assert len(list((art / "spill").iterdir())) == 1, "不该产生第二个落盘文件"


def test_without_artifacts_dir_falls_back_to_truncation(kit, repo):
    """单测/detect 场景没有落盘目录：保持老的截断行为，不崩。"""
    (repo / "big.py").write_text("z" * 20_000)
    out = kit.execute("read_file", {"path": "big.py"})
    assert "[输出被截断" in out
