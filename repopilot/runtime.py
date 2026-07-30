"""Runtime Manager：把目标仓库真正【跑起来】。

【为什么需要这一层】
旧版 RepoPilot 的能力边界等于"测试能不能替我判对错"。可是有一整类 bug，
单元测试根本看不见，只有页面真的渲染出来才会暴露：

    按钮显示了但点不动 / 跳转到了错误的页面 / 表单提交不了 /
    接口成功但页面没更新 / hydration error / 移动端布局错位 / 元素被遮挡

要抓这类问题，第一步就是让应用能起来、并且能确认它【真的可用】。

【核心认知：进程活着 ≠ 服务可用】
这是这个文件最重要的一句话。`npm run dev` 的进程一秒就起来了，但 Next.js
要编译十几秒才能响应第一个请求。如果只检查"进程还在"，agent 会立刻去打开
页面，拿到一个连接被拒绝的错误，然后开始"修"一个根本不存在的 bug。

所以就绪判定有两条独立的证据，任一成立即可：
    1) HTTP 健康检查通过（有端口的服务，首选）
    2) 日志里出现约定的就绪正则（没有 HTTP 端口的服务，比如 worker）

【组件自己的生命周期】
    STOPPED → STARTING → READY
                 ↓         ↓
               FAILED   STOPPING → STOPPED
注意这是【组件内部】的状态，和顶层任务状态机（BASELINE/PLAN/…）不是一回事：
顶层处在 REPRODUCE 时，Runtime 处在 READY，两者同时成立。

只用标准库：subprocess + threading + socket + urllib。不引入任何新依赖。
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path

from .adapters.base import ServiceSpec
from .config import (
    MAX_SERVICES,
    SERVICE_LOG_LINES,
    SERVICE_START_TIMEOUT,
)

# 这些环境变量绝对不能传给目标仓库的进程。
# 理由很硬：`npm run dev` 会执行目标仓库里的任意代码（package.json 的 scripts
# 就是一个任意命令执行入口）。我们的 API key 没有任何理由出现在那个进程里。
# 这条防线便宜得离谱（一行 dict 过滤），但它挡住的是"最不该发生的泄漏"。
_SECRET_PREFIXES = ("OPENAI_", "ANTHROPIC_", "DEEPSEEK_", "AWS_", "AZURE_",
                    "GITHUB_TOKEN", "GH_TOKEN", "GOOGLE_", "SSH_", "NPM_TOKEN",
                    "HF_TOKEN", "SLACK_", "STRIPE_")


def child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """给子进程的环境：剥掉一切密钥，再叠加服务自己声明的变量。"""
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(_SECRET_PREFIXES)}
    env.setdefault("CI", "1")            # 让工具走非交互模式，别在这儿等输入
    env.setdefault("NO_COLOR", "1")      # 日志里少点 ANSI 垃圾，好解析也好读
    env.update(extra or {})
    return env


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """端口是不是已经被占了。开工前必查 —— 否则健康检查会连上【别人的】服务，
    agent 拿着别人的页面判断自己的 bug，怎么都对不上。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


