"""推理侧与判分侧之间的那道墙 —— 用【类型】而不是纪律来保证它不被翻越。

    Inference（推理侧）          Evaluation（判分侧）
    输入：Issue + Base Commit    输入：Instance ID + Agent Patch
    输出：Agent Patch + Trace    内部持有：test_patch / F2P / P2P / gold
                                 输出：评测报告

【为什么不能靠"我小心一点"】
上一版评测里，agent 拿到的测试命令是从 test_patch 里抠出来的测试文件路径。
那不是恶意，是顺手：test_patch 就在手边的字典里，抠一下就能把测试范围缩小到
笔记本跑得动。但它泄露了一条真答案级别的提示 —— **官方隐藏测试在哪个文件**。
一个知道"去看 tests/test_config.py"的 agent，和一个不知道的，不是同一个 agent。

顺手是必然会发生的。所以边界不能是一条纪律，必须是一个**类型**：
推理侧的函数签名里只出现 PublicInstance，它连字段都没有，抠不出来。

PublicInstance 里的每个字段都要能回答"为什么它对推理侧是公开的"：
  instance_id             只是个名字，用来对账
  repo / base_commit      提交历史的公开事实，agent 本来就要 clone 它
  problem_statement       就是那个 issue，任务本身
  environment_setup_commit 装依赖用的公开提交（不含任何修复信息）
不在里面的：gold patch、test_patch、FAIL_TO_PASS、PASS_TO_PASS、hints_text。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

# 推理侧一旦碰到这些字段名就是越界。名字本身也是线索（"FAIL_TO_PASS" 出现在
# 提示词里，等于告诉模型"有一批隐藏测试正在等着"）。
SECRET_FIELDS = ("patch", "gold_patch", "test_patch", "FAIL_TO_PASS", "PASS_TO_PASS",
                 "fail_to_pass", "pass_to_pass", "hints_text")


class LeakError(AssertionError):
    """推理侧碰到了它不该看到的东西。这是【硬错误】，不是警告。

    为什么必须直接把这一批评测炸掉，而不是记一条 warning 继续跑：
    一次泄露的运行产出的数字是【无效】的，但它长得和有效数字一模一样，
    会混进 RESULTS.md、混进简历、混进对话。带毒的数字比没有数字危险得多。
    """


@dataclass(frozen=True)
class PublicInstance:
    """推理侧允许看到的【全部】信息。冻结 = 谁也别想往里塞第六个字段。"""

    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    environment_setup_commit: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_row(cls, row: dict) -> "PublicInstance":
        """从数据集原始行里【只挑】公开字段。这是唯一的入口。"""
        return cls(
            instance_id=row["instance_id"],
            repo=row["repo"],
            base_commit=row["base_commit"],
            problem_statement=row["problem_statement"],
            environment_setup_commit=row.get("environment_setup_commit") or "",
        )


@dataclass(frozen=True)
class SecretInstance:
    """判分侧独有的答案。**只应被 grader.py 导入。**

    如果哪天你发现 inference.py 里 import 了这个类，那就是墙被推倒了 ——
    这一行注释就是给未来的自己看的警戒线。
    """

    instance_id: str
    test_patch: str
    fail_to_pass: list[str] = field(default_factory=list)
    pass_to_pass: list[str] = field(default_factory=list)
    gold_patch: str = ""

    @classmethod
    def from_row(cls, row: dict) -> "SecretInstance":
        return cls(
            instance_id=row["instance_id"],
            test_patch=row["test_patch"],
            fail_to_pass=_as_list(row.get("FAIL_TO_PASS")),
            pass_to_pass=_as_list(row.get("PASS_TO_PASS")),
            gold_patch=row.get("patch") or "",
        )

    def secret_tokens(self) -> set[str]:
        """泄露扫描要找的字符串：测试 id、测试文件路径。

        只取【足够独特】的 token。像 "tests" 这种词满仓库都是，拿它去扫会
        把所有实例都报成泄露 —— 一个永远报警的检查等于没有检查。
        """
        tokens: set[str] = set()
        for tid in (*self.fail_to_pass, *self.pass_to_pass):
            tokens.add(tid)
            if "::" in tid:
                tokens.add(tid.split("::")[0])       # 测试文件路径
                tokens.add(tid.split("::")[-1].split("[")[0])   # 用例函数名
        for line in self.test_patch.splitlines():
            if line.startswith("diff --git a/"):
                tokens.add(line.split()[2][2:])
        return {t for t in tokens if len(t) > 8}


def _as_list(v) -> list[str]:
    """数据集里 F2P/P2P 有时是 JSON 字符串、有时已经是列表，两种都要吃得下。"""
    if v is None:
        return []
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except json.JSONDecodeError:
            return [v]
    return [str(x) for x in v]


# ---------------------------------------------------------------------------
# 泄露扫描：证明"不存在隐藏测试泄露"这件事本身也需要证据
# ---------------------------------------------------------------------------
def assert_public_only(payload: dict, where: str = "推理侧输入") -> None:
    """推理侧要发出去的东西，事前过一道。发现禁词直接炸。"""
    blob = json.dumps(payload, ensure_ascii=False, default=str)
    hits = [f for f in SECRET_FIELDS if f'"{f}"' in blob]
    if hits:
        raise LeakError(f"{where}里出现了判分侧字段：{hits}。"
                        "推理侧只能拿 PublicInstance。")


def scan_for_leak(texts: dict[str, str], secret: SecretInstance) -> list[dict]:
    """事后扫描：agent 的轨迹里出现过隐藏测试的 id 或路径吗？

    事前断言管住的是"我们给了什么"，这一步管的是"它是不是从别处知道了"——
    比如某个工具把 test_patch 目录列出来了、或者仓库里恰好有残留。
    发现命中【不自动判违规】：agent 自己 ls 到一个测试文件是完全正常的。
    这里只负责如实记录，让报告里能写出"某某实例的轨迹中出现了官方测试路径"。
    """
    tokens = secret.secret_tokens()
    findings = []
    for name, text in texts.items():
        if not text:
            continue
        for tok in tokens:
            if tok in text:
                idx = text.index(tok)
                findings.append({"where": name, "token": tok,
                                 "context": text[max(0, idx - 60):idx + 60]})
                break          # 每个来源报一条就够，不刷屏
    return findings
