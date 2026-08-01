"""统一预算：把"这次运行被允许花多少"变成一个可序列化、可比较的对象。

【为什么这个文件必须存在】
两个 agent 版本的 resolved 率只有在【同一预算】下才可比。带 Reviewer 的版本
成功率更高，可能只是因为它多花了一倍 token —— 那不是设计更好，是买得更多。
所以预算不能散落在 config.py 的一堆常量里、更不能由每次运行随手改：
它必须是一个**对象**，能整体传递、能整体哈希、能整体写进 manifest。

Budget（冻结）= 允许花多少 —— 事前约定，运行期只读，进 manifest。
Ledger（可变）= 实际花了多少 —— 运行期累加，事后进报告。

两者分开的理由很实在：约定和实际必须能对账。只有一个"当前 token 数"的话，
你永远回答不了"这次跑超支了吗"。

【硬约束 vs 软约束，又一次】
"请节约使用 token" 写在提示词里是软约束，模型可以无视。
Ledger.exhausted() 在主循环每一圈都被检查，超了就停 —— 这是硬约束。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field, replace

from .config import MAX_FIX_ATTEMPTS, MAX_MODIFIED_FILES, MAX_TURNS, MODEL

# ---------------------------------------------------------------------------
# 价格表：$ / 1M tokens。(输入未命中缓存, 输出, 输入命中缓存)
# 缓存命中价单列是有意义的：多轮 agent 的 prompt 前缀高度重复，把命中当未命中
# 算会把成本高估好几倍，而成本是这份评测要报的核心数字之一。
# 价格会变 —— 它属于配置，不属于结论。改这里，别改报告。
# ---------------------------------------------------------------------------
PRICING: dict[str, tuple[float, float, float]] = {
    "deepseek-chat":     (0.27, 1.10, 0.07),
    "deepseek-reasoner": (0.55, 2.19, 0.14),
    "gpt-4o-mini":       (0.15, 0.60, 0.075),
    "gpt-4o":            (2.50, 10.00, 1.25),
}
_UNKNOWN_PRICE = (0.0, 0.0, 0.0)


@dataclass(frozen=True)
class Budget:
    """一次实例求解被允许消耗的全部资源。冻结 = 运行中途改不了。

    字段分三类，评测比较时【三类都必须相同】：
      能力类  allow_* —— 允许用哪些手段（有没有 Reviewer 是个能力差异，不是预算差异，
                        但它显著影响成本，所以放在同一个对象里一起冻结）
      规模类  max_*   —— 允许做多少次
      时间类  timeout —— 允许耗多久
    """

    # -- 模型 --
    model: str = MODEL
    temperature: float = 0.0

    # -- 规模 --
    max_turns: int = MAX_TURNS              # executor 单轮最多几圈
    max_tool_calls: int = 100               # 整个实例最多几次工具调用
    max_fix_attempts: int = MAX_FIX_ATTEMPTS  # 验证失败后最多重来几轮
    max_review_rounds: int = 1              # Reviewer 驳回后最多再修几轮
    max_test_runs: int = 15                 # 最多跑几次测试（防"改一行跑一次"刷时间）
    max_modified_files: int = MAX_MODIFIED_FILES
    max_api_retries: int = 4                # 瞬态错误重试次数

    # -- token --
    # 单次响应上限。8192 而不是 4096 是被基线 A 逼出来的：它必须在**一次**
    # 响应里吐完整个 unified diff，4096 会把它从中间切断，产出一个必然
    # `git apply` 失败的半截补丁。那样测出来的不是"它会不会修 bug"，
    # 是"它的答案短不短" —— 一个弱得可笑的基线会让主方案的数字虚高。
    # 上调是对【所有变体】一起调的：它是天花板不是目标，完整版单轮响应本来
    # 就远短于此，不受影响，而预算保持一致才谈得上可比。
    max_output_tokens: int = 8192
    # 整个实例的 token 总额（输入+输出）。
    # 【注意它统计的是"计费 token"，不是"上下文峰值"】每一轮都要把完整对话史
    # 重发一遍，所以 20 轮 × 15k 上下文 = 300k 计费 token，而峰值只有 15k。
    # 要控成本就得按计费口径算 —— 峰值再小，重发几十次一样要付钱。
    # 实测：flask 这类小仓库跑完一个实例约 26 万计费 token（其中九成命中缓存）。
    token_budget: int = 600_000

    # -- 成本（美元）--
    # 【为什么要有第二种结算单位】token 口径把缓存命中按全价记账，而首轮实测
    # 92% 的计费 token 是缓存命中，价格只有未命中的 1/4（0.07 vs 0.27）。
    # 结果就是：预算在一个几乎不要钱的资源上掐流量 —— 11 条实例里 7 条被
    # 600k 上限停下时，实际只花了 6 分钱。更糟的是它对变体还不公平：前缀越
    # 稳定的 agent 命中率越高，按 token 计价恰好把这项真实的效率优势抹掉。
    # 这个文件开头论证预算存在的那句话 ——"成功率高可能只是买得更多"——
    # 里的"买"，单位本来就是钱。
    #
    # 设了这个字段、且模型在价格表里，预算就按钱结算，token_budget 退出裁决
    # （一次运行只有一种货币，双重计量等于两个都说不清）。设了但模型没价格
    # 属于配置错误，由调用方（eval/run.py）在入口直接拒绝，而不是这里悄悄
    # 退回 token 口径 —— 悄悄换单位比报错糟得多。
    cost_budget_usd: float | None = None

    # -- 时间 --
    instance_timeout: int = 1800            # 单实例墙钟上限（秒），由外层 harness 执行

    # -- 能力开关 --
    allow_runtime: bool = False             # 允许起服务
    allow_browser: bool = False             # 允许开浏览器
    allow_explorer: bool = True             # 允许派探索子 agent
    # Reviewer 默认关。两次独立 sweep 的测量：它对 resolve 数贡献为零
    # （有它没它修好的题一样多），仅有的两次关键裁决都是误杀；它真正产出
    # 的是"声称可信度"（说修好了就没错过）—— 那只在补丁将被【自动合并、
    # 无人复查】时值钱。所以它是按需上岗的合并闸门（CLI --review），
    # 不是每次都跑的终审。消融变体不受此默认影响：budget_for 里逐项钉死。
    allow_reviewer: bool = False
    allow_halting_policy: bool = True       # 允许停机/回滚策略介入

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Budget":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

    def variant(self, **overrides) -> "Budget":
        """派生一个只改了指定字段的新预算 —— 消融实验就是靠它保证"只差这一项"。"""
        return replace(self, **overrides)

    def metered_in_usd(self) -> bool:
        """预算按不按钱结算。两件事同时成立才算：设了钱数、模型有价格表。"""
        return self.cost_budget_usd is not None and self.model in PRICING

    def fingerprint(self) -> str:
        """预算指纹。两次运行的指纹不同 = 结果不可直接比较，报告里必须写明。"""
        d = self.to_dict()
        if self.metered_in_usd():
            # 按钱结算时，价格表是预算语义的一部分：命中价从 0.07 变 0.10，
            # 同一个 $0.25 买到的轮数就不一样了。把生效价格并进指纹，价一变
            # 指纹就变，两次运行自动不可比。token 口径的运行不受价格影响，
            # 指纹也就不跟着价格变 —— 指纹只收"影响行为的东西"。
            d["pricing"] = PRICING[self.model]
        blob = json.dumps(d, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    def describe(self) -> str:
        caps = [n for n, on in (("runtime", self.allow_runtime),
                                ("browser", self.allow_browser),
                                ("explorer", self.allow_explorer),
                                ("reviewer", self.allow_reviewer),
                                ("halting", self.allow_halting_policy)) if on]
        money = (f"$≤{self.cost_budget_usd}（按钱结算，缓存命中按实价）"
                 if self.metered_in_usd() else f"tokens≤{self.token_budget:,}")
        return (f"{self.model} T={self.temperature} | "
                f"turns≤{self.max_turns} tools≤{self.max_tool_calls} "
                f"attempts≤{self.max_fix_attempts} tests≤{self.max_test_runs} | "
                f"{money} | {self.instance_timeout}s | "
                f"能力：{'+'.join(caps) or '无'}")


# 消融实验用的四个标准配置。它们【只在能力开关上不同】，规模和 token 完全一致 ——
# 这正是"统一预算后再比较"那条要求的代码表达：想证明 Reviewer 有用，
# 就不能让带 Reviewer 的那版顺便多拿 100k token。
FULL = Budget()
NO_REVIEWER = FULL.variant(allow_reviewer=False)
NO_EXPLORER = FULL.variant(allow_explorer=False)
NO_HALTING = FULL.variant(allow_halting_policy=False)


@dataclass
class Ledger:
    """实际花销账本。executor / planner / reviewer 每次调模型都往这里记一笔。"""

    budget: Budget = field(default_factory=Budget)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0        # 命中前缀缓存的输入 token（价格低一档）
    api_calls: int = 0
    tool_calls: int = 0
    test_runs: int = 0
    started_at: float = field(default_factory=time.time)
    # 分账：哪个角色花的。回答"Reviewer 到底加了多少成本"必须靠它。
    by_role: dict[str, int] = field(default_factory=dict)

    # -- 记账 ---------------------------------------------------------------
    def record_usage(self, usage, role: str = "executor") -> None:
        """从 OpenAI 风格的 usage 对象记一笔。usage 为 None 时安静跳过。"""
        if usage is None:
            return
        prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion = int(getattr(usage, "completion_tokens", 0) or 0)
        cached = 0
        details = getattr(usage, "prompt_tokens_details", None)
        if details is not None:
            cached = int(getattr(details, "cached_tokens", 0) or 0)
        # DeepSeek 用的是平铺字段名
        cached = cached or int(getattr(usage, "prompt_cache_hit_tokens", 0) or 0)

        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.cached_tokens += min(cached, prompt)
        self.api_calls += 1
        self.by_role[role] = self.by_role.get(role, 0) + prompt + completion

    def note_tool_call(self) -> None:
        self.tool_calls += 1

    def note_test_run(self) -> None:
        self.test_runs += 1

    # -- 查账 ---------------------------------------------------------------
    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def elapsed(self) -> float:
        return time.time() - self.started_at

    def pressure(self) -> float:
        """预算用掉了几成。0.8 是"禁止大范围探索"的触发线（见 halting.py）。

        分子分母跟着结算单位走：按钱结算就是 花的钱/钱的预算。单位换了
        触发线不换 —— 0.8 的含义始终是"还剩两成，省着花"。
        """
        if self.budget.metered_in_usd():
            if self.budget.cost_budget_usd <= 0:
                return 0.0
            return self.cost_usd() / self.budget.cost_budget_usd
        if self.budget.token_budget <= 0:
            return 0.0
        return self.total_tokens / self.budget.token_budget

    def exhausted(self) -> str | None:
        """超支了吗？返回超支原因（可直接当失败分类码），没超返回 None。"""
        if self.budget.metered_in_usd():
            # 按钱结算：缓存命中按 0.07 而不是 0.27 记账。token_budget 在这个
            # 口径下【不再裁决】—— 一次运行只有一种货币。失控由别的闸兜着：
            # 每次调用都花钱且钱只增不减，加上 max_turns / max_tool_calls /
            # instance_timeout 三道独立的闸。
            if self.cost_usd() >= self.budget.cost_budget_usd:
                return "COST_LIMIT"
        elif self.total_tokens >= self.budget.token_budget:
            return "TOKEN_LIMIT"
        if self.tool_calls >= self.budget.max_tool_calls:
            return "TOOL_CALL_LIMIT"
        if self.elapsed >= self.budget.instance_timeout:
            return "TIMEOUT"
        return None

    def cost_usd(self) -> float:
        miss_price, out_price, hit_price = PRICING.get(self.budget.model, _UNKNOWN_PRICE)
        miss = max(self.prompt_tokens - self.cached_tokens, 0)
        return (miss * miss_price + self.cached_tokens * hit_price
                + self.completion_tokens * out_price) / 1_000_000

    def to_dict(self) -> dict:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cached_tokens": self.cached_tokens,
            "total_tokens": self.total_tokens,
            "api_calls": self.api_calls,
            "tool_calls": self.tool_calls,
            "test_runs": self.test_runs,
            "elapsed_secs": round(self.elapsed, 1),
            "cost_usd": round(self.cost_usd(), 6),
            "priced": self.budget.model in PRICING,
            "tokens_by_role": dict(self.by_role),
        }

    def summary(self) -> str:
        d = self.to_dict()
        price = f"${d['cost_usd']:.4f}" if d["priced"] else "(该模型无价格表)"
        return (f"{d['total_tokens']:,} tokens（缓存命中 {d['cached_tokens']:,}）| "
                f"{d['tool_calls']} 次工具 | {d['test_runs']} 次测试 | "
                f"{d['elapsed_secs']}s | {price}")
