"""Evidence Collector：把"我看到页面坏了"变成可归档、可比较的证据。

【为什么截图不够】
截图只能证明"看起来不对"。可是最常见的一类前端 bug 是——**看起来完全正常**：
按钮好好地画在那儿，只是点了没反应。这种问题截图前后一模一样。

所以证据必须分两类，用途完全不同：

    功能问题 → DOM / URL / 网络请求 / 控制台错误（可断言、可比较）
    视觉问题 → 截图（只有这类才该用图）

【为什么必须脱敏】
证据要落盘、要进报告、要给人看，而它记录的是真实请求。请求头里有
Authorization、Cookie 里有 session、URL 里有 ?token=xxx。一旦原样写进
runs/ 目录，就等于把凭据抄进了一个谁都会 cat 的文件。脱敏发生在【写入前】，
不是展示前 —— 已经落盘的秘密就已经泄漏了。

组件内部状态（不是顶层任务状态）：
    IDLE → RECORDING_BASELINE → BASELINE_SAVED → RECORDING_AFTER → COMPLETED
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# 值需要被抹掉的键名（大小写不敏感）
_SECRET_KEYS = re.compile(
    r"(authorization|cookie|set-cookie|x-api-key|api[_-]?key|token|secret|"
    r"password|passwd|session|bearer|refresh)", re.I)

# URL 里 ?token=xxx&api_key=yyy 这类查询参数
_SECRET_QUERY = re.compile(
    r"([?&](?:token|api[_-]?key|access[_-]?token|key|secret|password|sig|signature)=)"
    r"[^&#\s]+", re.I)

# 长得像密钥的裸串：sk-xxx / ghp_xxx / 32+ 位十六进制 / JWT
_SECRET_BLOB = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|"
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+|"
    r"[a-f0-9]{32,})\b")

_MASK = "«已脱敏»"


def redact(value: str) -> str:
    """把一段文本里像凭据的部分抹掉。宁可多抹，不可漏抹。"""
    if not value:
        return value
    value = _SECRET_QUERY.sub(rf"\1{_MASK}", value)
    value = _SECRET_BLOB.sub(_MASK, value)
    return value


def redact_headers(headers: dict) -> dict:
    return {k: (_MASK if _SECRET_KEYS.search(k) else redact(str(v)))
            for k, v in (headers or {}).items()}


@dataclass
class ConsoleEntry:
    level: str        # error / warning
    text: str
    location: str = ""

    def render(self) -> str:
        where = f"  @{self.location}" if self.location else ""
        return f"[{self.level}] {self.text}{where}"


@dataclass
class NetworkEntry:
    method: str
    url: str
    status: int | None       # None = 请求本身失败（连不上、被拦截）
    failure: str = ""

    def render(self) -> str:
        code = self.status if self.status is not None else f"FAILED({self.failure})"
        return f"{self.method} {self.url} → {code}"


@dataclass
class EvidenceCollector:
    """一次任务的全部运行证据。前后各存一份，比较的是同一组字段。"""

    state: str = "IDLE"
    label: str = ""
    console: list[ConsoleEntry] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    network: list[NetworkEntry] = field(default_factory=list)
    screenshots: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    # -- 记录（由 BrowserSession 的事件回调调用）------------------------------
    def start_recording(self, label: str) -> None:
        self.label = label
        self.state = f"RECORDING_{label.upper()}"
        self.console.clear()
        self.page_errors.clear()
        self.network.clear()
        self.screenshots.clear()

    def add_console(self, level: str, text: str, location: str = "") -> None:
        # 只留 error/warning：info/debug 是噪音，会把上下文预算吃光
        if level not in ("error", "warning"):
            return
        self.console.append(ConsoleEntry(level, redact(text)[:500], location))

    def add_page_error(self, text: str) -> None:
        self.page_errors.append(redact(text)[:800])

    def add_network(self, method: str, url: str, status: int | None,
                    failure: str = "") -> None:
        # 只记录出错的请求。全量记录一个 SPA 首屏能有两百条，全是噪音。
        if status is not None and status < 400:
            return
        self.network.append(NetworkEntry(method, redact(url)[:300], status,
                                         redact(failure)[:200]))

    def add_screenshot(self, path: str) -> None:
        self.screenshots.append(path)

    # -- 读取 ---------------------------------------------------------------
    @property
    def clean(self) -> bool:
        return not (self.console or self.page_errors or self.network)

    def summary(self) -> str:
        if self.clean:
            return "运行证据：控制台无错误、无未捕获异常、无失败请求。"
        parts = [f"运行证据（{self.label or '未命名'}）："]
        errs = [c for c in self.console if c.level == "error"]
        if errs:
            parts.append(f"控制台错误 {len(errs)} 条：")
            parts += [f"  {c.render()}" for c in errs[:8]]
        if self.page_errors:
            parts.append(f"未捕获页面异常 {len(self.page_errors)} 条：")
            parts += [f"  {e}" for e in self.page_errors[:5]]
        if self.network:
            parts.append(f"失败/异常请求 {len(self.network)} 条：")
            parts += [f"  {n.render()}" for n in self.network[:10]]
        if self.screenshots:
            parts.append("截图：" + "，".join(self.screenshots[-3:]))
        return "\n".join(parts)

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "label": self.label,
            "console": [vars(c) for c in self.console],
            "page_errors": self.page_errors,
            "network": [vars(n) for n in self.network],
            "screenshots": self.screenshots,
            "notes": self.notes,
        }

    def save(self, run_dir: Path, label: str | None = None) -> Path:
        """落盘。文件名带 label（before/after），这样前后两份能并排比较。"""
        name = label or self.label or "evidence"
        path = Path(run_dir) / f"evidence-{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
                        encoding="utf-8")
        self.state = f"{name.upper()}_SAVED"
        return path


def diff_evidence(before: EvidenceCollector, after: EvidenceCollector) -> str:
    """前后证据对比 —— 报告里最有说服力的一段。

    比较的是【集合差】而不是数量：数量相同可能是"修好一个又坏一个"。
    """
    def keys(ev: EvidenceCollector) -> set[str]:
        return ({f"console:{c.text}" for c in ev.console if c.level == "error"}
                | {f"pageerror:{e}" for e in ev.page_errors}
                | {f"net:{n.method} {n.url} {n.status}" for n in ev.network})

    b, a = keys(before), keys(after)
    gone, appeared = sorted(b - a), sorted(a - b)
    lines = []
    if gone:
        lines.append(f"已消失的错误 {len(gone)} 条：")
        lines += [f"  - {g[:160]}" for g in gone[:8]]
    if appeared:
        lines.append(f"新出现的错误 {len(appeared)} 条（可能是改动引入的）：")
        lines += [f"  + {x[:160]}" for x in appeared[:8]]
    if not lines:
        lines.append("运行证据前后一致（没有错误消失，也没有新错误）。")
    return "\n".join(lines)
