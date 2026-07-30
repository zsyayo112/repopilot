"""RepoPilot：面向真实代码仓库的 Issue Resolution Agent。

核心闭环：
    Issue → 基线测试 → [启动应用 → 浏览器复现] → 计划(Planner) → 执行(Executor)
          → 验证(Verifier：测试 + lint/类型/构建 + 同一份复现脚本)
          → 失败重试 → 独立复查(Reviewer) → Git Diff / 报告 → 清理

手写、无框架。模块与架构图一一对应：

  Agent 本体
    orchestrator.py  Agent State Machine（总指挥，10 状态）
    planner.py       Planner
    executor.py      Executor（主循环，源自 study_agent/agent/loop.py）
    explorer.py      探索子 agent（上下文隔离：脏上下文留在它那边，只回结论）
    reviewer.py      Independent Reviewer（上下文隔离的另一次应用：去偏见）
  仓库理解
    workspace.py     git 工作区（干净检查 / diff / 回滚兜底）
    adapters/        Repository Adapter 包（唯一认识具体技术栈的地方）
                     base 契约 / registry 探测+monorepo / symbols 多语言符号
                     / *_stack.py 每种技术栈一个文件
  工具与运行时
    tools.py         Tool Runtime（28 个工具，按 core/runtime/browser 分组暴露）
    runtime.py       Runtime Manager（起服务、健康检查、日志、进程组清理）
    browser.py       Browser Session（Playwright，可选依赖；带 ref 的页面快照）
    evidence.py      Evidence Collector（控制台/网络/截图，写盘前脱敏）
    scenario.py      结构化复现脚本（修改前后跑完全相同的步骤和断言）
  验证与安全
    verifier.py      测试基线对比 + 验证流水线（整个项目的灵魂）
    doctor.py        环境体检（区分"代码有问题"和"环境没装好"）
    policy.py        安全策略（路径越狱 / 危险命令 / 不可逆意图 / 改动规模）
    permissions.py   人对每个改动性操作的可见与否决权
  可观察性
    trace.py         执行轨迹（run.jsonl，评测的原始数据）
    github.py        GitHub 集成（可选外壳，Phase 4）
"""

__version__ = "0.1.0"
