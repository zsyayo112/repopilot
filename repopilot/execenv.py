"""执行环境（executor seam）：命令【在哪里跑】与命令【是什么】解耦。

v0.5 及之前，agent 的每条命令都是宿主 subprocess —— 于是评测环境 = 宿主
venv 能装出什么。四道题因为现代解释器装不出 2019 年的依赖直接废掉，环境
漂移成了贯穿性混淆项。v0.6 起评测走官方 SWE-bench 镜像：每实例一个长驻
容器，宿主 worktree bind-mount 到容器内的 /testbed。

分工的关键在于【只有执行进容器，其他都留在宿主】：
  · run_bash / run_tests / run_validation / run_e2e → DockerExecutor
  · 文件读写、search_code、git、jail → 宿主直接操作 bind-mount 的树
两侧看到的是同一棵树，路径恒等（宿主 <repo> ≡ 容器 /testbed），
editable 安装的 import 路径因此不变。

超时是双保险：容器内 `timeout -k 10` 是主闸（在 PID namespace 里杀，
不留僵尸）；宿主 subprocess 的 timeout+30 只是兜底，兜住 docker 客户端
本身卡死的情形，触发后再尽力 pkill 容器内残留。

约定与 environment.sh() 一致：executor 永远不抛异常，
失败用 code=-1 + 人话 output 表达。
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

#: 官方镜像的 conda testbed 环境优先。镜像的 ENV PATH 不含 testbed，
#: 不显式给的话 `python` 会解析到 base 环境 —— 那不是装了依赖的那个。
CONDA_PATH = ("/opt/miniconda3/envs/testbed/bin:/opt/miniconda3/bin:"
              "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")

CONTAINER_PYTHON = "/opt/miniconda3/envs/testbed/bin/python"


@dataclass
class ExecResult:
    code: int              # -1 = 超时或无法启动
    output: str            # stdout+stderr 合并；超时时是已产生的部分输出
    timed_out: bool = False


def _decode(raw) -> str:
    if raw is None:
        return ""
    return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)


class HostExecutor:
    """宿主 subprocess —— v0.5 的原行为，也是非评测场景的默认。"""

    def run(self, command: str, cwd=None, timeout: int = 180) -> ExecResult:
        try:
            proc = subprocess.run(command, shell=True, cwd=cwd,
                                  capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            return ExecResult(-1, _decode(e.stdout) + _decode(e.stderr),
                              timed_out=True)
        except OSError as e:
            return ExecResult(-1, f"命令无法启动：{e}")
        return ExecResult(proc.returncode, proc.stdout + proc.stderr)


class DockerExecutor:
    """docker exec 进长驻的每实例容器。

    以宿主 uid:gid 执行 —— 容器写进 bind-mount 的每个文件宿主都可删，
    reset() / git clean 才可信。HOME=/tmp 把 pytest/hypothesis 之类的
    家目录缓存引出 worktree。
    """

    CONTAINER_ROOT = "/testbed"

    def __init__(self, container: str, host_root, python: str = CONTAINER_PYTHON):
        self.container = container
        self.host_root = Path(host_root).resolve()
        self.python = python

    def _map(self, cwd) -> str:
        """宿主路径 → 容器路径。host_root 之外的一律落到 /testbed（防呆）。"""
        if cwd is None:
            return self.CONTAINER_ROOT
        try:
            rel = Path(cwd).resolve().relative_to(self.host_root)
        except ValueError:
            return self.CONTAINER_ROOT
        return f"{self.CONTAINER_ROOT}/{rel.as_posix()}".rstrip("/.")

    def run(self, command: str, cwd=None, timeout: int = 180) -> ExecResult:
        timeout = max(int(timeout), 1)
        argv = ["docker", "exec",
                "-w", self._map(cwd),
                "-u", f"{os.getuid()}:{os.getgid()}",
                "-e", f"PATH={CONDA_PATH}",
                "-e", "HOME=/tmp",
                "-e", "PYTHONDONTWRITEBYTECODE=1",
                self.container,
                "timeout", "-k", "10", str(timeout), "bash", "-c", command]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  timeout=timeout + 30)
        except subprocess.TimeoutExpired as e:
            self._reap(command)
            return ExecResult(-1, _decode(e.stdout) + _decode(e.stderr),
                              timed_out=True)
        except OSError as e:
            return ExecResult(-1, f"命令无法启动：{e}")
        out = proc.stdout + proc.stderr
        if proc.returncode == 124:          # 容器内 timeout(1) 到点
            return ExecResult(-1, out, timed_out=True)
        return ExecResult(proc.returncode, out)

    def _reap(self, command: str) -> None:
        """宿主兜底超时触发后，尽力清掉容器内可能残留的进程。"""
        frag = re.escape(command.strip()[:60])
        try:
            subprocess.run(["docker", "exec", self.container,
                            "pkill", "-9", "-f", frag],
                           capture_output=True, timeout=15)
        except (subprocess.TimeoutExpired, OSError):
            pass
