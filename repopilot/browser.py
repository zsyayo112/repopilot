"""Browser Session：让 agent 像真实用户一样操作页面。

【三个必须一开始就想清楚的设计决定】

1) **会话必须长命**。不能每调一个工具就新开一个浏览器 —— cookie、登录态、
   当前页面全丢，第二步操作直接从头开始。所以浏览器是一个跨工具调用存活的
   对象，只在任务结束时关闭。

2) **给模型的是"带编号的可交互元素表"，不是 HTML**。
   两条路都试过的人才知道差别：
       ✗ 把 HTML 塞给模型  → 一个页面几十万字符，直接爆上下文
       ✗ 让模型写 CSS 选择器 → `div:nth-child(3) > button` 一改样式就失效
       ✓ 快照返回 `ref=e12  button "立即预订"`，模型下一步 click(ref="e12")
   ref 是【本次快照】期间打在 DOM 上的属性（data-rp-ref），稳定、唯一、
   和视觉结构解耦。这是把"页面"压缩成模型能推理的东西的关键一步。

3) **只准访问本地地址**。浏览器是一个完整的 HTTP 客户端，不加限制就等于给
   agent 开了一个内网探测器（云上最经典的目标是元数据接口 169.254.169.254）。
   所以 goto 前先检查 host 白名单 —— 白名单，不是黑名单。

Playwright 是【可选依赖】：不装照样能跑全部原有功能，只是浏览器工具会返回
一句"没装，这样装"。核心的"依赖只有两个"这条底线没有被破。
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

from .config import (
    BROWSER_ALLOWED_HOSTS,
    BROWSER_HEADLESS,
    BROWSER_SNAPSHOT_CHARS,
    BROWSER_TIMEOUT,
    BROWSER_VIEWPORT,
)
from .evidence import EvidenceCollector

INSTALL_HINT = (
    "浏览器功能需要 Playwright（可选依赖，默认不装）。安装方式：\n"
    "    pip install -e \".[browser]\"\n"
    "    playwright install chromium\n"
    "装完重新运行即可。"
)

# 把页面压缩成"可交互元素表"的注入脚本。
# 关键点是 covered 检测：用 elementFromPoint 判断元素中心点是否被别的东西盖住 ——
# 这正好抓住那类"看起来正常但点不动"的 bug，截图永远看不出来。
_SNAPSHOT_JS = r"""
() => {
  const SEL = 'a,button,input,select,textarea,summary,[role],[onclick],[contenteditable="true"]';
  document.querySelectorAll('[data-rp-ref]').forEach(el => el.removeAttribute('data-rp-ref'));

  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return false;
    const s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0';
  };
  const covered = (el) => {
    const r = el.getBoundingClientRect();
    const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    if (cx < 0 || cy < 0 || cx > innerWidth || cy > innerHeight) return false;
    const top = document.elementFromPoint(cx, cy);
    return !!(top && top !== el && !el.contains(top) && !top.contains(el));
  };
  const nameOf = (el) => String(
    el.getAttribute('aria-label') ||
    (el.labels && el.labels[0] ? el.labels[0].innerText : '') ||
    el.getAttribute('placeholder') || el.getAttribute('title') ||
    el.getAttribute('alt') || (el.innerText || '').trim() ||
    el.getAttribute('name') || ''
  ).replace(/\s+/g, ' ').trim().slice(0, 80);
  const roleOf = (el) => {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit;
    const t = el.tagName;
    if (t === 'A') return 'link';
    if (t === 'BUTTON' || t === 'SUMMARY') return 'button';
    if (t === 'SELECT') return 'combobox';
    if (t === 'TEXTAREA') return 'textbox';
    if (t === 'INPUT') {
      const ty = (el.type || 'text').toLowerCase();
      if (ty === 'checkbox' || ty === 'radio') return ty;
      if (ty === 'submit' || ty === 'button' || ty === 'reset') return 'button';
      return 'textbox';
    }
    return t.toLowerCase();
  };

  const elements = [];
  let n = 0;
  for (const el of document.querySelectorAll(SEL)) {
    if (!visible(el)) continue;
    const ref = 'e' + (++n);
    el.setAttribute('data-rp-ref', ref);
    elements.push({
      ref, role: roleOf(el), name: nameOf(el),
      disabled: !!(el.disabled || el.getAttribute('aria-disabled') === 'true'),
      covered: covered(el),
      value: (el.type === 'password') ? '«hidden»'
             : (el.value === undefined ? '' : String(el.value).slice(0, 40)),
      href: el.tagName === 'A' ? (el.getAttribute('href') || '') : ''
    });
    if (n >= 200) break;
  }
  return {
    url: location.href, title: document.title, elements,
    text: (document.body ? document.body.innerText : '').replace(/\n{3,}/g, '\n\n').slice(0, 2500)
  };
}
"""


class BrowserUnavailable(RuntimeError):
    """Playwright 没装。单独一个异常类型，好在工具层转成友好提示而不是堆栈。"""


def host_allowed(url: str) -> bool:
    """白名单校验。**只认本地** —— 见文件头第 3 条。"""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    return (parsed.hostname or "") in BROWSER_ALLOWED_HOSTS


class BrowserSession:
    """一个长命的浏览器会话 + 自动挂载的证据采集。"""

    def __init__(self, base_url: str | None = None, artifacts_dir: Path | None = None):
        self.base_url = base_url
        self.artifacts_dir = artifacts_dir
        self.state = "CLOSED"
        self.evidence = EvidenceCollector()
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._shot_seq = 0
        self._last_refs: dict[str, dict] = {}

    # -- 生命周期 -----------------------------------------------------------
    def start(self) -> None:
        if self._page is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            self.state = "FAILED"
            raise BrowserUnavailable(INSTALL_HINT) from e

        self.state = "STARTING"
        try:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=BROWSER_HEADLESS)
            self._context = self._browser.new_context(viewport=dict(BROWSER_VIEWPORT))
            self._context.set_default_timeout(BROWSER_TIMEOUT)
            self._page = self._context.new_page()
        except Exception as e:
            self.close()
            self.state = "FAILED"
            raise BrowserUnavailable(
                f"Playwright 已安装但浏览器启动失败：{e}\n"
                "多半是没下载浏览器内核，跑一次：playwright install chromium"
            ) from e

        self._wire_evidence()
        self.evidence.start_recording("session")
        self.state = "ACTIVE"

    def _wire_evidence(self) -> None:
        """从浏览器启动的第一刻就开始录 —— 不是等出问题了才开始。

        错误往往发生在【首屏加载】那几百毫秒里；等 agent 反应过来再挂监听，
        那些事件已经过去了，永远抓不到。
        """
        page = self._page
        page.on("console", lambda msg: self.evidence.add_console(
            msg.type, msg.text, _loc(msg)))
        page.on("pageerror", lambda err: self.evidence.add_page_error(str(err)))
        page.on("requestfailed", lambda req: self.evidence.add_network(
            req.method, req.url, None, (req.failure or "unknown")))
        page.on("response", lambda resp: self.evidence.add_network(
            resp.request.method, resp.url, resp.status))

    def close(self) -> None:
        """幂等收尾：任何一步炸了都继续往下关，别把资源漏在半路。"""
        for obj, method in ((self._context, "close"), (self._browser, "close"),
                            (self._pw, "stop")):
            if obj is not None:
                try:
                    getattr(obj, method)()
                except Exception:
                    pass
        self._page = self._context = self._browser = self._pw = None
        self.state = "CLOSED"

    @property
    def page(self):
        if self._page is None:
            self.start()
        return self._page

    @property
    def active(self) -> bool:
        return self._page is not None

    # -- 导航 ---------------------------------------------------------------
    def resolve(self, url: str) -> str:
        """相对路径按 base_url 补全 —— 模型给 "/packages/123" 就该能用。"""
        if url.startswith(("http://", "https://")):
            return url
        if not self.base_url:
            raise ValueError(
                f"给的是相对路径 {url!r}，但当前没有已知的服务地址。"
                "先 start_services 把应用跑起来，或者给完整的 http:// 地址。")
        return self.base_url.rstrip("/") + "/" + url.lstrip("/")

    def goto(self, url: str) -> str:
        full = self.resolve(url)
        if not host_allowed(full):
            raise PermissionError(
                f"拒绝访问 {full}：浏览器只允许访问本地地址 "
                f"（{', '.join(BROWSER_ALLOWED_HOSTS)}）。"
                "这条限制防的是用浏览器去探测内网。")
        page = self.page
        page.goto(full, wait_until="domcontentloaded", timeout=30_000)
        return self.snapshot(header=f"已打开 {full}")

    def reload(self) -> str:
        self.page.reload(wait_until="domcontentloaded")
        return self.snapshot(header="已刷新")

    def set_viewport(self, width: int, height: int) -> str:
        """移动端 bug 必须固定 viewport 才能复现（notes 里那个 390×844 就是 iPhone）。"""
        self.page.set_viewport_size({"width": width, "height": height})
        return self.snapshot(header=f"viewport 已设为 {width}×{height}")

    # -- 快照 ---------------------------------------------------------------
    def snapshot(self, header: str = "") -> str:
        page = self.page
        data = page.evaluate(_SNAPSHOT_JS)
        self._last_refs = {e["ref"]: e for e in data["elements"]}

        lines = [header] if header else []
        lines.append(f"url={data['url']}")
        lines.append(f"title={data['title']}")

        blocked = [e for e in data["elements"] if e["covered"] or e["disabled"]]
        if blocked:
            # 单独拎出来说：这正是"看起来正常但点不动"的证据
            lines.append(f"⚠ 有 {len(blocked)} 个元素被遮挡或禁用（见下表 [遮挡]/[禁用] 标记）")

        lines.append("\n可交互元素（用 ref 调用 browser_click / browser_fill）：")
        for e in data["elements"]:
            flags = "".join(["[禁用]" if e["disabled"] else "",
                             "[遮挡]" if e["covered"] else ""])
            value = f" value={e['value']!r}" if e["value"] else ""
            href = f" href={e['href']}" if e["href"] else ""
            lines.append(f"  ref={e['ref']:<5} {e['role']:<10} {e['name']!r}{value}{href}{flags}")
        if not data["elements"]:
            lines.append("  (页面上没有可交互元素 —— 可能还在加载，或者渲染失败了)")

        lines.append("\n页面文本：")
        lines.append(data["text"])

        out = "\n".join(lines)
        return out[:BROWSER_SNAPSHOT_CHARS] + (
            "\n…[快照被截断]" if len(out) > BROWSER_SNAPSHOT_CHARS else "")

    # -- 交互 ---------------------------------------------------------------
    def _locator(self, ref: str | None, role: str | None, name: str | None):
        """两种定位方式：ref（首选，来自上一次快照）或 role+name（语义定位）。

        都不给就报错 —— 绝不接受 CSS 选择器：那是最脆弱的一种定位，
        模型写出来的 nth-child 选择器改一次样式就全废。
        """
        page = self.page
        if ref:
            if ref not in self._last_refs:
                raise ValueError(
                    f"ref={ref} 不在最近一次快照里。ref 只在快照之后有效 —— "
                    "先 browser_snapshot 再用新的 ref。")
            return page.locator(f'[data-rp-ref="{ref}"]')
        if role and name:
            return page.get_by_role(role, name=name)
        if name:
            return page.get_by_text(name, exact=False)
        raise ValueError("必须给 ref，或者给 role+name（语义定位）")

    def click(self, ref: str | None = None, role: str | None = None,
              name: str | None = None) -> str:
        target = self._describe(ref, role, name)
        locator = self._locator(ref, role, name)
        before_url = self.page.url
        locator.first.click(timeout=BROWSER_TIMEOUT)
        self._settle()
        moved = "" if self.page.url == before_url else f"（URL 从 {before_url} 变为 {self.page.url}）"
        return self.snapshot(header=f"已点击 {target}{moved}")

    def fill(self, text: str, ref: str | None = None, label: str | None = None,
             submit: bool = False) -> str:
        page = self.page
        if ref:
            locator = self._locator(ref, None, None)
        elif label:
            locator = page.get_by_label(label)
        else:
            raise ValueError("必须给 ref 或 label")
        locator.first.fill(text, timeout=BROWSER_TIMEOUT)
        if submit:
            locator.first.press("Enter")
            self._settle()
        return self.snapshot(header=f"已填入 {ref or label}={text!r}"
                                    f"{'（已回车提交）' if submit else ''}")

    def select(self, value: str, ref: str | None = None, label: str | None = None) -> str:
        locator = (self._locator(ref, None, None) if ref
                   else self.page.get_by_label(label or ""))
        locator.first.select_option(value, timeout=BROWSER_TIMEOUT)
        self._settle()
        return self.snapshot(header=f"已选择 {value!r}")

    def _settle(self) -> None:
        """等页面安定下来。networkidle 会等到没有网络活动 —— 但它可能永远等不到
        （有轮询的页面），所以超时是【正常情况】，吞掉就好，不算失败。"""
        try:
            self.page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:
            pass

    def _describe(self, ref: str | None, role: str | None, name: str | None) -> str:
        if ref and ref in self._last_refs:
            e = self._last_refs[ref]
            return f"{e['role']} {e['name']!r} (ref={ref})"
        return f"{role or ''} {name or ref or '?'}".strip()

    # -- 观察 ---------------------------------------------------------------
    def current_url(self) -> str:
        return self.page.url

    def screenshot(self, name: str = "") -> str:
        if self.artifacts_dir is None:
            return "没有配置产物目录，无法保存截图"
        self._shot_seq += 1
        safe = re.sub(r"[^\w.-]+", "_", name) or f"shot{self._shot_seq}"
        path = Path(self.artifacts_dir) / f"{self._shot_seq:02d}-{safe}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(path), full_page=True)
        self.evidence.add_screenshot(str(path))
        return f"截图已保存：{path}"

    def errors(self) -> str:
        return self.evidence.summary()


def _loc(msg) -> str:
    try:
        loc = msg.location or {}
        return f"{loc.get('url', '')}:{loc.get('lineNumber', '')}"
    except Exception:
        return ""
