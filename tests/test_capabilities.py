"""工具分组暴露、不可逆意图拦截、证据脱敏、Scenario 校验。

这四件事的共同点：都是【护栏】。护栏必须有测试守着 —— 软约束才靠祈祷。
"""

import json
import subprocess

import pytest

from repopilot.evidence import EvidenceCollector, diff_evidence, redact, redact_headers
from repopilot.policy import looks_irreversible
from repopilot.scenario import Scenario
from repopilot.tools import TOOL_GROUPS, ToolKit, is_safe
from repopilot.workspace import Workspace


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "hello.py").write_text("def greet():\n    return 'hi'\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=t@t",
                    "-c", "user.name=t", "commit", "-qm", "init"], check=True)
    return tmp_path


# --------------------------------------------------------------- 工具分组
def test_default_exposes_only_core_tools(repo):
    """给一个纯 Python 库任务塞九个浏览器工具，它会真的去试着开浏览器。"""
    kit = ToolKit(Workspace(repo))
    names = {t["function"]["name"] for t in kit.specs()}
    assert names == set(TOOL_GROUPS["core"])
    assert "browser_open" not in names
    assert "start_services" not in names
    assert len(names) < len(TOOL_GROUPS["core"]) + len(TOOL_GROUPS["runtime"])


def test_runtime_group_adds_service_tools(repo):
    kit = ToolKit(Workspace(repo), groups=("core", "runtime"))
    names = {t["function"]["name"] for t in kit.specs()}
    assert "start_services" in names
    assert "browser_open" not in names        # browser 还是没开


def test_disabled_tool_is_refused_with_an_explanation(repo):
    """模型凭记忆调了个没开的工具时，要告诉它为什么，别只说"不存在"。"""
    kit = ToolKit(Workspace(repo))
    out = kit.execute("browser_open", {"url": "http://127.0.0.1:3000"})
    assert "未启用" in out and "browser" in out


def test_unknown_tool_still_returns_a_string(repo):
    """工具层永远不抛异常 —— 一抛主循环就断、消息配对就破。"""
    assert "不存在" in ToolKit(Workspace(repo)).execute("no_such_tool", {})


def test_bad_arguments_return_the_signature_hint(repo):
    kit = ToolKit(Workspace(repo))
    assert "参数不对" in kit.execute("read_file", {"nope": 1})


def test_cleanup_is_safe_when_nothing_started(repo):
    assert "没有需要清理" in ToolKit(Workspace(repo)).cleanup()


# --------------------------------------------------- 不可逆意图（浏览器交互）
def test_ordinary_click_is_auto_approved():
    """页面上乱点通常无害，每点一下都问人的话根本没法用。"""
    assert is_safe("browser_click", {"ref": "e3", "name": "查看详情"}) is True


@pytest.mark.parametrize("name", [
    "确认支付", "立即付款", "删除账户", "提交订单", "Delete project", "Checkout",
])
def test_irreversible_click_needs_confirmation(name):
    """点下去就回不来的那类必须回到人手上。"""
    assert is_safe("browser_click", {"ref": "e9", "name": name}) is False
    assert looks_irreversible(name)


def test_scenario_with_irreversible_step_needs_confirmation():
    """复现脚本里藏一步付款也要拦 —— 不只看单次点击。"""
    payload = json.dumps({"steps": [{"action": "click", "name": "确认支付"}]})
    assert is_safe("run_scenario", {"scenario_json": payload}) is False


def test_write_tools_are_never_auto_approved():
    assert is_safe("edit_file", {"path": "a.py"}) is False
    assert is_safe("run_bash", {"command": "ls"}) is False


# --------------------------------------------------------------- 证据脱敏
def test_redact_scrubs_tokens_before_they_hit_disk():
    """已经落盘的秘密就已经泄漏了 —— 脱敏必须发生在写入前。"""
    assert "sk-abcdef123456" not in redact("key=sk-abcdef123456 done")
    assert "ghp_abcdef123456" not in redact("token ghp_abcdef123456")
    out = redact("GET /api/x?token=supersecretvalue&page=2")
    assert "supersecretvalue" not in out
    assert "page=2" in out                     # 无关参数不该被抹掉


