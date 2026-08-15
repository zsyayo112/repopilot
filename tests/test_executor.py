"""Executor 的重复输出断路器。

T=0 下模型的典型病理：同一段分析一字不差地重复几十遍（实测 testify#1785
一次 completion 里重复了几十遍、8k 输出额度烧完，最后零改动"完成"）。
停机策略的规则对它全部免疫 —— 不调工具、不改文件、失败指纹不变。
断路器必须装在 executor 层：检测 → 截断 → 纠偏 → 两次无效就放弃。
"""

from repopilot import executor
from repopilot.executor import SPIN_FEEDBACK, detect_spin
from repopilot.permissions import Permissions
from repopilot.trace import Trace

PARA = "Actually, let me look at the issue from a completely different angle. " \
       "Maybe the problem is in how the arguments are compared."


# ---------------------------------------------------------------------------
# detect_spin：纯函数判据
# ---------------------------------------------------------------------------
def test_third_verbatim_repeat_is_spin():
    content = "\n\n".join(["前情提要", PARA, "中间还想了点别的", PARA, PARA, "后面全是空转"])
    truncated = detect_spin(content)
    assert truncated is not None
    assert truncated.count(PARA) == 2, "截断到复发点：保留绕圈的证据，丢掉空转"
    assert "后面全是空转" not in truncated


def test_short_repeats_are_not_spin():
    """短句（"OK."、代码片段的重复行）天然会重复，不构成打转。"""
    assert detect_spin("\n\n".join(["OK.", "OK.", "OK.", "OK."])) is None


def test_normal_analysis_is_not_spin():
    assert detect_spin("\n\n".join(f"第 {i} 步：{PARA}{i}" for i in range(10))) is None


def test_whitespace_variants_count_as_the_same_paragraph():
    """缩进/换行不同的同一段话是同一段话 —— 归一后再数。"""
    variants = [PARA, PARA.replace(" ", "  "), "  " + PARA.replace(". ", ".\n")]
    assert detect_spin("\n\n".join(variants)) is not None


# ---------------------------------------------------------------------------
# run_executor：断路器的干预回路
# ---------------------------------------------------------------------------
class _ToolkitStub:
    def specs(self):
        return []


def _run(monkeypatch, tmp_path, replies):
    """把 _stream_once 换成脚本化的回复序列，跑一遍 run_executor。"""
    it = iter(replies)
    monkeypatch.setattr(executor, "_stream_once",
                        lambda *a, **k: (next(it), [], 100))
    messages = [{"role": "user", "content": "去修 issue"}]
    summary, _ = executor.run_executor(
        _ToolkitStub(), Permissions(trust_all=True), messages,
        Trace(tmp_path / "run"))
    return summary, messages


def test_spin_without_tools_gets_corrected_not_returned(monkeypatch, tmp_path):
    """打转 + 不调工具 ≠ 总结完收工：喂回纠偏指令，再给一次机会。"""
    spin = "\n\n".join([PARA] * 5)
    summary, messages = _run(monkeypatch, tmp_path, [spin, "改完了，测试绿了。"])

    assert summary == "改完了，测试绿了。"
    assert any(m["role"] == "user" and m["content"] == SPIN_FEEDBACK
               for m in messages), "必须注入纠偏指令"
    spun = next(m for m in messages if m["role"] == "assistant")
    assert "[输出在此被截断" in spun["content"], "重复文本不能原样喂回上下文"


def test_persistent_spin_gives_up_after_two_strikes(monkeypatch, tmp_path):
    spin = "\n\n".join([PARA] * 5)
    summary, _ = _run(monkeypatch, tmp_path, [spin, spin, "不该走到第三次"])
    assert "重复输出" in summary and "停止" in summary
