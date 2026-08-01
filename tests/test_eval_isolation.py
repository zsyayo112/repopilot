"""推理侧与判分侧那道墙的守卫测试。

这些测试的存在理由很直白：**上一版的泄露不是恶意，是顺手。**
test_patch 就在手边的字典里，抠一下就能把测试范围缩小到笔记本跑得动。
顺手是必然会发生的，所以边界必须由机器来守。

三道防线，从静态到动态：
  1) AST 静态检查：推理侧的源码里根本不许出现判分侧的名字
  2) 运行时断言：真正发给 agent 的 payload 过一遍禁词
  3) 测试命令自查：命令里出现用例级 id（`::`）就说明它来自 F2P/P2P
"""

import ast
import sys
from pathlib import Path

import pytest

EVAL = Path(__file__).resolve().parents[1] / "eval"
sys.path.insert(0, str(EVAL))

from harness.environment import assert_command_public, derive_test_command  # noqa: E402
from harness.instances import (  # noqa: E402
    LeakError,
    PublicInstance,
    SecretInstance,
    assert_public_only,
    scan_for_leak,
)

# 推理侧的每一个模块。改动这份名单前先想清楚：新模块凭什么可以看答案。
INFERENCE_MODULES = ["harness/inference.py", "harness/environment.py",
                     "harness/baselines.py", "harness/instances.py"]

FORBIDDEN_IMPORTS = {"grader"}
FORBIDDEN_NAMES = {"SecretInstance", "load_secret", "FAIL_TO_PASS", "PASS_TO_PASS",
                   "test_patch", "gold_patch", "hints_text"}


def _imported_names(tree: ast.AST) -> set[str]:
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[-1] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module.split(".")[-1])
            names |= {a.name for a in node.names}
    return names


@pytest.mark.parametrize("rel", INFERENCE_MODULES)
def test_inference_side_never_imports_the_answers(rel):
    """推理侧不许 import grader，也不许 import SecretInstance / load_secret。

    instances.py 定义了 SecretInstance（判分侧要用），所以它豁免"定义"，
    但同样不许 import grader —— 定义一个类和使用一个类是两回事。
    """
    src = (EVAL / rel).read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported = _imported_names(tree)

    assert not (imported & FORBIDDEN_IMPORTS), \
        f"{rel} import 了判分侧模块：{imported & FORBIDDEN_IMPORTS}"

    if rel != "harness/instances.py":
        leaked = imported & {"SecretInstance", "load_secret"}
        assert not leaked, f"{rel} import 了判分侧类型：{leaked}"


@pytest.mark.parametrize("rel", ["harness/inference.py", "harness/environment.py",
                                 "harness/baselines.py"])
