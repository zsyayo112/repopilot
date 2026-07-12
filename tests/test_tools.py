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
