"""测试输出解析：每种技术栈一组真实输出样本。

这些样本是从真实工具输出里剪下来的。解析器最容易在两处翻车，所以每处都有专门的
用例守着：
  1) 把"没解析出来"伪装成"0 个失败"（parsed 标志就是为它存在的）
  2) 把编译/收集阶段的失败当成"0 个失败"（一个用例都没跑起来，比失败更严重）
"""

from repopilot.adapters.go_stack import GoAdapter
from repopilot.adapters.java_stack import MavenAdapter
from repopilot.adapters.node_stack import NodeAdapter
from repopilot.adapters.python_stack import PythonAdapter
from repopilot.adapters.ruby_stack import RubyAdapter
from repopilot.adapters.rust_stack import RustAdapter

# ---------------------------------------------------------------- pytest


def test_pytest_counts(tmp_path):
    out = """
tests/test_a.py ..F                                                      [ 60%]
=================================== FAILURES ===================================
FAILED tests/test_a.py::test_three - assert 1 == 2
=========================== short test summary info ============================
================== 2 passed, 1 failed, 1 skipped in 0.42s ==================
"""
    r = PythonAdapter(tmp_path).parse_test_output(out, 1)
    assert r.parsed is True
    assert (r.passed, r.failed, r.skipped) == (2, 1, 1)
    assert r.failed_names == ["tests/test_a.py::test_three"]


def test_pytest_parametrized_id_with_space_is_not_truncated(tmp_path):
    """真实踩过的坑：按空格切分会把带空格的参数化用例 id 切成跑不起来的碎片。"""
    out = """
=========================== short test summary info ============================
FAILED tests/test_scale.py::test_convert[a b] - AssertionError
=============================== 1 failed in 0.1s ===============================
"""
    r = PythonAdapter(tmp_path).parse_test_output(out, 1)
    assert r.failed_names == ["tests/test_scale.py::test_convert[a b]"]


def test_pytest_collection_error_is_not_zero_failures(tmp_path):
    """收集阶段就炸了 = 一个用例都没跑。绝不能报成"0 失败"。"""
    out = """
ERROR tests/test_a.py
E   ModuleNotFoundError: No module named 'numpy'
!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
"""
    r = PythonAdapter(tmp_path).parse_test_output(out, 2)
    assert r.parsed is True
    assert r.errors >= 1 or r.failed >= 1
    assert not r.green


def test_pytest_summary_taken_from_the_last_line(tmp_path):
    """全文搜数字会命中被测代码自己打印的内容；必须只认结尾那行汇总。"""
    out = """
被测代码的日志: 99 passed, 88 failed
=========================== short test summary info ============================
============================== 3 passed in 0.05s ===============================
"""
    r = PythonAdapter(tmp_path).parse_test_output(out, 0)
    assert (r.passed, r.failed) == (3, 0)


# ---------------------------------------------------------------- jest / vitest


def _node(tmp_path, deps, scripts=None):
    return NodeAdapter(tmp_path, [], {"devDependencies": deps,
                                      "scripts": scripts or {"test": "x"}})


def test_jest_counts(tmp_path):
    out = """
  ✕ should reject empty name (5 ms)
  ✓ should accept a name (2 ms)
Test Suites: 1 failed, 3 passed, 4 total
Tests:       2 failed, 15 passed, 17 total
"""
    r = _node(tmp_path, {"jest": "^29"}).parse_test_output(out, 1)
    assert r.framework == "jest"
    assert (r.passed, r.failed) == (15, 2)
    assert r.failed_names == ["should reject empty name"]   # 耗时括号被去掉了


def test_jest_timing_suffix_stripped_so_names_are_comparable(tmp_path):
    """耗时每次都不一样，不剥掉会被 verifier 当成"新出现的失败"，误报回归。"""
    adapter = _node(tmp_path, {"jest": "^29"})
    a = adapter.parse_test_output("  ✕ flaky one (5 ms)\nTests: 1 failed, 0 passed", 1)
    b = adapter.parse_test_output("  ✕ flaky one (9 ms)\nTests: 1 failed, 0 passed", 1)
    assert a.failed_names == b.failed_names


def test_jest_suite_failed_to_run_counts_as_error(tmp_path):
    """import 挂了：Tests: 行可能根本不出现，但这是最严重的失败。"""
    out = """
● Test suite failed to run
  Cannot find module './missing'
Test Suites: 1 failed, 1 total
"""
    r = _node(tmp_path, {"jest": "^29"}).parse_test_output(out, 1)
    assert r.parsed is True
    assert r.errors == 1


def test_vitest_counts(tmp_path):
    out = """
 ❯ src/a.test.ts (3)
   × adds numbers
 Test Files  1 failed | 2 passed (3)
      Tests  2 failed | 15 passed (17)
"""
    r = _node(tmp_path, {"vitest": "^1"}).parse_test_output(out, 1)
    assert r.framework == "vitest"
    assert (r.passed, r.failed) == (15, 2)


