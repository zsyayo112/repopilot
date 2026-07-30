"""安全策略：所有硬约束集中在这里，一眼看全。

四道闸：
  1) jail()               所有文件路径必须落在仓库内；.git 禁止写入
  2) check_command()      危险命令黑名单
  3) MAX_MODIFIED_FILES   改动规模上限（在 orchestrator 里检查）
  4) looks_irreversible() 浏览器交互里的不可逆意图（付款/删除/下单）

另有两道不在本文件、但属于同一体系的边界：
  · browser.host_allowed()  浏览器只准访问本地地址（白名单）
  · runtime.child_env()     子进程环境里剥掉所有密钥

黑名单防的是【事故】，不是防高手 —— 真正的强隔离要靠 Docker 沙箱，
那是 Phase 5 的独立课题。MVP 的现实防线是：黑名单 + git 可回滚 + 权限确认。
"""

import re
from pathlib import Path

DANGEROUS = [
    "rm -rf /", "sudo ", "mkfs", "dd if=", ":(){", "> /dev/", "chmod 777 /",
    # 网络与外发：MVP 阶段 agent 不需要装包/上网，出现就是跑偏了
    "curl ", "wget ", "pip install", "npm install",
    # 提交与推送是【人】的决定：agent 只负责产出 diff，最后一步永远留给用户
    "git commit", "git push", "git reset",
]


def check_command(command: str) -> tuple[bool, str]:
    """返回 (是否放行, 拒绝理由)。"""
    for pat in DANGEROUS:
        if pat in command:
            return False, f"命令被安全策略拒绝（匹配到 {pat!r}）"
    return True, ""


# 第四道闸：浏览器交互里的【不可逆意图】。
#
# 为什么需要这个：在页面上乱点通常无害（点错了刷新一下就好），所以浏览器交互
# 默认自动放行 —— 否则 agent 每点一下都要问人，根本没法用。但有一类点击点下去
# 就回不来了：下单、付款、删除账户、清空数据。
#
# 判断依据是【元素的可见名称】—— 也就是用户在屏幕上真正读到的那几个字。
# 这比看 URL 或 DOM 结构靠谱：按钮上写着"确认支付"，那它就是在收钱。
#
# 诚实交代它的局限：这仍然是子串匹配，和命令黑名单一样属于弱防御 ——
# 一个写着 "Continue" 的支付按钮它抓不到。真正的解法是"不可逆操作必须由人
# 在真实环境里做"，而 agent 只该在测试环境和测试数据上跑。
IRREVERSIBLE_INTENT = (
    # 中文
    "支付", "付款", "结算", "下单", "提交订单", "确认订单", "删除", "移除",
    "清空", "注销", "解绑", "转账", "提现", "退款", "永久",
    # 英文
    "pay", "payment", "checkout", "purchase", "place order", "submit order",
    "confirm order", "delete", "remove", "destroy", "wipe", "deactivate",
    "cancel account", "transfer", "withdraw", "refund",
)


_UNICODE_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")


def looks_irreversible(text: str) -> bool:
    """这段文字（元素名称/场景 JSON）看起来像在做不可逆的事吗。

    先还原 \\uXXXX 转义再匹配。**这不是洁癖，是一个真实的绕过**：模型给
    run_scenario 的参数常常是 ensure_ascii 编码过的 JSON，"确认支付" 到我们
    手里长这样 `\\u786e\\u8ba4\\u652f\\u4ed8` —— 不还原就一个字都匹配不上，
    护栏形同虚设。（这条是被自己的单测抓出来的。）
    """
    unescaped = _UNICODE_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), text)
    low = unescaped.lower()
    return any(kw in low for kw in IRREVERSIBLE_INTENT)


def jail(path: str, root: Path, writing: bool = False) -> Path:
    """把模型给的路径落进仓库内，挡住越狱。

    继承自 agent/tools.py 的 _resolve()，两处进化：
      - 剥"仓库名前缀"改成【先信字面路径，剥完不存在才剥】：
        agent/tools.py 原版无条件剥前缀，这在 playground 沙盒里成立
        （沙盒不可能有同名子目录），但真实仓库大量 Python 项目
        顶层包目录名恰好等于仓库名（tinydb/tinydb、django/django），
        无脑剥掉会把合法路径剥成不存在的路径。改成"存在性验证优先"：
        字面路径本来就存在 → 直接用；只有字面路径不存在、且剥掉前缀后
        的路径存在，才认定是模型画蛇添足加的前缀。
      - 新增：写操作禁止碰 .git（改坏引用整个仓库就废了）
    """
    path = path.strip().lstrip("/")

    p = (root / path).resolve()
    prefix = f"{root.name}/"
    if not p.exists() and path.startswith(prefix):
        stripped = (root / path[len(prefix):]).resolve()
        if stripped.exists():
            p = stripped

    if not p.is_relative_to(root):
        raise PermissionError(f"拒绝访问仓库之外的路径：{path}")

    rel = p.relative_to(root)
    if writing and rel.parts and rel.parts[0] == ".git":
        raise PermissionError("拒绝写入 .git 目录")
    return p