class Service:
    """一个被托管的进程 + 它的日志缓冲 + 它的就绪判定。"""

    def __init__(self, spec: ServiceSpec, root: Path):
        self.spec = spec
        self.root = root
        self.state = "STOPPED"
        self.proc: subprocess.Popen | None = None
        self.error = ""
        self.started_at = 0.0
        self.ready_at = 0.0
        # 环形缓冲：只留最后 N 行。日志是无限的，内存不是。
        self.log: deque[str] = deque(maxlen=SERVICE_LOG_LINES)
        self._reader: threading.Thread | None = None

    # -- 启动 ---------------------------------------------------------------
    def start(self) -> str:
        if self.state in ("STARTING", "READY"):
            return f"{self.spec.name} 已经在运行（{self.state}）"

        workdir = self.root / self.spec.cwd
        if not workdir.is_dir():
            self.state, self.error = "FAILED", f"工作目录不存在：{self.spec.cwd}"
            return self.error

        if self.spec.port and port_in_use(self.spec.port):
            self.state = "FAILED"
            self.error = (f"端口 {self.spec.port} 已被占用。这不是代码问题 —— "
                          f"先关掉占用它的进程，或换个端口再来。")
            return self.error

        self.state, self.error = "STARTING", ""
        self.started_at = time.time()
        self.log.clear()

        try:
            self.proc = subprocess.Popen(
                self.spec.command, shell=True, cwd=workdir,
                env=child_env(self.spec.env),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, errors="replace", bufsize=1,
                # 关键：让子进程自成一个进程组。dev server 会 fork 一堆子进程
                # （webpack worker、esbuild、nodemon…），只 kill 父进程会留下
                # 一屋子孤儿进程继续占着端口。有了进程组才能整组端掉。
                start_new_session=(os.name != "nt"),
            )
        except OSError as e:
            self.state, self.error = "FAILED", f"进程启动失败：{e}"
            return self.error

        self._reader = threading.Thread(target=self._pump_logs, daemon=True)
        self._reader.start()
        return f"{self.spec.name} 启动中（pid={self.proc.pid}）"

    def _pump_logs(self) -> None:
        """后台线程持续搬运日志。必须有人一直读 stdout ——
        管道缓冲区满了子进程会被【阻塞】，表现是"服务莫名卡死"。"""
        assert self.proc is not None and self.proc.stdout is not None
        for line in self.proc.stdout:
            self.log.append(line.rstrip("\n"))

    # -- 就绪判定 -----------------------------------------------------------
    def wait_ready(self, timeout: int | None = None) -> tuple[bool, str]:
        """轮询到就绪或超时。返回 (是否就绪, 说明)。"""
        limit = timeout or min(self.spec.startup_timeout, SERVICE_START_TIMEOUT)
        deadline = time.time() + limit

        while time.time() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                self.state = "FAILED"
                self.error = f"进程已退出（exit={self.proc.returncode}），启动失败"
                return False, self.error

            ok, why = self._probe()
            if ok:
                self.state = "READY"
                self.ready_at = time.time()
                took = round(self.ready_at - self.started_at, 1)
                return True, f"{self.spec.name} 就绪（{took}s，{why}）"
            time.sleep(0.5)

        self.state = "FAILED"
        self.error = (f"{self.spec.name} 在 {limit}s 内没有就绪。"
                      f"这大概率是环境问题（依赖没装 / 端口 / 配置），不是 issue 本身。")
        return False, self.error

    def _probe(self) -> tuple[bool, str]:
        """两条独立证据，任一成立即算就绪。"""
        if self.spec.ready_pattern:
            import re
            joined = "\n".join(self.log)
            if re.search(self.spec.ready_pattern, joined):
                return True, "日志出现就绪标志"

        url = self.spec.health_url
        if url:
            try:
                with urllib.request.urlopen(url, timeout=2) as resp:
                    # 2xx/3xx 都算活着；连 404 也说明 HTTP 服务起来了（只是这个路径没有）
                    if resp.status < 500:
                        return True, f"健康检查 {url} → {resp.status}"
            except urllib.error.HTTPError as e:
                if e.code < 500:
                    return True, f"健康检查 {url} → {e.code}"
            except (urllib.error.URLError, TimeoutError, OSError, ValueError):
                pass
        return False, ""

    # -- 状态与日志 ---------------------------------------------------------
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def status_line(self) -> str:
        where = f" :{self.spec.port}" if self.spec.port else ""
        pid = self.proc.pid if self.proc else "-"
        extra = f"  {self.error}" if self.error else ""
        # 进程活着但状态是 READY，两件事都要报 —— 它们真的会不一致
        liveness = "进程存活" if self.alive() else "进程已退出"
        return f"{self.spec.name:<8} {self.state:<9}{where:<7} pid={pid:<8} {liveness}{extra}"

    def tail_log(self, lines: int = 50) -> str:
        got = list(self.log)[-lines:]
        return "\n".join(got) or "(暂无日志)"

    # -- 停止 ---------------------------------------------------------------
    def stop(self, grace: float = 5.0) -> str:
        if self.proc is None or self.proc.poll() is not None:
            self.state = "STOPPED"
            return f"{self.spec.name} 未在运行"

        self.state = "STOPPING"
        try:
            if os.name != "nt":
                # 端掉整个进程组，不是只端父进程 —— 见 start() 里的说明
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            else:
                self.proc.terminate()
        except (ProcessLookupError, PermissionError, OSError):
            pass

        try:
            self.proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            # 先礼后兵：给了缓冲期还不走的，直接 KILL。
            self._kill()

        self.state = "STOPPED"
        # 【进程退出 ≠ 端口释放】。这是 start() 那句"进程活着≠服务可用"的镜像版：
        # 我们 wait 的是最外层的 shell，而真正 listen 的是它孙子进程里的 node；
        # shell 一退我们就返回，此时端口往往还没被内核回收。
        # 不等的后果非常具体：紧接着的下一次 start_all() 会报"端口已被占用"，
        # 而占用者正是我们自己刚"停掉"的服务 —— 一个看起来像环境问题的自伤。
        freed = self._wait_port_free()
        if not freed:
            return (f"{self.spec.name} 进程已结束，但端口 {self.spec.port} 仍未释放 —— "
                    f"下次启动可能撞端口，必要时手动检查")
        return f"{self.spec.name} 已停止"

    def _kill(self) -> None:
        try:
            if os.name != "nt":
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
            else:
                self.proc.kill()
        except (ProcessLookupError, PermissionError, OSError):
            pass

    def _wait_port_free(self, limit: float = 6.0) -> bool:
        if not self.spec.port:
            return True
        deadline = time.time() + limit
        escalated = False
        while time.time() < deadline:
            if not port_in_use(self.spec.port):
                return True
            # 还占着说明有孙子进程活着，SIGTERM 没传下去 —— 升级到 SIGKILL 整组端掉
            if not escalated and time.time() > deadline - limit + 1.5:
                self._kill()
                escalated = True
            time.sleep(0.2)
        return not port_in_use(self.spec.port)