def test_jest_json_report_is_preferred(tmp_path):
    """--json 输出应该被优先采用（换命令就自动变准，无需改代码）。"""
    out = ('{"numTotalTests":3,"numPassedTests":1,"numFailedTests":2,'
           '"numPendingTests":0,"numRuntimeErrorTestSuites":0,'
           '"testResults":[{"name":"a.test.ts","assertionResults":['
           '{"status":"failed","ancestorTitles":["Cart"],"title":"totals up"}]}]}')
    r = _node(tmp_path, {"jest": "^29"}).parse_test_output(out, 1)
    assert "json" in r.framework
    assert (r.passed, r.failed) == (1, 2)
    assert r.failed_names == ["Cart > totals up"]


def test_unknown_node_framework_admits_it_cannot_parse(tmp_path):
    """核心诉求：看不懂就说看不懂，不要报一堆 0 冒充成功。"""
    r = _node(tmp_path, {}, {"test": "node ./weird-runner.js"}).parse_test_output(
        "everything is fine, trust me", 0)
    assert r.parsed is False
    assert "未解析" in r.summary()


# ---------------------------------------------------------------- go


def test_go_text(tmp_path):
    out = """
--- PASS: TestAdd (0.00s)
--- FAIL: TestLogin (0.01s)
    login_test.go:22: want 200, got 500
FAIL	example.com/user	0.012s
"""
    r = GoAdapter(tmp_path).parse_test_output(out, 1)
    assert (r.passed, r.failed) == (1, 1)
    assert r.failed_names == ["TestLogin"]


def test_go_json_is_preferred(tmp_path):
    out = "\n".join([
        '{"Action":"pass","Package":"m/user","Test":"TestAdd"}',
        '{"Action":"fail","Package":"m/user","Test":"TestLogin"}',
        '{"Action":"fail","Package":"m/user"}',          # 包级汇总，不该重复计数
    ])
    r = GoAdapter(tmp_path).parse_test_output(out, 1)
    assert (r.passed, r.failed) == (1, 1)
    assert r.failed_names == ["m/user.TestLogin"]


def test_go_subtests_not_double_counted(tmp_path):
    """子测试是缩进的，只数顶层，否则父子重复计数。"""
    out = "--- FAIL: TestX (0.00s)\n    --- FAIL: TestX/case_a (0.00s)\n"
    r = GoAdapter(tmp_path).parse_test_output(out, 1)
    assert r.failed == 1


# ---------------------------------------------------------------- rust


def test_rust_sums_all_targets(tmp_path):
    """workspace 会输出多行 test result，只取第一行会严重少算。"""
    out = """
test result: ok. 5 passed; 0 failed; 1 ignored; 0 measured; 0 filtered out
failures:
    queries::tests::le_boundary
test result: FAILED. 2 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out
"""
    r = RustAdapter(tmp_path).parse_test_output(out, 101)
    assert (r.passed, r.failed, r.skipped) == (7, 1, 1)
    assert r.failed_names == ["queries::tests::le_boundary"]


# ---------------------------------------------------------------- java


def test_maven_text_takes_last_summary(tmp_path):
    out = """
Tests run: 5, Failures: 0, Errors: 0, Skipped: 0
Tests run: 17, Failures: 2, Errors: 0, Skipped: 1
"""
    r = MavenAdapter(tmp_path).parse_test_output(out, 1)
    assert (r.passed, r.failed, r.skipped) == (14, 2, 1)


def test_junit_xml_beats_text(tmp_path):
    """XML 比终端日志稳定得多：schema 二十年没变，日志格式随版本变。"""
    d = tmp_path / "target" / "surefire-reports"
    d.mkdir(parents=True)
    (d / "TEST-a.xml").write_text(
        '<testsuite tests="3" failures="1" errors="0" skipped="0">'
        '<testcase classname="com.x.CartTest" name="totals"><failure/></testcase>'
        '</testsuite>')
    r = MavenAdapter(tmp_path).parse_test_output("随便什么日志", 1)
    assert r.framework == "junit(xml)"
    assert (r.passed, r.failed) == (2, 1)
    assert r.failed_names == ["com.x.CartTest#totals"]


# ---------------------------------------------------------------- ruby


def test_rspec_counts(tmp_path):
    r = RubyAdapter(tmp_path).parse_test_output(
        "12 examples, 2 failures, 1 pending\n", 1)
    assert (r.passed, r.failed, r.skipped) == (9, 2, 1)


def test_minitest_counts(tmp_path):
    r = RubyAdapter(tmp_path).parse_test_output(
        "12 runs, 30 assertions, 2 failures, 1 errors, 1 skips\n", 1)
    assert r.framework == "minitest"
    assert (r.passed, r.failed, r.errors, r.skipped) == (8, 2, 1, 1)
