"""RepoPilot：面向真实代码仓库的 Issue Resolution Agent。

核心闭环：
    Issue → 基线测试 → 计划(Planner) → 执行(Executor) → 验证(Verifier)
          → 失败重试 → 独立复查(Reviewer) → Git Diff / 报告

手写、无框架。模块与架构图一一对应：
    orchestrator.py  Agent State Machine（总指挥）
    planner.py       Planner
    executor.py      Executor（主循环，源自 study_agent/agent/loop.py）
    verifier.py      Verification（测试基线对比 —— 整个项目的灵魂）
    reviewer.py      Independent Reviewer（上下文隔离的直系应用）
    workspace.py     git 工作区（干净检查 / diff / 回滚兜底）
    adapters.py      Repository Adapter（唯一认识具体技术栈的地方）
    tools.py         Tool Runtime（read/search/edit/bash/tests/diff/symbols）
    policy.py        安全策略（路径越狱 / 危险命令 / 改动规模上限）
    trace.py         执行轨迹（run.jsonl，评测的原始数据）
    github.py        GitHub 集成（可选外壳，Phase 4）
"""

__version__ = "0.1.0"
