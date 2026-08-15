"""上下文压缩：历史只增不减是首轮评测测出的头号瓶颈。

数据：13 条实例里 6 条 agent 从头到尾没改过一行代码，预算烧光时还在读文件。
完整版平均 54.5 万 token（9 条里 6 条撞上限），而朴素 ReAct 用 27 万
解出了同样的 4 条。

这些测试守的是压缩的三条不变量，每一条都对应一种"压了但压坏了"的失败。
"""

from repopilot.compaction import (
    KEEP_RECENT_CHARS,
    compact,
    context_chars,
    should_compact,
)


def _history(n_tools: int, size: int = 2000) -> list:
    """造一段真实形状的历史：system + user + (assistant带tool_calls + tool) × n。"""
    msgs = [{"role": "system", "content": "你是一个 agent"},
            {"role": "user", "content": "## Issue\n修复这个 bug\n" + "背景 " * 100}]
    for i in range(n_tools):
        msgs.append({"role": "assistant", "content": f"我来读第 {i} 个文件",
                     "tool_calls": [{"id": f"c{i}", "type": "function",
                                     "function": {"name": "read_file",
                                                  "arguments": '{"path":"a.py"}'}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "x" * size})
    return msgs


def test_compaction_preserves_tool_call_pairing():
    """**最容易踩的坑**：删掉一条 tool 消息，API 会直接报配对错误。

    所以压缩只改 content，绝不删消息、绝不动 assistant 的 tool_calls。
    """
    msgs = _history(20)
    before_roles = [m["role"] for m in msgs]
    before_ids = [m.get("tool_call_id") for m in msgs]

    compact(msgs)

    assert [m["role"] for m in msgs] == before_roles
    assert [m.get("tool_call_id") for m in msgs] == before_ids
    # 每个 assistant 的 tool_calls 都还能找到对应的 tool 消息
    for m in msgs:
        for tc in (m.get("tool_calls") or []):
            assert any(x.get("tool_call_id") == tc["id"] for x in msgs)


def test_system_prompt_and_the_task_itself_are_never_touched():
    """任务本身（issue + 计划 + 基线）丢了就跑偏了，它不参与压缩。"""
    msgs = _history(20)
    system, task = msgs[0]["content"], msgs[1]["content"]
    compact(msgs)
    assert msgs[0]["content"] == system
    assert msgs[1]["content"] == task


def test_recent_tool_results_survive_verbatim():
    """最近几次工具返回是【当前正在处理的证据】，压掉它等于让 agent 失忆。

    保护窗口按【字符预算】：size=2000 时预算罩住最近 KEEP_RECENT_CHARS/2000 条。
    """
    msgs = _history(20)
    compact(msgs)
    tools = [m for m in msgs if m["role"] == "tool"]
    guard = KEEP_RECENT_CHARS // 2000
    for m in tools[-guard:]:
        assert not m["content"].startswith("[已省略"), "预算内的工具返回不该被压缩"
    assert any(m["content"].startswith("[已省略") for m in tools[:-guard])


def test_protection_window_narrows_when_results_are_big():
    """字符预算的意义所在：返回值越大，按条数算的保护窗口越窄。

    按【条数】保留 6 条整段 diff 会守住 5 万多字符 —— 窗口大小完全失控，
    这正是 testify#1785 里关键证据反被挤出去的机制。
    """
    msgs = _history(20, size=9000)
    compact(msgs)
    tools = [m for m in msgs if m["role"] == "tool"]
    intact = [m for m in tools if not m["content"].startswith("[已省略")]
    assert len(intact) == 2      # ceil(16000/9000)：预算只罩得住最近两条


def test_folded_placeholder_keeps_the_key_lines():
    """折叠不是销毁：diff hunk 头、测试失败行这些"要点"要留在占位符里。

    testify#1785 的教训：模型刚拿到 v1.10..v1.11 的 diff 就被折叠，
    只记得自己的【错误结论】，证据没了，只好全价重读。
    """
    msgs = _history(20)
    msgs[7]["content"] = ("diff --git a/mock.go b/mock.go\n"
                          + "x" * 500
                          + "\n@@ -938,6 +948,8 @@ func Diff\n"
                          + "y" * 500
                          + "\n--- FAIL: TestArgumentMatcher\n" + "z" * 500)
    compact(msgs)
    assert msgs[7]["content"].startswith("[已省略")
    assert "@@ -938,6 +948,8 @@" in msgs[7]["content"]
    assert "--- FAIL: TestArgumentMatcher" in msgs[7]["content"]


def test_assistant_reasoning_survives():
    """模型自己的结论比它读过的原始内容值钱得多。

    "问题在 config.py 第 48 行"这句话要留，为得到它读过的四千字符不用留。
    """
    msgs = _history(20)
    compact(msgs)
    for m in msgs:
        if m["role"] == "assistant":
            assert m["content"].startswith("我来读第")


def test_placeholder_says_which_tool_so_it_can_be_re_fetched():
    msgs = _history(20)
    compact(msgs)
    folded = next(m for m in msgs if m["role"] == "tool"
                  and m["content"].startswith("[已省略"))
    assert "read_file" in folded["content"]


def test_short_results_are_left_alone():
    """压缩短返回值省不下什么，却平白丢信息。"""
    msgs = _history(20, size=50)
    evicted, _ = compact(msgs)
    assert evicted == 0


def test_compaction_actually_shrinks_and_is_idempotent():
    msgs = _history(30)
    before = context_chars(msgs)
    evicted, saved = compact(msgs)
    assert evicted > 0 and saved > 0
    assert context_chars(msgs) < before / 2

    # 再压一次不该重复折叠已经折叠过的（否则占位符会层层嵌套）
    again, _ = compact(msgs)
    assert again == 0


def test_nothing_happens_when_history_is_still_short():
    msgs = _history(6)      # 6 × 2000 = 12000 字符，在保护预算内
    assert compact(msgs) == (0, 0)


def test_threshold_is_measured_not_guessed():
    """触发线用【真实测得的 prompt_tokens】，不自己估算 —— 估算通常低估。"""
    assert not should_compact(1000)
    assert should_compact(30_000)
