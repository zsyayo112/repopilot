"""集中配置：模型客户端、预算与硬约束。配置只有一处来源（沿用 agent/config.py 的原则）。

.env 查找顺序：repopilot 项目根 → 上一级目录（学习期直接复用 study_agent 的 key）。

【为什么 client 是懒加载的】导入本模块不该有副作用、更不该要求 API key：
单元测试、CI、以及 `repo-pilot detect`（纯静态探测，根本不调模型）都必须能在
没有 key 的环境里跑起来。所以 key 只在【真正要发起模型调用时】才检查——见 get_client()。
"""

import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

PROJECT_ROOT = Path(__file__).resolve().parents[1]  # repopilot/ 项目根

for _candidate in (PROJECT_ROOT / ".env", PROJECT_ROOT.parent / ".env"):
    if _candidate.exists():
        load_dotenv(_candidate)
        break
else:
    load_dotenv()

MODEL = os.environ.get("OPENAI_MODEL", "deepseek-chat")

_client: OpenAI | None = None


def get_client() -> OpenAI:
    """懒加载单例。第一次真正要调模型时才构造，也才要求 key 存在。"""
    global _client
    if _client is None:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "未设置 OPENAI_API_KEY。请在项目根执行 `cp .env.example .env` 并填入你的 key。"
            )
        _client = OpenAI(
            api_key=api_key,
            base_url=os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com"),
        )
    return _client


# ---------------------------------------------------------------------------
# 预算与硬约束。数字偏保守，跑通后再放宽。
# 每一条都是"用代码而不是提示词"兜底的例子（硬约束 vs 软约束）。
# ---------------------------------------------------------------------------
MAX_TOOL_OUTPUT = 6000      # 单个工具返回值上限（字符），保护上下文
MAX_TURNS = 40              # executor 单次最多循环圈数（真实仓库比 playground 圈数多）
MAX_FIX_ATTEMPTS = 3        # 测试失败后最多整轮重试次数
MAX_MODIFIED_FILES = 8      # 一次任务最多允许改动的文件数，防"改跑偏"
MAX_BASELINE_UNITS = 4      # monorepo 最多给几个子项目跑基线（基线不是免费的）
CMD_TIMEOUT = 180           # 单条命令超时（秒）
TEST_TIMEOUT = CMD_TIMEOUT * 2  # 测试/构建放宽到 2 倍（框架项目 build 很慢）

# --- 探索子 agent（上下文隔离）---------------------------------------------
# 它的价值不是分工，是【脏上下文留在它那边】：读十个文件九个没用，但九个都会
# 永久留在主上下文里。子 agent 只把一句结论带回来。
EXPLORER_MAX_TURNS = 20     # 子 agent 自己的圈数上限，比主循环紧
EXPLORER_MAX_OUTPUT = 4000  # 子 agent 结论回填上限：它要是长篇大论就失去意义了

# --- 运行时（把大型框架应用真正跑起来）--------------------------------------
# 为什么单独一套超时：框架 dev server 冷启动要编译，Next.js/Spring 动辄 1~3 分钟，
# 用 CMD_TIMEOUT 那套秒级思维会在真项目上一律超时。
SERVICE_START_TIMEOUT = 180   # 单个服务等就绪上限（秒）
SERVICE_LOG_LINES = 200       # 保留在内存里的日志行数（环形缓冲，防日志刷爆内存）
MAX_SERVICES = 6              # 一次任务最多允许起的服务数，防"起了一屋子进程"

# --- 浏览器（Playwright）---------------------------------------------------
BROWSER_HEADLESS = os.environ.get("REPOPILOT_HEADED", "") == ""   # 设 REPOPILOT_HEADED=1 可看见浏览器
BROWSER_VIEWPORT = {"width": 1440, "height": 900}
BROWSER_TIMEOUT = 15_000      # 单个浏览器动作超时（毫秒）
BROWSER_SNAPSHOT_CHARS = 4000  # 页面快照回填上限：一个页面能有几万字符，必须封顶
# 浏览器只允许访问本地地址。**这是一道真正的安全边界**：不设的话，agent 可以
# 用浏览器去敲内网服务（云元数据接口 169.254.169.254 是最经典的目标）。
BROWSER_ALLOWED_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "[::1]", "::1")

RUNS_DIR = PROJECT_ROOT / "runs"       # 每次执行的轨迹目录
CLONES_DIR = PROJECT_ROOT.parent / "targets"  # --repo 传 URL 时克隆到这里

# ANSI 颜色
DIM, CYAN, YELLOW, RED, GREEN, BOLD, RESET = (
    "\033[90m", "\033[36m", "\033[33m", "\033[31m", "\033[32m", "\033[1m", "\033[0m",
)