class RuntimeManager:
    """按依赖顺序编排一组服务。"""

    def __init__(self, root: Path, specs: list[ServiceSpec]):
        self.root = Path(root)
        if len(specs) > MAX_SERVICES:
            specs = specs[:MAX_SERVICES]
        self.services: dict[str, Service] = {
            s.name: Service(s, self.root) for s in specs
        }

    @property
    def available(self) -> bool:
        return bool(self.services)

    def _ordered(self) -> list[Service]:
        """拓扑排序：db 先起、api 再起、web 最后。环或缺失依赖不报错，
        退化成声明顺序 —— 编排失败不该让整个任务崩掉。"""
        done: list[Service] = []
        seen: set[str] = set()

        def visit(name: str, trail: set[str]) -> None:
            if name in seen or name in trail or name not in self.services:
                return
            svc = self.services[name]
            for dep in svc.spec.depends_on:
                visit(dep, trail | {name})
            seen.add(name)
            done.append(svc)

        for name in self.services:
            visit(name, set())
        return done

    def start_all(self) -> tuple[bool, str]:
        """全部起起来并等就绪。任一失败就【回滚已起的】—— 半启动状态最难 debug。"""
        if not self.available:
            return False, "这个项目没有可启动的服务（adapter 没识别出启动方式）"

        lines: list[str] = []
        for svc in self._ordered():
            lines.append(svc.start())
            if svc.state == "FAILED":
                self.stop_all()
                return False, "\n".join([*lines, f"启动失败：{svc.error}"])

            ok, why = svc.wait_ready()
            lines.append(why)
            if not ok:
                lines.append(f"--- {svc.spec.name} 日志末尾 ---")
                lines.append(svc.tail_log(30))
                self.stop_all()
                return False, "\n".join(lines)
        return True, "\n".join(lines)

    def status(self) -> str:
        if not self.available:
            return "没有托管任何服务"
        head = [s.status_line() for s in self._ordered()]
        urls = [f"  {s.spec.name}: {s.spec.base_url}"
                for s in self._ordered() if s.spec.base_url]
        return "\n".join(head + (["可访问地址："] + urls if urls else []))

    def logs(self, name: str | None = None, lines: int = 50) -> str:
        if name:
            svc = self.services.get(name)
            if svc is None:
                return f"没有名为 {name} 的服务。已知：{', '.join(self.services)}"
            return f"--- {name} ---\n{svc.tail_log(lines)}"
        return "\n\n".join(f"--- {n} ---\n{s.tail_log(lines)}"
                           for n, s in self.services.items())

    def restart(self, name: str) -> str:
        """改完代码要重启才生效 —— 除非 dev server 自带热重载。
        agent 分不清哪种情况，所以给它一个明确的按钮，别让它猜。"""
        svc = self.services.get(name)
        if svc is None:
            return f"没有名为 {name} 的服务。已知：{', '.join(self.services)}"
        out = [svc.stop(), svc.start()]
        ok, why = svc.wait_ready()
        out.append(why)
        return "\n".join(out) if ok else "\n".join([*out, svc.tail_log(20)])

    def stop_all(self) -> str:
        """反向停止（web → api → db）。必须幂等：重复调用不能报错 ——
        它会被 finally 调用，而 finally 可能在任何奇怪的时刻触发。"""
        return "\n".join(svc.stop() for svc in reversed(self._ordered()))

    def base_url(self, name: str | None = None) -> str | None:
        """给浏览器工具用：默认拿第一个有端口的服务地址。"""
        pool = [self.services[name]] if name and name in self.services else self._ordered()
        return next((s.spec.base_url for s in pool if s.spec.base_url), None)
