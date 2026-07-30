"""Runtime Manager：起真实进程的测试。

这里【不打桩】—— 用 `python -m http.server` 起一个真的 HTTP 服务。理由是这个
模块的全部价值都在"和真实进程打交道"这件事上：健康检查、进程组、端口占用、
超时。全打成 mock 就等于什么都没测。
"""

import socket
import sys

import pytest

from repopilot.adapters.base import ServiceSpec
from repopilot.runtime import RuntimeManager, child_env, port_in_use


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def http_service(tmp_path):
    port = free_port()
    spec = ServiceSpec(
        name="web", command=f"{sys.executable} -m http.server {port}",
        port=port, health_path="/", startup_timeout=30)
    manager = RuntimeManager(tmp_path, [spec])
    yield manager, port
    manager.stop_all()


def test_start_waits_until_actually_serving(http_service):
    """核心认知：进程活着 ≠ 服务可用。start_all 必须等到健康检查真的通过。"""
    manager, port = http_service
    ok, detail = manager.start_all()
    assert ok, detail
    assert manager.services["web"].state == "READY"
    assert port_in_use(port)          # 返回之后端口一定已经在监听
    assert "就绪" in detail


def test_stop_frees_the_port(http_service):
    """漏关的 dev server 会一直占着端口，下一次任务直接起不来。"""
    manager, port = http_service
    assert manager.start_all()[0]
    manager.stop_all()
    assert manager.services["web"].state == "STOPPED"
    assert not port_in_use(port)


def test_stop_all_is_idempotent(http_service):
    """它会被 finally 调用，而 finally 可能在任何奇怪的时刻触发。"""
    manager, _ = http_service
    manager.start_all()
    manager.stop_all()
    manager.stop_all()            # 重复调用不能抛异常
    assert manager.services["web"].state == "STOPPED"


def test_port_conflict_is_reported_as_environment_problem(tmp_path):
    """端口被别人占着时，健康检查会连上【别人的】服务 —— 必须提前拦住。"""
    port = free_port()
    with socket.socket() as squatter:
        squatter.bind(("127.0.0.1", port))
        squatter.listen(1)

        spec = ServiceSpec(name="web", command="sleep 60", port=port)
        manager = RuntimeManager(tmp_path, [spec])
        ok, detail = manager.start_all()
        assert not ok
        assert "已被占用" in detail
        assert "不是代码问题" in detail


def test_failed_start_is_detected_not_waited_out(tmp_path):
    """命令立刻退出时不该傻等到超时 —— poll() 一发现退出就该判失败。"""
    spec = ServiceSpec(name="web", command="exit 3", port=free_port(),
                       startup_timeout=30)
    manager = RuntimeManager(tmp_path, [spec])
    ok, detail = manager.start_all()
    assert not ok
    assert "进程已退出" in detail


def test_ready_pattern_works_without_a_port(tmp_path):
    """没有 HTTP 端口的服务（worker）靠日志正则判就绪。"""
    spec = ServiceSpec(
        name="worker",
        command="echo 'Worker booted and listening'; sleep 30",
        ready_pattern=r"Worker booted", startup_timeout=20)
    manager = RuntimeManager(tmp_path, [spec])
    try:
        ok, detail = manager.start_all()
        assert ok, detail
        assert "日志出现就绪标志" in detail
    finally:
        manager.stop_all()


def test_logs_are_captured(tmp_path):
    spec = ServiceSpec(name="worker", command="echo hello-from-service; sleep 30",
                       ready_pattern=r"hello-from-service", startup_timeout=20)
    manager = RuntimeManager(tmp_path, [spec])
    try:
        manager.start_all()
        assert "hello-from-service" in manager.logs("worker")
    finally:
        manager.stop_all()


def test_dependency_order(tmp_path):
    """db → api → web：起错顺序 api 连不上库，会以一个假的"代码 bug"形式出现。"""
    specs = [
        ServiceSpec(name="web", command="sleep 1", depends_on=["api"]),
        ServiceSpec(name="api", command="sleep 1", depends_on=["db"]),
        ServiceSpec(name="db", command="sleep 1"),
    ]
    manager = RuntimeManager(tmp_path, specs)
    assert [s.spec.name for s in manager._ordered()] == ["db", "api", "web"]


def test_dependency_cycle_does_not_hang(tmp_path):
    """编排出问题不该让整个任务崩掉，退化成声明顺序就好。"""
    specs = [ServiceSpec(name="a", command="true", depends_on=["b"]),
             ServiceSpec(name="b", command="true", depends_on=["a"])]
    manager = RuntimeManager(tmp_path, specs)
    assert len(manager._ordered()) == 2


def test_stop_frees_port_even_with_a_grandchild_process(tmp_path):
    """【回归测试】真实的启动链是 shell → pnpm → node，listen 的是【孙子进程】。

    我们 wait 的是最外层 shell，它一退就返回 —— 此时端口往往还没释放。
    后果很具体：紧接着的下一次 start_all() 报"端口已被占用"，占用者正是
    我们自己刚"停掉"的服务。所以 stop() 必须等到端口真的空出来。
    """
    port = free_port()
    # 用 shell 起一个后台子进程再自己 wait，精确模拟 pnpm 那一层
    command = f"{sys.executable} -m http.server {port} & wait"
    spec = ServiceSpec(name="web", command=command, port=port, health_path="/",
                       startup_timeout=30)
    manager = RuntimeManager(tmp_path, [spec])
    try:
        assert manager.start_all()[0]
        manager.stop_all()
        # 不 sleep、不重试 —— stop_all 返回时端口就必须已经可用
        assert not port_in_use(port)
        # 而且必须能立刻再起来
        ok, detail = manager.start_all()
        assert ok, detail
    finally:
        manager.stop_all()


def test_secrets_are_stripped_from_child_env(monkeypatch):
    """`npm run dev` 执行的是目标仓库里的任意代码。我们的 API key 没有任何理由
    出现在那个进程里 —— 这条防线一行代码，挡的是最不该发生的泄漏。"""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-never-leak")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_should-never-leak")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = child_env({"PORT": "3000"})
    assert "OPENAI_API_KEY" not in env
    assert "GITHUB_TOKEN" not in env
    assert env["PATH"] == "/usr/bin"      # 正常变量照常传
    assert env["PORT"] == "3000"
