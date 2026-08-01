"""判分侧：输入 Instance ID + Agent Patch，输出评测报告。持有全部答案。

**推理侧永远不会 import 本模块。** 这里是 test_patch / FAIL_TO_PASS /
PASS_TO_PASS / gold patch 唯一的容身之处。

判分协议与官方一致：
    还原测试文件 → 打上官方 test_patch → 跑 FAIL_TO_PASS + PASS_TO_PASS
    → 全部通过才算 resolved

三个必须原样保留的细节（都是上一版实测踩出来的）：

  1) 【不能】按 node id 逐个选测试。数据集里的 F2P/P2P id 是当年按空格切
     pytest 输出生成的，参数含空格的 id 被切成了碎片
     （如 `test_locate_app[cliapp.factory-`），拿去 `pytest <id>` 必然 no-name 报错。
     官方判分是：跑整个测试文件，用同样的"空格截断"规则解析 -rA 摘要得到
     id→状态表，再逐个比对 —— 两边截断方式一致，碎片 id 也能精确对上。

  2) 打 test_patch 之前必须把测试文件【还原到 base 状态】。agent 对测试
     文件的改动一律不计分（只保留源码修改），否则它自己补的测试会和官方
     补丁冲突，`git apply` 直接失败，一个本来能过的补丁被判成打不上。
     这一条也正是"改测试作弊拿不到分"在机制上的保证。

  3) 两套语义不能混：F2P 是"必须修好"，必须白纸黑字 PASSED；
     P2P 是"不许退步"，只有显式 FAILED/ERROR 才算坏 —— 本环境跑不了而被
     SKIPPED 或没收集到的，agent 和 gold 一视同仁地忽略，对比仍然公平。

【gold 校准为什么必须提前、且隔离】
gold patch 在【推理开始之前】就跑一遍：gold 自己都过不了的实例，说明是环境
伪影（py3.12 杀老代码之类），判分器在那条实例上不可信。提前跑的好处是
这个判断不受 agent 结果影响 —— 事后再跑，就有了"哪条对我不利就说它是伪影"
的空间。gold 只用于环境校准和辅助分析，**绝不用文本相似度判定正确性**。
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from repopilot.patch_audit import audit as audit_patch

from .environment import Env, reset, sh
from .instances import SecretInstance, scan_for_leak

_STATUS_LINE = re.compile(r"^(PASSED|FAILED|ERROR|XFAIL|XPASS|SKIPPED)\s+(\S+)")


@dataclass
class GradeResult:
    instance_id: str
    resolved: bool = False
    f2p_ok: bool = False
    p2p_ok: bool = False
    patch_applied: bool = True
    note: str = ""
    f2p_total: int = 0
    p2p_total: int = 0
    f2p_bad: list[str] = field(default_factory=list)
    p2p_bad: list[str] = field(default_factory=list)
    gold_ok: bool | None = None          # None = 还没校准
    gold_note: str = ""
    judge: str = ""                      # 判分执行环境：local-container / local-venv
    audit: dict = field(default_factory=dict)
    leak_findings: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def grade_patch(env: Env, secret: SecretInstance, patch: str, *,
                timeout: int = 1800) -> GradeResult:
    """给一份补丁判分。这是判分侧的主入口。"""
    res = GradeResult(instance_id=secret.instance_id,
                      f2p_total=len(secret.fail_to_pass),
                      p2p_total=len(secret.pass_to_pass))
    repo_dir = env.repo_dir
    if repo_dir is None:
        res.note = "没有可用的仓库目录（环境准备失败）"
        return res

    # 补丁质量与作弊审计。在还原测试文件【之前】做 —— 之后就看不到
    # agent 到底动没动测试文件了，而那恰恰是最该记的一条。
    res.audit = audit_patch(patch, gold_diff=secret.gold_patch).to_dict()

    reset(env)
    if patch.strip():
        applied, note = _apply(repo_dir, patch)
        res.patch_applied = applied
        if not applied:
            res.note = f"agent 补丁打不上：{note}"
            return res
    else:
        res.patch_applied = True
        res.note = "空补丁（agent 没有做任何修改）"

    ok = _apply_test_patch(repo_dir, secret)
    if not ok:
        res.note = "官方 test_patch 打不上（判分环境异常）"
        return res

    res.f2p_ok, res.p2p_ok, res.f2p_bad, res.p2p_bad, note = _run_official(
        env, repo_dir, secret, timeout)
    res.resolved = bool(res.f2p_ok and res.p2p_ok)
    res.judge = "local-container" if env.exec_mode == "docker" else "local-venv"
    res.note = res.note or note
    reset(env)
    return res


def calibrate_gold(env: Env, secret: SecretInstance, *, timeout: int = 1800) -> dict:
    """用官方 gold patch 在【我们的环境】里跑同一套判分。

    gold 过不了 = 环境伪影，这条实例上判分器不可信。**它仍然留在分母里**，
    只是在报告里单列 —— 从分母里删掉"对我不利"的实例是评测里最常见的自欺。
    """
    repo_dir = env.repo_dir
    if repo_dir is None:
        return {"gold_ok": False, "gold_note": "环境准备失败"}

    reset(env)
    if secret.gold_patch.strip():
        applied, note = _apply(repo_dir, secret.gold_patch)
        if not applied:
            reset(env)
            return {"gold_ok": False, "gold_note": f"gold 打不上：{note}"}
    if not _apply_test_patch(repo_dir, secret):
        reset(env)
        return {"gold_ok": False, "gold_note": "test_patch 打不上"}

    f2p_ok, p2p_ok, f2p_bad, p2p_bad, note = _run_official(
        env, repo_dir, secret, timeout)
    reset(env)
    return {"gold_ok": bool(f2p_ok and p2p_ok), "gold_note": note[:300],
            "judge": "local-container" if env.exec_mode == "docker" else "local-venv"}


# 泄露扫描分两级，因为它们说的是两件完全不同的事。
#   input  我们【喂给】agent 的东西里出现了隐藏测试标识 → 真泄露，必须是 0
#   trace  agent 【自己产出】的东西里出现了 → 多半是巧合，只作记录
# 实测出的那个巧合：agent 自己补了一个测试，随手起名 test_empty_name_not_allowed，
# 正好和官方 FAIL_TO_PASS 的用例同名 —— 两边都取了最自然的名字而已。
# 不分级的话，这种巧合会被记成"疑似泄露"，而真泄露反而淹没在里面。
_INPUT_ARTIFACTS = ("inference_input.json", "issue.md")
_OUTPUT_ARTIFACTS = ("solve_result.json", "baseline_b_trace.json", "baseline_a_raw.txt")


def audit_inference(out_dir: Path, secret: SecretInstance) -> list[dict]:
    """事后泄露扫描。返回带 severity 的命中列表（input 级必须为空）。"""
    findings = []
    for names, severity in ((_INPUT_ARTIFACTS, "input"), (_OUTPUT_ARTIFACTS, "trace")):
        texts = {n: (out_dir / n).read_text(encoding="utf-8", errors="replace")
                 for n in names if (out_dir / n).exists()}
        findings += [{**f, "severity": severity} for f in scan_for_leak(texts, secret)]
    return findings


# ---------------------------------------------------------------------------
# 官方判分的三个零件
# ---------------------------------------------------------------------------
def _apply(repo_dir: Path, patch: str) -> tuple[bool, str]:
    import subprocess
    proc = subprocess.run(["git", "apply", "--whitespace=nowarn", "-"],
                          input=patch if patch.endswith("\n") else patch + "\n",
                          cwd=repo_dir, capture_output=True, text=True)
    return proc.returncode == 0, (proc.stdout + proc.stderr)[-300:]


def _apply_test_patch(repo_dir: Path, secret: SecretInstance) -> bool:
    """先还原测试文件，再打官方 test_patch。顺序不能反（见文件头第 2 条）。"""
    test_files = sorted(set(re.findall(r"^diff --git a/(\S+)",
                                       secret.test_patch, re.M)))
    if test_files:
        sh(["git", "checkout", "HEAD", "--", *test_files], cwd=repo_dir)  # 还原已跟踪的
        sh(["git", "clean", "-fq", "--", *test_files], cwd=repo_dir)      # 删掉新建的
    applied, _ = _apply(repo_dir, secret.test_patch)
    return applied


def _run_official(env: Env, repo_dir: Path, secret: SecretInstance,
                  timeout: int):
    """跑整个测试文件，解析 -rA 摘要，再逐个比对 F2P / P2P。

    docker 模式下 pytest 在官方镜像容器里跑（era-correct 环境）；
    git 打补丁/还原永远在宿主 —— 两侧本来就是同一棵 bind-mount 的树。
    """
    ids = [*secret.fail_to_pass, *secret.pass_to_pass]
    files = sorted({i.split("::")[0] for i in ids})
    if not files:
        return False, False, [], [], "数据集里没有 F2P/P2P，无法判分"

    if env.exec_mode == "docker":
        from repopilot.execenv import DockerExecutor
        ex = DockerExecutor(env.container_name, repo_dir, env.container_python)
        r = ex.run(f"{env.container_python} -m pytest -rA --color=no "
                   + " ".join(files), cwd=repo_dir, timeout=timeout)
        out = r.output
    else:
        _, out = sh([env.venv_py, "-m", "pytest", "-rA", "--color=no", *files],
                    cwd=repo_dir, timeout=timeout)

    status: dict[str, str] = {}
    for line in out.splitlines():
        m = _STATUS_LINE.match(line.strip())
        if m:
            status[m.group(2)] = m.group(1)   # 同款截断：\S+ 到第一个空格为止

    f2p_bad = [i for i in secret.fail_to_pass
               if status.get(i) not in ("PASSED", "XPASS")]
    p2p_bad = [i for i in secret.pass_to_pass
               if status.get(i) in ("FAILED", "ERROR")]
    note = " | ".join(f"{i}→{status.get(i, '未收集')}"
                      for i in (f2p_bad + p2p_bad)[:3])
    return not f2p_bad, not p2p_bad, f2p_bad, p2p_bad, note


def save(out_dir: Path, res: GradeResult) -> None:
    (out_dir / "grade.json").write_text(
        json.dumps(res.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


def load(out_dir: Path) -> GradeResult | None:
    p = out_dir / "grade.json"
    if not p.exists():
        return None
    return GradeResult(**json.loads(p.read_text()))
