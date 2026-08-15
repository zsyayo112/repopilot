"""停机策略与最佳补丁检查点：让 agent 知道【什么时候该停下来】。

【这个文件是被评测数据逼出来的】
上一轮 SWE-bench 迷你评测里，失败实例出现过 54～120 次工具调用、最长 901 秒。
翻轨迹会看到同一种画面反复出现：

    改一处 → 跑测试 → 同样的 3 个失败 → 再改一处 → 跑测试 → 同样的 3 个失败 …

模型不会自己承认"我这个方向错了"。它总能在当前修改上再想出一个小调整，
于是永远在最新的（也是越改越大的）diff 上继续叠。**没人喊停，它就不停。**

停机规则的共同形状是：**不看模型说了什么，只看可观测量的变化。**
  · 失败指纹连续两次相同  → 这一轮什么都没改变，重新规划
  · 同一个文件连改三次没改善 → 这个方案不成立，换方案
  · diff 在变大、失败数没减 → 这一轮是负收益，回滚
  · token 用到八成       → 禁止大范围探索，把剩下的额度留给收尾
  · Reviewer 两次提同一个意见 → 它不会被说服，别再烧一轮

【最佳补丁检查点，以及为什么它比"继续改"更重要】
默认行为是"在最新修改上继续работать"，但最新 ≠ 最好：

    补丁 A：失败 3 个
    补丁 B：失败 1 个   ← 最好
    补丁 C：失败 5 个   ← 最新

停在 C 上交卷，就是把已经拿到手的成绩丢了。checkpoint 机制记住 B，
恶化时回滚到 B 再继续 —— 单调不降，这是一种很便宜的稳定性。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 对比状态的"好坏序"。数字越小越好，用来判断"这轮比上轮好还是坏"。
# no_regression 与 still_green 同级：红基线是环境的底色，不是这轮的失分。
# improved 也在 0 级：它只可能在红基线下出现，且剩余失败必然是基线红的子集
# —— 严格优于 no_regression。同级之间靠 score 的失败数分高下，improved
# 的失败数一定更少。把它排在 1 会让 no_regression 在选最佳补丁时反超它。
_RANK = {"fixed": 0, "still_green": 0, "no_regression": 0, "improved": 0,
         "no_change": 2, "regressed": 3}

TOKEN_PRESSURE_LINE = 0.8      # 用到八成就收缩：留出的两成是给收尾和审查的
SAME_FILE_STREAK = 3           # 同一文件连改几次没改善就判方案不成立


@dataclass
class Checkpoint:
    """某一轮结束时的完整现场：diff + 它的成绩。回滚就是把 diff 重放回去。"""

    attempt: int
    diff: str
    status: str
    failures: int
    signature: str
    summary: str = ""
    # 这一轮的新增失败名单。回滚到这个检查点时，对外报告的 new_failures
    # 必须跟着换成这一份 —— 否则工作区是补丁 B、报告里却挂着补丁 C 的回归名单。
    new_failures: list[str] = field(default_factory=list)

    @property
    def score(self) -> tuple[int, int]:
        """(状态等级, 失败数) —— 字典序比较，越小越好。"""
        return (_RANK.get(self.status, 2), self.failures)

    @property
    def is_passing(self) -> bool:
        """测试这一关已经过了。

        【这个属性存在的理由】停机规则几乎全是"没进展就停"的形状，而"没进展"
        在已经通过的状态下是【好事】：测试连续两轮都绿、失败集合都是空集，
        指纹当然一样 —— 那不是卡住了，那是修好了。少了这道判断，
        Reviewer 驳回后的第二轮会被"连续两次失败相同"误杀在门口。

        improved 也算通过：它只可能在红基线下出现（绿基线下任何失败都是
        regressed），剩余的红全是基线预置的环境底色。真实案例（tenacity#233）：
        基线 7 红 → 修完 6 红判 improved，循环却因"没全绿"又烧了一整轮预算
        去修那 6 个与 issue 无关的环境失败，直到 TOKEN_LIMIT 才靠止损交卷。
        与基线打平的 no_regression 都算过，严格更好的 improved 没有理由不算。
        """
        return self.status in ("fixed", "still_green", "no_regression", "improved")


@dataclass
class Decision:
    """停机策略给出的裁决。code 会原样进 trace 和失败分类统计。"""

    action: str          # continue / stop / rollback / narrow
    code: str = ""       # SAME_FAILURE_TWICE / NO_PROGRESS / DIFF_GROWING / …
    reason: str = ""     # 给人看的一句话

    @property
    def should_stop(self) -> bool:
        return self.action == "stop"


@dataclass
class HaltingPolicy:
    """一轮一轮地观察，给出裁决。无状态的规则 + 有状态的历史，都在这里。"""

    budget: object                       # Budget；只读 max_modified_files 等
    enabled: bool = True
    history: list[Checkpoint] = field(default_factory=list)
    file_streak: dict[str, int] = field(default_factory=dict)
    reviewer_requests: list[str] = field(default_factory=list)
    narrowed: bool = False               # 是否已经进入"禁止大范围探索"状态
    events: list[dict] = field(default_factory=list)   # 触发过哪些规则，进报告

    # -- 检查点 -------------------------------------------------------------
    @property
    def best(self) -> Checkpoint | None:
        """当前最佳补丁。同分取更早的那个 —— 更早通常意味着 diff 更小。"""
        return min(self.history, key=lambda c: (c.score, c.attempt), default=None)

    def record(self, attempt: int, comparison: dict, report, diff: str,
               modified: list[str]) -> Checkpoint:
        """把这一轮的结果存成检查点，并更新"同一文件连改几次"的计数。"""
        cp = Checkpoint(
            attempt=attempt, diff=diff, status=comparison["status"],
            failures=_failure_count(report, comparison),
            signature=report.signature() if report is not None else "",
            summary=comparison.get("after", ""),
            new_failures=list(comparison.get("new_failures") or []),
        )
        improved = self._improved_over_previous(cp)
        for path in modified:
            self.file_streak[path] = 0 if improved else self.file_streak.get(path, 0) + 1
        self.history.append(cp)
        return cp

    def _improved_over_previous(self, cp: Checkpoint) -> bool:
        # 已经通过的状态永远算"有进展" —— 否则测试全绿时 streak 会一直涨，
        # 最后被 NO_PROGRESS 判死在一个其实已经修好的补丁上。
        if cp.is_passing:
            return True
        # 第一轮没有"上一轮"可比，但它有【基线】可比 —— 而 status 本来就是
        # 相对基线的判断。走到这里的第一轮只剩 no_change / regressed，
        # 都不算进展。把第一轮无条件算作有进展，会让 streak 白白少一格，
        # 于是"连续三轮没改善"实际要跑四轮才触发。
        if not self.history:
            return False
        return cp.score < self.history[-1].score

    # -- 裁决 ---------------------------------------------------------------
    def decide(self, ledger, modified: list[str]) -> Decision:
        """看完这一轮，决定继续 / 收缩 / 回滚 / 停。

        顺序有讲究：先判"硬超支"（再跑也是白花钱），再判"没进展"（跑了也没用），
        最后才是"收缩"（还能跑，但要省着跑）。
        """
        if not self.enabled:
            return Decision("continue")

        over = ledger.exhausted()
        if over:
            return Decision("stop", over, f"预算耗尽（{over}），停止并交出当前最佳补丁。")

        if len(modified) > self.budget.max_modified_files:
            return Decision("stop", "MODIFIED_FILES_EXCEEDED",
                            f"改动了 {len(modified)} 个文件，超过上限 "
                            f"{self.budget.max_modified_files}，判为跑偏。")

        # 先判"方案已经死了"，再判"这一轮亏了"。两者可能同时成立，
        # 而结论强度不同：stop 是"别修了"，rollback 是"退一步再试"。
        # 同时成立时必须给出更强的那个，否则会退一步、再试、再退一步，
        # 正好复现掉这套规则要消灭的那种打转。
        stuck = [p for p, n in self.file_streak.items() if n >= SAME_FILE_STREAK]
        if stuck:
            return Decision("stop", "NO_PROGRESS",
                            f"{'、'.join(stuck[:3])} 已连续改了 {SAME_FILE_STREAK} 轮"
                            "仍无改善 —— 当前方案不成立。")

        if len(self.history) >= 2:
            last, prev = self.history[-1], self.history[-2]

            # "改坏了"要判，即使这一轮看起来还行 —— 回归是所有规则里最该拦的。
            if last.score > prev.score:
                return Decision("rollback", "REGRESSION_IN_ROUND",
                                f"这一轮把结果改坏了（{prev.status} → {last.status}），"
                                f"回滚到第 {(self.best or prev).attempt} 轮的最佳补丁。")

            # 下面两条都是"没进展就停"，只在【还没通过】时才有意义（见 is_passing）
            if not last.is_passing:
                if last.signature and last.signature == prev.signature:
                    return Decision("stop", "SAME_FAILURE_TWICE",
                                    "连续两轮的失败完全相同 —— 这一轮没有改变任何事实，"
                                    "继续在同一方向上试只会重复。")

                if last.score == prev.score and len(last.diff) > len(prev.diff) * 1.2:
                    return Decision("rollback", "DIFF_GROWING",
                                    "diff 在变大但失败数没减少 —— 这一轮是负收益，回滚。")

        if not self.narrowed and ledger.pressure() >= TOKEN_PRESSURE_LINE:
            self.narrowed = True
            # pressure() 的分母跟着结算单位走（token 或钱），这里的措辞也跟着
            # 中性化。代码 TOKEN_PRESSURE 保持不变 —— 它已经进了历史轨迹。
            return Decision("narrow", "TOKEN_PRESSURE",
                            f"预算已用掉 {ledger.pressure():.0%}，"
                            "关闭大范围探索，把剩余额度留给收尾。")

        return Decision("continue")

    # -- Reviewer 循环 ------------------------------------------------------
    def reviewer_repeats(self, verdict: dict) -> bool:
        """Reviewer 是不是又提了同一个意见。是 = 它不会被说服，别再烧一轮。"""
        key = "|".join(sorted(str(a) for a in verdict.get("requested_actions", [])))
        if not key:
            key = str(verdict.get("comments", ""))[:120]
        repeated = key in self.reviewer_requests
        self.reviewer_requests.append(key)
        return repeated

    # -- 记录 ---------------------------------------------------------------
    def note(self, decision: Decision, attempt: int) -> None:
        if decision.action != "continue":
            self.events.append({"attempt": attempt, "action": decision.action,
                                "code": decision.code, "reason": decision.reason})

    def to_dict(self) -> dict:
        best = self.best
        return {
            "checkpoints": [{"attempt": c.attempt, "status": c.status,
                             "failures": c.failures, "diff_bytes": len(c.diff)}
                            for c in self.history],
            "best_attempt": best.attempt if best else None,
            "events": self.events,
            "narrowed": self.narrowed,
        }


def _failure_count(report, comparison: dict) -> int:
    """失败数。解析不出数字时退回"新增失败名单长度"，再退回 exit_code。

    这里绝不能在解析失败时返回 0 —— 那会让一个跑挂的测试套件看起来像满分，
    最佳补丁就会选中一个把测试搞崩的版本（TestReport.parsed 存在的同一个理由）。
    """
    if report is not None and report.parsed:
        return report.failed + report.errors
    if comparison.get("new_failures"):
        return len(comparison["new_failures"])
    if report is not None:
        return 0 if report.green else 999
    return 999