def test_redact_headers_masks_by_key_name():
    headers = redact_headers({"Authorization": "Bearer xyz", "Accept": "text/html"})
    assert "xyz" not in headers["Authorization"]
    assert headers["Accept"] == "text/html"


def test_collector_only_keeps_errors_not_noise():
    """一个 SPA 首屏能有两百条正常请求，全记等于把上下文预算烧光。"""
    ev = EvidenceCollector()
    ev.start_recording("before")
    ev.add_console("log", "just chatting")
    ev.add_console("error", "TypeError: x is not a function")
    ev.add_network("GET", "http://127.0.0.1:3000/ok", 200)
    ev.add_network("POST", "http://127.0.0.1:3000/api/order", 500)
    ev.add_network("GET", "http://127.0.0.1:3000/gone", None, "ECONNREFUSED")
    assert len(ev.console) == 1
    assert len(ev.network) == 2
    assert not ev.clean


def test_evidence_diff_reports_appeared_and_gone_separately():
    """数量相同可能是"修好一个又坏一个" —— 所以比集合差，不比计数。"""
    before, after = EvidenceCollector(), EvidenceCollector()
    before.add_console("error", "old boom")
    after.add_console("error", "new boom")
    out = diff_evidence(before, after)
    assert "old boom" in out and "已消失" in out
    assert "new boom" in out and "新出现" in out


def test_evidence_saved_file_contains_no_secret(tmp_path):
    ev = EvidenceCollector()
    ev.start_recording("before")
    ev.add_network("GET", "http://127.0.0.1/api?api_key=topsecretkey", 401)
    path = ev.save(tmp_path, "before")
    assert "topsecretkey" not in path.read_text(encoding="utf-8")


# --------------------------------------------------------------- Scenario
def test_scenario_requires_assertions():
    """没有断言的复现脚本证明不了任何事 —— 直接拒绝，别让它假装成证据。"""
    with pytest.raises(ValueError, match="断言"):
        Scenario.from_dict({"name": "x", "steps": [{"action": "open", "url": "/"}]})


def test_scenario_requires_steps():
    with pytest.raises(ValueError, match="step"):
        Scenario.from_dict({"name": "x", "assertions": [{"type": "url", "value": "/"}]})


def test_scenario_rejects_unknown_action():
    with pytest.raises(ValueError, match="action"):
        Scenario.from_dict({"steps": [{"action": "hack_the_planet"}],
                            "assertions": [{"type": "url", "value": "/"}]})


def test_scenario_round_trips(tmp_path):
    """存下来能原样读回来 —— 修改前后跑的必须是同一份。"""
    data = {
        "name": "mobile-booking",
        "viewport": {"width": 390, "height": 844},
        "steps": [{"action": "open", "url": "/packages/123"},
                  {"action": "click", "role": "button", "name": "立即预订"}],
        "assertions": [{"type": "url", "value": "/orders/new"},
                       {"type": "no_console_errors"}],
    }
    path = tmp_path / "s.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    sc = Scenario.load(path)
    assert sc.name == "mobile-booking"
    assert sc.viewport["width"] == 390
    assert len(sc.steps) == 2 and len(sc.assertions) == 2
    assert Scenario.from_dict(sc.to_dict()).to_dict() == sc.to_dict()


def test_scenario_accepts_matches_as_alias_for_value():
    """笔记里写的是 "matches"，实现用 "value" —— 两种都收，别让人踩这个坑。"""
    sc = Scenario.from_dict({
        "steps": [{"action": "open", "url": "/"}],
        "assertions": [{"type": "url", "matches": "/orders/new"}]})
    assert sc.assertions[0].value == "/orders/new"