def test_inference_side_never_mentions_secret_fields(rel):
    """连字段名都不许出现在【代码】里。

    只扫代码，不扫注释和字符串字面量：文件头那段"绝不能从 test_patch 抠路径"
    的说明本身就要提到这个词，把注释也一起禁掉就没法解释为什么要禁了。
    """
    tree = ast.parse((EVAL / rel).read_text(encoding="utf-8"))
    hits = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            hits.add(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_NAMES:
            hits.add(node.attr)
    assert not hits, f"{rel} 的代码里出现了判分侧字段：{hits}"


def test_public_instance_carries_no_answers():
    """PublicInstance 的字段就那几个 —— 从数据集原始行里挑，不是过滤掉几个。"""
    row = {"instance_id": "x__y-1", "repo": "x/y", "base_commit": "abc",
           "problem_statement": "issue text", "environment_setup_commit": "def",
           "patch": "GOLD", "test_patch": "TESTS",
           "FAIL_TO_PASS": '["t::a"]', "PASS_TO_PASS": '["t::b"]',
           "hints_text": "HINT"}
    pub = PublicInstance.from_row(row)
    blob = str(pub.to_dict())
    for secret in ("GOLD", "TESTS", "t::a", "t::b", "HINT"):
        assert secret not in blob, f"PublicInstance 里漏出了 {secret}"
    assert set(pub.to_dict()) == {"instance_id", "repo", "base_commit",
                                  "problem_statement", "environment_setup_commit"}


def test_assert_public_only_rejects_secret_fields():
    with pytest.raises(LeakError):
        assert_public_only({"instance": {"instance_id": "a"},
                            "FAIL_TO_PASS": ["t::a"]})
    assert_public_only({"instance": {"instance_id": "a"}, "test_cmd": "pytest tests"})


def test_derived_test_command_is_directory_scoped_not_case_scoped():
    """测试命令只能到【目录】粒度。到用例粒度就说明它来自 F2P/P2P。"""
    cmd = derive_test_command("/v/bin/python", ["tests", "testing"])
    assert "tests" in cmd and "::" not in cmd
    assert_command_public(cmd)          # 不该抛

    with pytest.raises(LeakError):
        assert_command_public("/v/bin/python -m pytest tests/test_x.py::test_y")
    with pytest.raises(LeakError):
        assert_command_public("pytest $FAIL_TO_PASS")


def test_leak_scan_finds_official_test_ids_in_a_trace():
    """事后扫描：轨迹里出现隐藏测试的 id 会被如实记录（记录，不是判违规）。"""
    secret = SecretInstance(
        instance_id="x__y-1", test_patch="diff --git a/tests/test_config.py b/tests/test_config.py\n",
        fail_to_pass=["tests/test_config.py::test_from_file_toml"],
        pass_to_pass=[])
    clean = scan_for_leak({"issue.md": "the config loader crashes on toml"}, secret)
    assert clean == []

    dirty = scan_for_leak(
        {"trace.json": "let me run tests/test_config.py::test_from_file_toml"}, secret)
    assert len(dirty) == 1 and dirty[0]["where"] == "trace.json"


def test_leak_scan_separates_real_leaks_from_name_collisions(tmp_path):
    """两种命中说的是完全不同的事，不能混算：

      input 级  我们【喂给】agent 的东西里出现了隐藏测试标识 → 真泄露，必须是 0
      trace 级  agent 【自己产出】的东西里出现了 → 多半是巧合，只作记录

    实测撞到过的那个巧合：agent 自己补了一个测试，随手起名
    test_empty_name_not_allowed，正好和官方 FAIL_TO_PASS 同名 ——
    两边都取了最自然的名字而已。不分级的话，这种巧合会被记成"疑似泄露"，
    真泄露反而淹没在里面。
    """
    from harness.grader import audit_inference

    secret = SecretInstance(
        instance_id="x", test_patch="",
        fail_to_pass=["tests/test_blueprints.py::test_empty_name_not_allowed"],
        pass_to_pass=[])

    (tmp_path / "issue.md").write_text("Blueprint 名称为空时没有报错", encoding="utf-8")
    (tmp_path / "solve_result.json").write_text(
        '{"public_api_changed": ["tests/test_blueprints.py::test_empty_name_not_allowed"]}',
        encoding="utf-8")

    found = audit_inference(tmp_path, secret)
    assert [f["severity"] for f in found] == ["trace"]

    # 真泄露：把官方用例 id 写进了发给 agent 的输入
    (tmp_path / "inference_input.json").write_text(
        '{"test_cmd": "pytest tests/test_blueprints.py::test_empty_name_not_allowed"}',
        encoding="utf-8")
    severities = {f["severity"] for f in audit_inference(tmp_path, secret)}
    assert "input" in severities


def test_secret_tokens_skips_words_too_common_to_be_evidence():
    """扫描词必须足够独特。拿 'tests' 去扫会把所有实例都报成泄露。"""
    secret = SecretInstance(instance_id="i", test_patch="",
                            fail_to_pass=["t.py::a"], pass_to_pass=[])
    assert all(len(t) > 8 for t in secret.secret_tokens())
