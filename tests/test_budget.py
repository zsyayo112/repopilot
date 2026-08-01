"""预算与账本：把"允许花多少"和"实际花了多少"分开，并且能对账。

守住的核心事实：两个 agent 版本的成功率只有在【同一预算】下才可比。
所以 Budget 必须可哈希（指纹）、可派生（消融只差一项）、可强制（超了就停）。
"""

from repopilot.budget import FULL, NO_EXPLORER, NO_REVIEWER, Budget, Ledger


class _Usage:
    """模仿 OpenAI 的 usage 对象。"""

    def __init__(self, prompt, completion, cached=0):
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        self.prompt_cache_hit_tokens = cached


def test_ablation_variants_differ_only_in_capability():
    """消融配置之间只能差能力开关 —— 规模和 token 必须完全一致。

    这条测试守的是整个消融实验的合法性：如果带 Reviewer 的那版顺便多拿了
    100k token，比出来的差值是钱的差，不是设计的差。
    """
    for variant in (NO_REVIEWER, NO_EXPLORER):
        for field in ("max_turns", "max_tool_calls", "max_fix_attempts",
                      "max_test_runs", "token_budget", "instance_timeout",
                      "model", "temperature"):
            assert getattr(variant, field) == getattr(FULL, field), \
                f"{field} 在消融变体里被改动了 —— 那就不是只差一项的对比了"
    assert NO_REVIEWER.allow_reviewer is False
    assert NO_EXPLORER.allow_explorer is False


def test_fingerprint_changes_when_any_field_changes():
    a = Budget()
    assert a.fingerprint() == Budget().fingerprint()
    assert a.fingerprint() != a.variant(token_budget=1).fingerprint()
    # 用和默认值相反的方向翻转 —— 测试守的是'开关变了指纹必须变'，
    # 不是某个具体默认值
    assert a.fingerprint() != a.variant(allow_reviewer=not a.allow_reviewer).fingerprint()


def test_ledger_stops_the_run_when_the_budget_is_gone():
    """超支不靠模型自觉，靠 exhausted() 返回一个非空的原因码。"""
    led = Ledger(budget=Budget(token_budget=1000, max_tool_calls=3))
    assert led.exhausted() is None

    led.record_usage(_Usage(900, 50))
    assert led.exhausted() is None          # 950 < 1000
    led.record_usage(_Usage(60, 10))
    assert led.exhausted() == "TOKEN_LIMIT"

    led2 = Ledger(budget=Budget(max_tool_calls=2))
    led2.note_tool_call()
    led2.note_tool_call()
    assert led2.exhausted() == "TOOL_CALL_LIMIT"


def test_cached_input_tokens_are_priced_lower():
    """缓存命中价单列不是抠门：多轮 agent 的前缀高度重复，

    把命中当未命中算会把成本高估好几倍 —— 而成本是这份评测要报的核心数字之一。
    """
    hit = Ledger(budget=Budget(model="deepseek-chat"))
    hit.record_usage(_Usage(100_000, 0, cached=100_000))
    miss = Ledger(budget=Budget(model="deepseek-chat"))
    miss.record_usage(_Usage(100_000, 0, cached=0))
    assert hit.cost_usd() < miss.cost_usd()


def test_unknown_model_reports_no_price_instead_of_zero():
    """没有价格表就说没有，不要报 $0.0000 —— 那和"免费"长得一模一样。"""
    led = Ledger(budget=Budget(model="some-new-model"))
    led.record_usage(_Usage(10_000, 1_000))
    assert led.to_dict()["priced"] is False
    assert "无价格表" in led.summary()


def test_tokens_are_booked_by_role():
    """Reviewer 到底多花了多少 —— 只有分角色记账才答得出来。"""
    led = Ledger(budget=Budget())
    led.record_usage(_Usage(1000, 100), role="executor")
    led.record_usage(_Usage(500, 50), role="reviewer")
    assert led.to_dict()["tokens_by_role"] == {"executor": 1100, "reviewer": 550}


def test_pressure_drives_the_narrowing_rule():
    led = Ledger(budget=Budget(token_budget=10_000))
    led.record_usage(_Usage(8_000, 100))
    assert led.pressure() >= 0.8
