"""线束硬杀：软预算拦不住的那一半。

状态机的预算闸门只能拦【状态入口】——它拦不住一次已经开跑的全量测试
（可以跑满 TEST_TIMEOUT），也拦不住一次卡在网络退避里的模型调用。
实测三条实例分别跑了 65 / 97 / 73 分钟，预算写的是 30 分钟。

所以外层还要有一道"无论如何都会停"。这个文件守它的两条性质：

  1) 杀的是【整个进程组】。agent 会派 pytest 出去，只杀直接子进程会留下
     一地孤儿 pytest —— 而这台机器的瓶颈正是内存，孤儿会把后面的实例拖垮。
  2) 杀完要【补一份结果】。不补的话下游连着错两次：失败归因把它归进
     UNKNOWN（"超时"和"agent 不会修"就混成了一件事），而判分那边先判
     inference.json 存在、再无条件读 agent.patch，缺文件当场把整批判分带崩。
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

EVAL = Path(__file__).resolve().parents[1] / "eval"
sys.path.insert(0, str(EVAL))

import run  # noqa: E402
from harness import failures  # noqa: E402


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@pytest.mark.skipif(os.name != "posix", reason="进程组语义是 POSIX 的")
def test_kill_group_takes_the_grandchildren_too(tmp_path):
    """父进程派了一个孙子进程出去，硬杀之后两个都不许还活着。"""
    pidfile = tmp_path / "child.pid"
    # bash 派一个 sleep 出去（孙子），记下它的 pid，然后自己也睡着。
    script = f'sleep 300 & echo $! > {pidfile}; sleep 300'
    proc = subprocess.Popen(["bash", "-c", script], start_new_session=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    deadline = time.time() + 10
    while not pidfile.exists() and time.time() < deadline:
        time.sleep(0.05)
    grandchild = int(pidfile.read_text().strip())
    assert _alive(grandchild)

    run._kill_group(proc, grace=3.0)

    assert proc.poll() is not None, "父进程没被杀掉"
    deadline = time.time() + 5
    while _alive(grandchild) and time.time() < deadline:
        time.sleep(0.05)
    assert not _alive(grandchild), "孙子进程活了下来 —— 孤儿 pytest 会把内存吃光"


def test_stamped_timeout_classifies_as_timeout_not_no_patch(tmp_path):
    """补出来的结果必须落进 TIMEOUT 桶。

    这条分得很细但很要紧：分类表里 NO_PATCH 也命中"一行代码都没改"，
    而"线束把它掐了"和"agent 跑完了但没做出来"对下一步优化的指向完全相反。
    """
    run._stamp_harness_timeout(tmp_path, "硬超时：超过 3600s", 3600)

    row = json.loads((tmp_path / "inference.json").read_text())
    assert row["halt_code"] == "TIMEOUT"
    assert row["killed_by_harness"] is True

    row.update({"resolved": False, "env_ok": True, "gold_ok": True})
    assert failures.classify(row) == "TIMEOUT"


def test_stamped_timeout_leaves_a_patch_file_for_grading(tmp_path):
    """判分那边 `(out_dir / "agent.patch").read_text()` 是无条件读的。

    只补 inference.json 不补 agent.patch，会在判分阶段抛 FileNotFoundError,
    把【整批】其他实例的判分一起带崩 —— 一条超时毁掉一次运行。
    """
    run._stamp_harness_timeout(tmp_path, "硬超时", 3600)

    patch = tmp_path / "agent.patch"
    assert patch.exists() and patch.read_text() == ""


def test_stamped_timeout_does_not_clobber_a_patch_that_survived(tmp_path):
    """子进程可能在被杀之前已经把补丁写下来了 —— 那份是真的，不许覆盖。"""
    (tmp_path / "agent.patch").write_text("diff --git a/x b/x\n")
    run._stamp_harness_timeout(tmp_path, "硬超时", 3600)

    assert (tmp_path / "agent.patch").read_text() == "diff --git a/x b/x\n"
