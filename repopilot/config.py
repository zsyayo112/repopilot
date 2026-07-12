"""集中配置：模型客户端、预算与硬约束。配置只有一处来源（沿用 agent/config.py 的原则）。

.env 查找顺序：repopilot 项目根 → 上一级目录（学习期直接复用 study_agent 的 key）。
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

client = OpenAI(
    api_key=os.environ["OPENAI_API_KEY"],
    base_url=os.environ["OPENAI_BASE_URL"],
)
MODEL = os.environ["OPENAI_MODEL"]

# ---------------------------------------------------------------------------
# 预算与硬约束。数字偏保守，跑通后再放宽。
# 每一条都是"用代码而不是提示词"兜底的例子（硬约束 vs 软约束）。
# ---------------------------------------------------------------------------
MAX_TOOL_OUTPUT = 6000      # 单个工具返回值上限（字符），保护上下文
MAX_TURNS = 40              # executor 单次最多循环圈数（真实仓库比 playground 圈数多）
MAX_FIX_ATTEMPTS = 3        # 测试失败后最多整轮重试次数
MAX_MODIFIED_FILES = 8      # 一次任务最多允许改动的文件数，防"改跑偏"
CMD_TIMEOUT = 180           # 单条命令超时（秒）；测试命令放宽到 2 倍

RUNS_DIR = PROJECT_ROOT / "runs"       # 每次执行的轨迹目录
CLONES_DIR = PROJECT_ROOT.parent / "targets"  # --repo 传 URL 时克隆到这里

# ANSI 颜色
DIM, CYAN, YELLOW, RED, GREEN, BOLD, RESET = (
    "\033[90m", "\033[36m", "\033[33m", "\033[31m", "\033[32m", "\033[1m", "\033[0m",
)
