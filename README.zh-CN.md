[English](README.md) | 中文

# RepoPilot：面向真实代码仓库的 Issue Resolution Agent

[![CI](https://github.com/zsyayo112/repopilot/actions/workflows/ci.yml/badge.svg)](https://github.com/zsyayo112/repopilot/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org)

给它一个 git 仓库和一个 issue，它负责走完整条软件工程闭环：

```
Issue → 基线测试 → [启动应用 → 浏览器复现] → 修改计划 → 代码定位与编辑
      → 验证（测试 + lint/类型检查/构建 + 同一份复现脚本）
      → 失败重试 → 独立审查 → Git Diff / 报告 → 清理
```

方括号里的两步是可选的（`--with-runtime` / `--scenario`），专门对付单元测试
**结构上看不见**的那类 bug：按钮渲染出来了但点不动、跳转到错误的路由、
接口返回成功而页面没有更新。

**手写、无框架。** 没有 LangChain / LangGraph —— agent 循环、工具调度、权限闸门、
结构化输出、状态机全部裸写。这既是学习方式，也是立场：核心机制必须可理解、可调试。

## 快速开始

```bash
pip install -e .
pip install pytest-cov  # 如果目标仓库的测试依赖它（很多 Python 项目会）
cp .env.example .env    # 填入 DeepSeek/OpenAI 兼容 key

# 挑一个真实仓库练手：这里克隆 tinydb 做演示，换成任何 git 仓库都行
git clone https://github.com/msiemens/tinydb ../tinydb-demo

repo-pilot detect --repo ../tinydb-demo    # 不花钱：看 adapter 识别结果 + monorepo 单元
repo-pilot doctor --repo ../tinydb-demo    # 不花钱：环境到底装好了吗
repo-pilot solve  --repo ../tinydb-demo --issue-file examples/tinydb_issue.md --plan-only  # 花一次调用：只出计划
repo-pilot solve  --repo ../tinydb-demo --issue-file examples/tinydb_issue.md              # 完整闭环
```

`examples/tinydb_issue.md` 描述的是一个真实可复现的边界值 bug（`<=` 查询漏掉恰好等于
边界的记录）。想亲眼验证 Verifier 的"基线对比"逻辑，可以先手动在 `../tinydb-demo` 里改坏
`tinydb/queries.py` 的 `__le__` 方法（把 `<=` 改成 `<`）再运行。

对"必须渲染出来才能复现"的问题，加上运行时那一层
（Playwright 是**可选依赖**，不装的话上面所有功能照常可用）：

```bash
pip install -e ".[browser]" && playwright install chromium

repo-pilot solve --repo ../my-next-app --issue-file issue.md --with-runtime
repo-pilot solve --repo ../my-next-app --issue-file issue.md \
                 --scenario examples/scenario_mobile_booking.json
```

## 架构：模块与文件一一对应

```
Agent Core            orchestrator.py   状态机（10 状态，其中 3 个是条件状态）
                      planner.py        issue → 结构化计划（JSON）
                      executor.py       工具调用主循环（流式）
                      explorer.py       只读探索子 agent —— 上下文隔离，为的是省预算
                      reviewer.py       独立上下文复查 —— 同一套隔离，为的是去偏见
Repository Intel      workspace.py      git 工作区：干净门禁 / diff / 回滚兜底 / 文件清单
                      adapters/         唯一认识具体技术栈的地方
                        base.py           契约 + 统一名词（TestReport / ServiceSpec / …）
                        registry.py       探测登记处、RepoProfile、monorepo 扫描
                        symbols.py        多语言符号提取（Python 真 AST，其余正则）
                        *_stack.py        一门语言一个文件，该语言的全部知识
Tool Runtime          tools.py          28 个工具，分 3 个能力组（默认只暴露 13 个）
                      runtime.py        Runtime Manager：起服务、健康检查、日志、进程组清理
                      browser.py        Browser Session（Playwright 可选）—— 带 ref 的页面快照
                      evidence.py       Evidence Collector：控制台/网络/截图，写盘前脱敏
                      scenario.py       结构化复现：修改前后跑完全相同的步骤和断言
Verification          verifier.py       基线对比 + 验证流水线（lint→typecheck→test→build）
                      doctor.py         环境体检：区分"代码坏了"和"机器没准备好"
Safety                policy.py         路径越狱、命令黑名单、不可逆意图拦截
                      permissions.py    人对每次改动的知情与否决权
Observability         trace.py          run.jsonl 执行轨迹（评测的原始数据）
GitHub（外壳）        github.py         gh CLI 抓 issue；PR 创建是 Phase 4
```

## 支持的技术栈

核心 agent 框架无关；所有技术栈知识都关在
[`adapters/`](repopilot/adapters/) 里。加一种技术栈 = 一个 `*_stack.py` +
注册表里一行，**核心零改动**。认不出的项目也能用 `--test-cmd` 兜底。

每个 adapter 要回答的不再是两个问题，而是七个：这是什么项目、怎么跑测试、
**这段输出什么意思**、还要跑哪些验证、怎么把它跑起来、需要装什么、这个文件里有什么符号。

| 技术栈 | 靠什么识别 | 测试命令 | 输出怎么解析 | 能启动 |
|--------|-----------|----------|-------------|--------|
| Python | `pyproject.toml` / `setup.py` / `pytest.ini` / … | `pytest` | pytest 文本 | Django / Flask / FastAPI |
| Node | `package.json` | `npm`/`pnpm`/`yarn`/`bun test` | jest / vitest / mocha，或 `--json` | Vite |
| NestJS | `package.json` 里有 `@nestjs/core` | 同上 | 同上 | `nest start --watch` |
| Next.js / Nuxt / Angular | `next` / `nuxt` / `@angular/core` | 同上 | 同上 | dev server |
| Go | `go.mod` | `go test ./...` | `--- FAIL:` 文本**或** `-json` | — |
| Rust | `Cargo.toml` | `cargo test` | cargo 文本（所有 target 累加） | — |
| Java (Maven) | `pom.xml` | `mvn -q -B test` | **JUnit XML** → Surefire 文本 | Spring Boot |
| Java (Gradle) | `build.gradle[.kts]` | `./gradlew test` | **JUnit XML** → Gradle 文本 | Spring Boot |
| Ruby | `.rspec` / `spec/` / `Gemfile` | `bundle exec rspec` | rspec / minitest | Rails |
| _（兜底）_ | `Makefile` 里有 `test:` 目标 | `make test` | 只认 exit code | — |
| _任意_ | `--test-cmd "<命令>"` | 你指定的命令 | **仍然用探测到的解析器** | 同上 |

解析器一律【先试结构化（JSON/XML）、再退回文本】。所以把命令升级成
`--test-cmd "go test -json ./..."`，解析精度立刻变高，一行代码都不用改。

### monorepo

一个仓库不再假设只有一种技术栈。`scan()` 会扫出所有项目单元，
验证时只跑改动真正涉及的那几个：

```
$ repo-pilot detect --repo ../shop
识别到 5 个项目单元（monorepo）
  .              node       pnpm test
  apps/api       nestjs     pnpm test
  apps/search    go         go test ./...
  apps/web       nextjs     pnpm test
  apps/worker    python     pytest
```

每个单元有**自己的基线**（你只能比较你测量过的东西），也有自己的解析器 ——
workspace 根目录经常什么都读不懂，而 `apps/web` 说的是 vitest。

## 运行时验证（可选开启）

对"必须渲染出来才存在"的 bug，工具背后是三个各有生命周期的组件：

- **Runtime Manager** 按依赖顺序起服务，并等到它**真的可用**。`npm run dev` 一秒就返回，
  Next.js 还要再编译十几秒。只检查"进程还在"，agent 会立刻去打开页面、拿到连接被拒，
  然后开始"修"一个根本不存在的 bug。就绪必须有真凭据：HTTP 健康检查，或日志里的就绪行。
- **Browser Session** 交给模型的是**带编号的可交互元素表**，不是 HTML（会爆上下文），
  也不是 CSS 选择器（改一次样式就失效）。每次快照给 DOM 打上稳定的 ref，
  `click(ref="e12")` 就精确指一个元素 —— 并且会标出**被遮挡或被禁用**的元素，
  "按钮画得好好的但点了没反应"就是这么抓到的。
- **Evidence Collector** 从页面加载的第一毫秒就开始录控制台错误、未捕获异常、失败请求 ——
  错误往往就发生在首屏那几百毫秒，等 agent 反应过来早就过去了。
  凭据在**写盘之前**就被脱敏：已经落盘的秘密就已经泄漏了。

复现步骤被固化成 [JSON scenario](examples/)，这样修改前那一次和修改后那一次，
可以证明是同样的操作、同样的断言。如果 scenario 在**改动之前**就通过了，
RepoPilot 会停下来报告"这个 issue 在当前代码上不成立"—— 这是一份合格的产出，不是失败。

## 核心设计决策

- **验证是灵魂**：改前跑测试存基线，改后再跑对比。"修好了"是测量结果，不是模型的一句话。
- **不确定必须显式，不能伪装成确定**：每份 `TestReport` 带一个 `parsed` 标志。
  旧版用正则抠 `(\d+) passed`，抠不到就返回 0 —— 于是"真的 0 个失败"和"我没看懂这段输出"
  在数据上一模一样，verifier 对着一堆假的 0 做比较，还以为自己看得懂。
  现在它会报 `confidence: low`，并且明确告诉 Reviewer：别把"没有新增失败"当成安全的证据。
- **Reviewer 上下文隔离**：审查者只看客观证据，看不到 executor "我改好了"的对话史 ——
  自己查自己永远通过，隔离才有真审查。`explorer.py` 复用同一套机制，
  换来的是另一种收益：定位一个 bug 读的那十个文件，永远不进主上下文。
- **Adapter 模式**：核心永远不对 `kind` 做分支。支持新技术栈 = 加一个文件 + 一行登记，
  状态机、executor、reviewer 一行都不动。
- **硬约束优先**：路径锁死仓库内、危险命令黑名单、看起来不可逆的浏览器操作
  （付款/删除/下单）弹回给人、子进程环境里剥掉所有 API key、浏览器只能访问 localhost、
  改动文件数与轮数上限。git commit/push 一律封锁 —— 最后一步永远归人。
- **工具渐进式暴露**：28 个工具，默认只给 13 个。工具列表每轮都要重发，
  而且选项越多选错的概率越高 —— 给一个纯 Python 库任务塞九个浏览器工具，
  它会真的去试着开浏览器。
- **谁起的谁负责关**：服务和浏览器由 `CLEANUP` 状态**和** `finally` 双重收尾：
  状态那层是可观察的，`finally` 那层是可靠的。`stop()` 还会等端口真的释放 ——
  进程退出和内核回收 socket 不是同一个时刻。
- **全程可观察**：每次状态转移、每个工具调用都进 `runs/<时间戳>/run.jsonl`，
  评测 = 对这些文件做统计。

## 路线图

- [x] **Phase 0–3（MVP，当前）** 完整闭环：Plan / Execute / Verify(基线对比+重试) /
      Review(独立上下文) / Trace / 多技术栈 adapter / 安全策略 / CI + lint
- [x] **多语言深化** adapter 从回答 2 个问题变成回答 7 个：各框架测试输出解析
      （pytest / jest / vitest / mocha / go / cargo / JUnit XML / rspec / minitest，
      结构化优先）、验证流水线（lint → typecheck → test → build）、多语言符号骨架、
      环境依赖声明、monorepo 扫描 + 每个单元各自的基线
- [x] **运行时验证** Runtime Manager（按依赖起服务、真实健康检查、日志、进程组清理）、
      Browser Session（带 ref 的快照、遮挡/禁用检测、只允许 localhost）、
      Evidence Collector（控制台/网络/截图，写盘前脱敏）、
      结构化复现脚本（修改前后重放完全相同的步骤）
- [ ] **Phase 4 外壳** GitHub issue 直接抓取（已有雏形）、自动建分支与 Draft PR
- [x] **SWE-bench Lite 迷你评测** — 轻量本地设定下尝试 8 条纯 Python 实例：
      **可判定实例上 3/3 resolved**（gold 校准：官方标准答案必须能在本地通过，
      否则判为环境伪影——Python 3.12 杀死了多个 2022 时代的测试套件），
      保守口径 3/8。判分复刻官方协议（还原测试文件 → 打官方 test_patch →
      FAIL_TO_PASS + PASS_TO_PASS 全过），含官方的空格截断 ID 匹配规则。
      见 [`eval/`](eval/) 与 [`eval/RESULTS.md`](eval/RESULTS.md)。
- [ ] **Phase 5 深水区（一次挖一个）** Docker 沙箱替代黑名单
      （容器边界是内核级的，子串黑名单不是）/ tree-sitter 或 LSP 替代正则符号提取 /
      上下文压缩 / 依赖图检索 / 官方 Docker 线束下跑完整 SWE-bench Lite

### 已知缺口（主动交代，不藏）

- **没有上下文压缩**：历史只增不减，评测峰值撞过 42.6k / 64k 窗口。
  `explore` 把定位阶段的脏上下文挡在外面，缓解了压力，但它**不等于**压缩。
- **Python 之外的符号提取是正则**：会漏报也会误报。它用零新增依赖换到了这个功能
  80% 的价值（"大概有什么、在第几行"）；真正的解法是 tree-sitter 或 LSP。
- **黑名单和不可逆意图拦截都是子串匹配** —— 天生的弱防御。一个写着 "Continue"
  的支付按钮就能大摇大摆走过去。结构性的答案是：不可逆操作归人，agent 只该跑在测试数据上。
- **没有语义级相关性判定**：跑偏护栏都是量上的（文件数、片段唯一命中）。
  它在允许范围内改错了东西，抓不到。
- **没有持久化**：跑挂了从头再来。

## 开发

```bash
pip install -e ".[dev]"   # 安装 pytest + ruff
pytest -q                 # 106 条测试，离线，无需 API key
ruff check .              # lint
```

运行时那部分测试**没有打桩**：它真的起一个 `http.server`，真的断言启动会等到
健康检查通过，真的断言停止之后端口被释放（包括 shell → 子进程 → 孙子进程
这条链路 —— 那才是 `pnpm dev` 的真实形状）。全 mock 掉就等于什么都没测。

CI 会在每次 push / PR 时于 Python 3.10–3.12 上跑 lint + 测试。如何新增一个
Repository Adapter，见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 背景

本项目源自一门"从零手写编程 agent"的自学课程（不依赖任何框架，从裸 API 逐步实现
工具调用循环、权限闸门、上下文隔离等核心机制）。RepoPilot 是这门课程的毕业设计，
把学到的机制应用到一个更贴近真实工程场景的任务上：解决真实代码仓库里的 issue。

## License

[MIT](LICENSE)
