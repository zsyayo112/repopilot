[English](README.md) | 中文

# RepoPilot：面向真实代码仓库的 Issue Resolution Agent

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

给它一个 git 仓库和一个 issue，它负责走完整条软件工程闭环：

```
Issue → 基线测试 → 修改计划 → 代码定位与编辑 → 测试验证 → 失败重试 → 独立审查 → Git Diff / 报告
```

**手写、无框架。** 没有 LangChain / LangGraph —— agent 循环、工具调度、权限闸门、
结构化输出、状态机全部裸写。这既是学习方式，也是立场：核心机制必须可理解、可调试。

## 快速开始

```bash
pip install -e .
pip install pytest-cov  # 如果目标仓库的测试依赖它（很多 Python 项目会）
cp .env.example .env    # 填入 DeepSeek/OpenAI 兼容 key

# 挑一个真实仓库练手：这里克隆 tinydb 做演示，换成任何 git 仓库都行
git clone https://github.com/msiemens/tinydb ../tinydb-demo

repo-pilot detect --repo ../tinydb-demo                                    # 不花钱：看 adapter 识别结果
repo-pilot solve  --repo ../tinydb-demo --issue-file examples/tinydb_issue.md --plan-only  # 花一次调用：只出计划
repo-pilot solve  --repo ../tinydb-demo --issue-file examples/tinydb_issue.md              # 完整闭环
```

`examples/tinydb_issue.md` 描述的是一个真实可复现的边界值 bug（`<=` 查询漏掉恰好等于
边界的记录）。想亲眼验证 Verifier 的"基线对比"逻辑，可以先手动在 `../tinydb-demo` 里改坏
`tinydb/queries.py` 的 `__le__` 方法（把 `<=` 改成 `<`）再运行。

## 架构：模块与文件一一对应

```
Agent Core            orchestrator.py   状态机 BASELINE→PLAN→EXECUTE→VERIFY→REVIEW→REPORT
                      planner.py        issue → 结构化计划（JSON）
                      executor.py       工具调用主循环（流式）
                      reviewer.py       独立上下文复查（看不到 executor 的自我叙事）
Repository Intel      workspace.py      git 工作区：干净门禁 / diff / 回滚兜底 / 文件清单
                      adapters.py       唯一认识具体技术栈的地方（Python 深支持，Node 浅探测）
Tool Runtime          tools.py          read / list / search(ripgrep) / symbols(ast) /
                                        edit(片段替换) / write / bash / run_tests / git_diff
Verification          verifier.py       测试基线对比：fixed / regressed / improved / no_change
Safety                policy.py         路径越狱拦截、危险命令黑名单、.git 写保护
                      permissions.py    人对每次改动的知情与否决权
Observability         trace.py          run.jsonl 执行轨迹（评测的原始数据）
GitHub（外壳）        github.py         gh CLI 抓 issue；PR 创建是 Phase 4
```

## 核心设计决策

- **验证是灵魂**：改前跑测试存基线，改后再跑对比。"修好了"是测量结果，不是模型的一句话。
- **Reviewer 上下文隔离**：审查者只看 issue/计划/diff/测试对比这四样证据，看不到 executor
  "我改好了"的对话史 —— 自己查自己永远通过，隔离才有真审查。
- **Adapter 模式**：核心 agent 只问两个问题（什么项目类型？怎么跑测试？）。支持新技术栈
  = 加一个探测分支，核心零改动。
- **硬约束优先**：路径锁死仓库内、危险命令黑名单、改动文件数上限、轮数上限、
  git commit/push 一律封锁 —— 最后一步（commit）永远归人。
- **全程可观察**：每次状态转移、每个工具调用都进 `runs/<时间戳>/run.jsonl`，
  评测 = 对这些文件做统计。

## 路线图

- [x] **Phase 0–3（MVP，当前）** 完整闭环：Plan / Execute / Verify(基线对比+重试) /
      Review(独立上下文) / Trace / Python adapter / 安全策略
- [ ] **Phase 4 外壳** GitHub issue 直接抓取（已有雏形）、自动建分支与 Draft PR、
      NestJS adapter 深化（Jest/E2E 识别）
- [ ] **Phase 5 深水区（一次挖一个）** Docker 沙箱替代黑名单 / ts-morph 符号索引 /
      依赖图检索 / SWE-bench Lite 批量评测（成功率、token 成本、工具调用数）

## 背景

本项目源自一门"从零手写编程 agent"的自学课程（不依赖任何框架，从裸 API 逐步实现
工具调用循环、权限闸门、上下文隔离等核心机制）。RepoPilot 是这门课程的毕业设计，
把学到的机制应用到一个更贴近真实工程场景的任务上：解决真实代码仓库里的 issue。

## License

[MIT](LICENSE)
