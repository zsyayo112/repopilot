"""环境层：跨变体污染，以及"读仓库声明"而不是"对仓库假设"。

这些测试全都对应一次实测踩坑。放在这里不是为了覆盖率，是因为每一条都
曾经【安静地】产出过错误的评测数字 —— 而安静是最贵的那种失败。
"""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))

from harness.environment import (  # noqa: E402
    Env,
    _collected_a_lot,
    _declares_extra,
    _dist_name,
    capture_patch,
    derive_test_command,
    pick_python,
    read_build_requires,
    reset,
)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q", str(r)], check=True)
    (r / "src.py").write_text("x = 0\n")
    subprocess.run(["git", "-C", str(r), "add", "."], check=True)
    subprocess.run(["git", "-C", str(r), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init"], check=True)
    return r


def test_reset_removes_files_capture_patch_staged(repo):
    """这是整个环境层最重要的一条不变量：**复位必须真的复位**。

    capture_patch() 会 `git add -A -N`，否则 git diff 看不见 agent 新建的文件。
    但被 -N 加进索引之后：clean -fd 不删它（它已经"被跟踪"了），
    checkout -- . 还会把它恢复出来。于是它活过了复位。

    实测后果：`tmp/test_repro.py` 从 full 变体一路活到后面每个变体，
    把干净门禁崩掉、被当成别的变体的补丁交上去判分。
    """
    env = Env(instance_id="x", repo_dir=repo)
    (repo / "newfile.py").write_text("agent wrote this\n")
    (repo / "src.py").write_text("x = 1\n")

    patch = capture_patch(env)
    assert "newfile.py" in patch, "capture_patch 必须看得见新建文件"

    reset(env)
    assert not (repo / "newfile.py").exists(), "复位后新建文件必须消失"
    assert (repo / "src.py").read_text() == "x = 0\n"
    dirty = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                           capture_output=True, text=True).stdout
    assert dirty == "", f"复位后工作区必须干净，实际：{dirty!r}"


def test_reset_is_idempotent_and_safe_on_a_clean_tree(repo):
    env = Env(instance_id="x", repo_dir=repo)
    reset(env)
    reset(env)
    assert (repo / "src.py").read_text() == "x = 0\n"


def test_dist_name_does_not_swallow_setuptools_scm():
    """`^(setuptools|wheel)\\b` 会匹配 `setuptools-scm` —— `-` 是非单词字符。

    这个前缀匹配把 setuptools-scm 从构建依赖里剔掉了，而 pytest 靠它生成
    _version.py。三条 pytest 实例挂在 `No module named '_pytest._version'`，
    根因就是一个 `\\b`。
    """
    assert _dist_name("setuptools-scm[toml]>=3.4") == "setuptools-scm"
    assert _dist_name("setuptools>=42.0") == "setuptools"
    assert _dist_name("wheel") == "wheel"


def test_build_requires_keeps_scm_but_drops_setuptools(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["setuptools>=42.0", "setuptools-scm[toml]>=3.4", '
        '"wheel", "Cython>=0.29"]\nbuild-backend = "setuptools.build_meta"\n')
    reqs = read_build_requires(tmp_path)
    assert any(r.startswith("setuptools-scm") for r in reqs)
    assert any(r.startswith("Cython") for r in reqs)
    assert not any(_dist_name(r) in ("setuptools", "wheel") for r in reqs)


def test_extras_detected_in_all_three_syntaxes(tmp_path):
    """三种写法在真实仓库里都出现，漏掉任何一种都会少装测试依赖。"""
    (tmp_path / "setup.cfg").write_text("[options.extras_require]\ntesting =\n    hypothesis\n")
    assert _declares_extra(tmp_path, "testing")

    (tmp_path / "setup.cfg").unlink()
    (tmp_path / "pyproject.toml").write_text('[project.optional-dependencies]\ntest = ["pytest"]\n')
    assert _declares_extra(tmp_path, "test")

    # setup.py 的字典字面量用【冒号】不是等号 —— 漏了这种写法，
    # pytest 就会装成裸的 -e .，hypothesis 进不来
    (tmp_path / "pyproject.toml").unlink()
    (tmp_path / "setup.py").write_text('setup(extras_require={"testing": ["hypothesis"]})\n')
    assert _declares_extra(tmp_path, "testing")
    assert not _declares_extra(tmp_path, "nonesuch")


def test_partial_collection_failure_does_not_disqualify():
    """收集到一大批、只有几个文件报错 → 仍然可用。

    判分只跑 F2P/P2P 指定的那几个文件。因为两个无关文件 import 失败就扔掉
    整条实例，是用一个【比判分更严】的标准去筛样本。
    """
    assert _collected_a_lot("1989 tests collected, 2 errors in 1.21s")
    assert not _collected_a_lot("!!! Interrupted: 5 errors during collection !!!")
    assert not _collected_a_lot("3 tests collected, 2 errors")


def test_python_version_comes_from_the_repos_own_declaration():
    assert pick_python(">=3.8") == "3.9"        # 偏好最老可用，老代码怕新解释器
    assert pick_python(">=3.10") == "3.10"
    assert pick_python("") == "3.9"


def test_test_command_never_reaches_case_granularity():
    """目录级路径是公开信息；用例级 id 只可能来自 F2P/P2P。"""
    cmd = derive_test_command("/v/bin/python", ["tests"])
    assert "::" not in cmd and "tests" in cmd
