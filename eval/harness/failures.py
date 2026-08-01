"""失败归因：把每个没解决的实例归进【一个】主要原因。

【为什么必须只归一类】
"这条又是根因错、又超时、又改多了" —— 三个都记等于没记。归因的用处是
指导下一步优化，而下一步只能做一件事。所以每条失败只留一个主因，
按"哪个先发生、哪个更根本"排序判定。

分完之后统计的样子：

    10 个失败
    ├── 4 个根因定位错误
    ├── 2 个修改不完整
    ├── 2 个工具循环
    ├── 1 个引入回归
    └── 1 个超时

这张表直接告诉你下一阶段该干什么。**大部分失败是根因定位错误，
就该去改 Explorer 和代码检索，而不是继续加 Reviewer 轮数。**
上一版报告里只有一句"3/8 resolved"，那句话不指导任何行动。

【判定用的是可观测量，不是模型的自述】
每一条规则都能追到一个具体字段：判分结果、补丁审计、停机码、账本。
让模型自己解释"我为什么失败"是行不通的 —— 它会给一个听起来很合理的故事。
"""

from __future__ import annotations

# 顺序即优先级：越靠前越"根本"。第一个命中的就是主因。
TAXONOMY = [
    ("ENVIRONMENT_ERROR", "环境装不起来 / 基线跑不动，这条题目根本没能开始"),
    ("GOLD_UNCERTIFIABLE", "官方 gold 补丁在本环境也过不了 —— 判分器在这条上不可信"),
    ("HARNESS_ERROR", "评测线束自己崩了，与 agent 无关"),
    ("TIMEOUT", "单实例墙钟超时"),
    ("TOKEN_LIMIT", "token 预算耗尽"),
    ("TOOL_CALL_LIMIT", "工具调用次数耗尽"),
    # 这一类【必须】和补丁写错分开：两者都表现为 git apply 失败，但截断是
    # 【我们的预算】掐断了它，不是它自己的能力问题。混在一起会让评测者
    # 把自己造成的失败记到被测方案头上。
    ("OUTPUT_TRUNCATED", "模型输出被 max_output_tokens 截断，补丁不完整（预算问题，非能力问题）"),
    ("PATCH_APPLY_FAILED", "补丁格式不对，git apply 失败"),
    ("NO_PATCH", "跑完了但一行代码都没改"),
    ("ISSUE_NOT_REPRODUCED", "改之前就无法复现 issue，主动停止"),
    ("TEST_TAMPERING", "试图改测试文件 / 加 skip / 删断言来让测试变绿"),
    ("REGRESSION", "把原本通过的测试改挂了（P2P 退步）"),
    ("TOOL_LOOP", "在同一个错误上反复打转，被停机策略拦下"),
    ("REVIEWER_REJECTED", "Reviewer 驳回且没能改好"),
    ("WRONG_FILE_EDITED", "改的文件和标准答案完全不沾边"),
    ("INCOMPLETE_FIX", "方向对了但没修完：部分 FAIL_TO_PASS 仍然红"),
    ("ROOT_CAUSE_WRONG", "根因定位错了：FAIL_TO_PASS 一条都没转绿"),
    ("UNKNOWN", "以上都不是"),
]
LABELS = dict(TAXONOMY)

_HALT_TO_CATEGORY = {
    "TIMEOUT": "TIMEOUT",
    "TOKEN_LIMIT": "TOKEN_LIMIT",
    "TOOL_CALL_LIMIT": "TOOL_CALL_LIMIT",
    "ENVIRONMENT_ERROR": "ENVIRONMENT_ERROR",
    "HARNESS_ERROR": "HARNESS_ERROR",
    "ISSUE_NOT_REPRODUCED": "ISSUE_NOT_REPRODUCED",
    "SAME_FAILURE_TWICE": "TOOL_LOOP",
    "NO_PROGRESS": "TOOL_LOOP",
    "DIFF_GROWING": "TOOL_LOOP",
    "MODIFIED_FILES_EXCEEDED": "WRONG_FILE_EDITED",
    "REVIEWER_REJECTED": "REVIEWER_REJECTED",
    "REVIEWER_REPEAT": "REVIEWER_REJECTED",
}


def classify(row: dict) -> str:
    """row = 一条合并后的记录（inference + grade + env）。返回主因分类码。

    读的字段全部是可观测量：
      env_ok / gold_ok      环境与判分器可信度
      halt_code             状态机给出的停机原因
      patch_applied / patch 补丁本身
      f2p_bad / p2p_bad     判分明细：哪些该修的没修、哪些不该坏的坏了
      audit                 补丁审计：有没有动测试
    """
    if row.get("resolved"):
        return ""                                  # 成功的实例不归因

    if not row.get("env_ok", True):
        return "ENVIRONMENT_ERROR"
    if row.get("gold_ok") is False:
        return "GOLD_UNCERTIFIABLE"

    halt = row.get("halt_code") or ""
    if halt in ("HARNESS_ERROR", "NO_RESULT"):
        return "HARNESS_ERROR"
    if halt in _HALT_TO_CATEGORY and halt in ("TIMEOUT", "TOKEN_LIMIT",
                                              "TOOL_CALL_LIMIT", "ISSUE_NOT_REPRODUCED"):
        return _HALT_TO_CATEGORY[halt]

    if row.get("output_truncated") or halt == "OUTPUT_TRUNCATED":
        return "OUTPUT_TRUNCATED"
    if row.get("patch_applied") is False:
        return "PATCH_APPLY_FAILED"
    if row.get("empty_patch"):
        return "NO_PATCH"

    audit = row.get("audit") or {}
    if audit.get("suspicious") and (audit.get("test_tampering_detected")
                                    or audit.get("suspicious_skip_added")
                                    or audit.get("assertion_removed")):
        return "TEST_TAMPERING"

    # P2P 退步 = 把本来好的改坏了。这比"没修好"严重，先判。
    if row.get("p2p_bad"):
        return "REGRESSION"

    if halt in _HALT_TO_CATEGORY:
        return _HALT_TO_CATEGORY[halt]

    # 改的文件和 gold 完全不沾边 → 定位就错了。
    # 注意这里【只用文件集合的交集】，不用文本相似度：同一个问题有多种正确
    # 修法，按文本像不像打分等于奖励抄标准答案的形状。
    overlap = audit.get("files_overlap")
    if overlap is not None and overlap == 0 and audit.get("gold_files"):
        return "WRONG_FILE_EDITED"

    f2p_bad, f2p_total = row.get("f2p_bad") or [], row.get("f2p_total") or 0
    if f2p_total and len(f2p_bad) == f2p_total:
        return "ROOT_CAUSE_WRONG"       # 一条都没转绿 = 根因判断整个错了
    if f2p_bad:
        return "INCOMPLETE_FIX"         # 转绿了一部分 = 方向对，没修完

    return "UNKNOWN"


def tally(rows: list[dict]) -> list[tuple[str, int]]:
    """按分类统计，按数量倒序。返回 [(分类码, 个数), …]。"""
    counts: dict[str, int] = {}
    for r in rows:
        cat = r.get("failure_category")
        if cat:
            counts[cat] = counts.get(cat, 0) + 1
    order = {code: i for i, (code, _) in enumerate(TAXONOMY)}
    return sorted(counts.items(), key=lambda kv: (-kv[1], order.get(kv[0], 99)))


def render_tree(rows: list[dict]) -> str:
    """画成那棵树。比一张表更容易一眼看出"下一步该修哪里"。"""
    counts = tally(rows)
    total = sum(n for _, n in counts)
    if not total:
        return "（没有失败实例）"
    lines = [f"{total} 个失败"]
    for i, (code, n) in enumerate(counts):
        branch = "└──" if i == len(counts) - 1 else "├──"
        lines.append(f"{branch} {n} 个 {LABELS.get(code, code)}  [{code}]")
    return "\n".join(lines)
