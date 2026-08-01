# RepoPilot 评测线束

目标不是把 resolved 率做高，是让这个数字**经得起追问**。

下面每一节对应一个具体的追问，以及代码里回答它的那个位置。

---

## 第一次真实结果（dev 划分，2026-08-01）

冻结分母 15 | 环境失败 4 | gold 不可判定 2 | **可判定 9**

| 变体 | resolved | 平均 token | 平均成本 | 撞预算上限 |
|---|---:|---:|---:|---:|
| `full`（状态机+Explorer+Reviewer+停机策略） | 4/9 | 544,591 | $0.0606 | 6/9 |
| `no-explorer` | 4/9 | 497,778 | $0.0468 | 7/9 |
| `no-reviewer` | 5/9 | 451,835 | $0.0425 | 5/9 |
| **`baseline-b`（朴素 ReAct）** | **4/9** | **269,951** | **$0.0309** | **1/9** |
| `baseline-a`（单轮直接写补丁） | 0/9 | 4,861 | $0.0015 | — |

成对四格（同一批实例）：

| 对比 | full 独赢 | 对方独赢 | |
|---|---:|---:|---|
| full vs `no-explorer` | 1 | 1 | ← **阴性对照**，见下 |
| full vs `no-reviewer` | 0 | 1 | |
| full vs `baseline-b` | 0 | 0 | 解出的是**同一批**实例 |
| full vs `baseline-a` | 4 | 0 | |

### 三条结论，按可信度排序

**1（可信）：完整版比朴素 ReAct 贵一倍，成功率没有可测量的差别。**
两者解出**同一批** 4 条实例；token 544k vs 270k；9 条里完整版 6 条烧穿预算，
ReAct 只有 1 条。token 是连续量、方差远小于二值的 resolved，这条差异是实的。

**2（可信）：单轮直接写补丁 = 0/9。** 没有工具、没看过文件内容，靠记忆写出的
代码片段命中不了真实代码。这是诚实的下界 —— 这批题目**不存在**靠读 issue
猜就能对的。

**3（测不出来）：Reviewer 有没有用。**
效应量 1 条，噪声底噪 2 条（见下）。**不是"有害"，是这个实验没有分辨率。**

### 阴性对照：这套消融的分辨率有多低

`full` 与 `no-explorer` 的唯一差别是 `explore` 工具，而它在**全部实例里
一次都没被调用**。两者行为上完全等价，resolved 却在 2/9 条上相反
（`pylint-6903` ✅→❌，`pytest-7236` ❌→✅）。

`temperature=0` 不等于确定性 —— DeepSeek 是 MoE，采样固定了，专家路由和
批处理边界没有。

**所以：成对比较消得掉实例难度的差异，消不掉运行间方差。**
用户提的"成对比较"这条建议只解决了一半问题。要在 22% 的逐实例翻转率上
分辨 1/9 的真实差异，必须**同一配置重复跑 3–5 次取多数**，或把实例数提到 50+。
本轮没做，所以本轮不对 Reviewer 下结论。

（这个对照是意外得到的：我原本判断 `no-explorer` 必然和 `full` 相同、
称它"浪费 35 分钟"。它成了整轮 sweep 里最有价值的一个变体。）

### 失败在哪一步

13 条实例里 **6 条从未调用过 `edit_file`** —— 预算烧光时还停在读代码阶段。
`explore` 全程调用 0 次。agent 用 `run_bash` 147 次绕过 `run_tests`（13 次），
因为后者要跑整个测试目录。

**它不是在错误的方向上反复修改，它根本没走到"修改"那一步。**
瓶颈是上下文只增不减（README 已知缺口的第一条），而本轮加的编排机制
全在它下游。下一步该做上下文压缩，以及把 `run_tests` 改成 agent 能自己
缩范围的工具 —— 后者是去掉 test_patch 泄露时留下的坑。

### 口径与留痕

- 这是 **dev 划分**的结果。跑完之后我读了失败轨迹并据此改了代码，
  按本文档自己的规则，**dev 从此只能算开发样本，不能当对外数字**。
- 10 条 实例×变体 因线束 bug（见下"跨变体污染"）重跑过，
  每条留有 `rerun.json`。重跑后结论未变。

---

## 1. "你怎么证明 agent 没看到隐藏测试？"

**上一版没法证明，而且确实泄露了。** v1 脚本这样决定 agent 的测试命令：

```python
paths = re.findall(r"^diff --git a/(\S+)", inst["test_patch"], re.M)   # ← 泄露
```

从官方 `test_patch` 里抠出测试文件路径，再把 agent 的测试范围 scope 到那些文件。
省时间，但它把"官方隐藏测试藏在哪个文件"直接告诉了 agent。
不是恶意，是**顺手** —— 数据就在手边的字典里，抠一下就能用。

顺手是必然会发生的，所以边界不能是纪律，必须是结构：

| 推理侧 | 判分侧 |
|---|---|
| `instances.PublicInstance`（5 个字段，冻结） | `instances.SecretInstance` |
| `environment.py` / `inference.py` / `baselines.py` | `grader.py` / `failures.py` |
| 输入：Issue + Base Commit | 内部持有：test_patch / F2P / P2P / gold |
| 输出：Agent Patch + Trace | 输出：评测报告 |

三道防线，从静态到动态：

1. **AST 静态检查**（`tests/test_eval_isolation.py`）—— 推理侧模块的**代码**里
   不许出现 `grader` / `SecretInstance` / `FAIL_TO_PASS` / `test_patch`。CI 会拦。
2. **进程级隔离** —— `infer` 和 `grade` 是两条命令。推理进程的地址空间里
   从来没有出现过答案。这比"我这段没读那个字段"强得多。
3. **运行时断言** —— 真正发给 agent 的 payload 过一遍禁词
   (`assert_public_only`)；测试命令里出现用例级 `::` 直接抛
   (`assert_command_public`) —— 目录级路径是公开信息，用例级 id 只可能来自 F2P/P2P。

判分后还有一次事后扫描，分两级：

- `input` 级：喂给 agent 的东西里出现了官方测试标识 → **真泄露，必须是 0**
- `trace` 级：agent 自己产出的文本里撞上了 → 巧合，只记录

（实测撞到过：agent 自己补的测试随手起名 `test_empty_name_not_allowed`，
正好和官方 F2P 同名 —— 两边都取了最自然的名字而已。）

现在测试范围只来自两个公开来源：RepoPilot 自己的 adapter 探测，
以及仓库根目录下实际存在的测试目录。agent clone 完仓库自己 `ls` 一下
也能看到同样的东西，所以它们不构成任何额外信息。

### 这条修复是有代价的，代价也要说清楚

范围从"test_patch 涉及的几个文件"变成"整个测试目录"，测试就慢了 ——
seaborn 一轮全量套件要好几分钟，而 **agent 每一轮都要跑一次**。
后果是实打实的：默认 360 秒的测试超时会把这类实例一律判超时，
于是"堵住泄露"的代价变成"这些实例全废"。

处理方式是量出来、调上去，而不是偷偷把范围缩回去：
`env.json` 记录 `baseline_secs`（跑一遍全量要多久），
`infer --test-timeout`（默认 900 秒）把 agent 的测试超时提到匹配的量级。
更彻底的解法仍然是官方 Docker Harness —— 它的每实例镜像才能把这层成本摊掉。

---

## 2. "你是不是挑了对自己有利的实例？"

### 换 SWE-bench Verified

v1 是"从 Lite 里挑看起来能跑的仓库"。这句话有两个问题：挑选标准是我的环境，
而且挑选发生在**看过实例之后**。Verified 是官方请工程师逐条确认过
"可解、描述充分"的 500 条，挑选这一步不再由我做。

### 抽样总体是**事前声明**的，不是事后筛选

两者的区别是可信度的分界线：

- 事前声明：先按一条与实例无关的机械规则划定总体，再随机抽 —— 合法
- 事后筛选：抽完跑完，发现哪几条跑不动就删掉 —— 这是最常见的自欺

规则（三条都只关乎"这台笔记本装不装得上"，与题目难度无关）：

1. 纯 Python，pip 安装不需要编译 C / C++ / Cython 扩展
2. 测试用 pytest（判分协议依赖 pytest 的 `-rA` 摘要格式）
3. 仓库根目录有独立的测试目录

按此排除的仓库和原因，连同规则本身，一起写进了 `splits/*.json`：

| 排除 | 原因 |
|---|---|
| scikit-learn / matplotlib / astropy | 违反 1：要从源码编译扩展 |
| sympy | 违反 3：测试散在 `sympy/*/tests/`，无顶层测试目录 |
| django | 违反 2：自带 `runtests.py`，不走 pytest 协议 |

排除是**按仓库**做的，仓库内一条实例都不挑 —— 挑到实例粒度就又变成筛选了。
想扩大总体，正确做法是把环境层换成官方 Docker Harness，而不是放宽这里的规则。

### dev / test / holdout 三份冻结清单

在同一批实例上反复改提示词，最终一定会过拟合 —— 不是模型过拟合，是**我**过拟合：
我读了失败日志，然后照着日志改代码。

> 看过某个实例的 issue、失败日志或 agent 轨迹之后，
> 它就不再是严格意义上的测试样本，只能算开发样本。

| 划分 | 数量 | 用法 |
|---|---:|---|
| `dev` | 15 | 随便调，随便看轨迹 |
| `test` | 30 | 只在阶段性版本上跑，看聚合数字，不逐条读轨迹 |
| `holdout` | 10 | 从未看过，留给最终对外的那个数字 |

抽样是确定性的（`sha256(种子:instance_id)` 排序，与数据集行顺序无关），
清单带校验和、提交进 git，`load_split()` 每次读都会校验它没被改过。

### 分母不许动

跑不起来的实例**留在分母里**，只在报告中单列为"环境失败"。
`report.summarize()` 的分母固定取 manifest 里冻结清单的长度，不是"跑完的行数"。
同时给两个口径（全量 / 仅 gold 可判定），不挑对自己有利的那个报。

---

## 3. "为什么两次结果不同？"

每次 `infer` 在实例开跑**之前**写一份不可变的 manifest：

```json
{
  "run_id": "v0.5.0-dev-full",
  "agent_commit": "ccf9f57-dirty",
  "variant": "full",
  "model": "deepseek-chat", "temperature": 0.0,
  "prompt_hash": "2e58f5de8e2ce9a9",
  "tool_schema_hash": "eceeee5d66021037",
  "budget_fingerprint": "f0c6bc2409d927d3",
  "instance_checksum": "bdd33b5ea3c1b865",
  "harness_version": "0.2.0"
}
```

`prompt_hash` 哈希的是**所有真的会发给模型的**提示词（executor / planner /
reviewer / explorer / runtime 五段）。理由很实在：提示词是最容易随手改一句、
又最影响结果的东西，而人不会记得自己改过。哈希是自动的。

`agent_commit` 带 `-dirty` 后缀时，意味着那次结果不可复现 —— 报告里会照实显示。

同一个 `run_id` 写入两份不同配置会直接报错：不可变不是形容词，是行为。
`manifest.comparable()` 在 `compare` 时检查两次运行能不能直接比：
实例清单 / 模型 / 判分协议不同是**阻断项**；提示词、工具、预算不同只是提示
（消融实验里预算本来就该差一项）。

---

## 4. "预算一样吗？成本呢？"

`repopilot/budget.py` 把"允许花多少"变成一个可哈希、可派生、可强制的对象。

```python
FULL         = Budget()
NO_REVIEWER  = FULL.variant(allow_reviewer=False)      # 只差这一项
NO_EXPLORER  = FULL.variant(allow_explorer=False)
NO_HALTING   = FULL.variant(allow_halting_policy=False)
```

消融变体之间**只差能力开关**，规模和 token 完全一致（`tests/test_budget.py`
逐字段守着这条）。否则比出来的是钱的差，不是设计的差。

预算是**硬约束**：`Ledger.exhausted()` 在 executor 每一圈开头被检查，
超了就 return。"请节约使用 token" 写在提示词里是软约束，模型可以无视。

账本分角色记账，所以"Reviewer 到底多花了多少"是个能回答的问题。
成本用真实价格表算，且**单列缓存命中价** —— 多轮 agent 的 prompt 前缀高度重复，
把命中当未命中算会把成本高估好几倍。没有价格表的模型报"（无价格表）"，
不报 `$0.0000`（那和"免费"长得一模一样）。

报告里效果和成本永远并排：

| 变体 | Resolved | 平均 Token | 平均成本 | 平均工具调用 |
|---|---:|---:|---:|---:|
| full | 1/1 (100%) | 89,899 | $0.0098 | 14.0 |
| baseline-b | 1/1 (100%) | 43,329 | $0.0051 | 11.0 |
| baseline-a | 0/1 (0%) | 2,489 | $0.0008 | 0.0 |

（`pallets__flask-5014` 单实例冒烟测试的真实数据，不是示意。它已经透露了一个
值得追问的信号：**朴素 ReAct 以一半的成本做到了同样的事**。一条实例说明不了
什么，但这正是基线存在的意义 —— 没有这一行，"完整版 resolved" 读起来像是
状态机的功劳。）

---

## 5. "状态机 / Explorer / Reviewer 到底带来了什么？"

只测自己等于什么都没测。三个基线，同模型、同实例、同预算规模：

| 变体 | 有什么 |
|---|---|
| `baseline-a` | Issue + 仓库文件清单 → 模型直接吐一个 diff。没有工具、没有验证、没有重试 |
| `baseline-b` | 搜索 → 阅读 → 修改 → 测试 的裸 ReAct 循环。没有状态机 / Explorer / Reviewer / 基线对比 |
| `full` | 状态机 + Explorer + 验证重试 + Reviewer + 停机策略 |

基线要**弱得诚实，不要弱得可笑**：基线 A 拿得到仓库文件清单（不给它连路径
都写不对，那不是"简单"是"残废"），基线 B 拿得到和主 agent 相同的核心工具。
它们唯一少的就是被消融掉的那个结构。

### 一个真实的教训：基线 A 挂零挂了三次，头两次不是它的错

第一版让基线 A 直接输出 `git apply` 能用的 unified diff。三轮实测：

1. **输出被 `max_output_tokens=4096` 从中间切断** → 半截补丁必然打不上。
   这是**我们的预算**掐断了它唯一一次输出，不是它的能力问题。
   → 上限提到 8192（对**所有变体**一起提，预算保持一致才可比），
   并单独识别 `finish_reason == "length"`，归入 `OUTPUT_TRUNCATED` 而不是
   `PATCH_APPLY_FAILED` —— 混在一起等于把评测者自己造成的失败记到被测方案头上。
2. **提到 8192 之后仍然截断**，因为它输出的是一份退化的 diff：几十个几乎相同的
   `@@` hunk，从头到尾每隔十行加一个空行，行号全是编的。原因是结构性的 ——
   基线 A 只拿得到文件清单，从来没看过文件内容，它**不可能**写出正确的
   上下文行和行号。于是这个基线测出来的一大半是"会不会写 diff 语法"。
3. **换成 search/replace 编辑格式**（和 RepoPilot 自己的 `edit_file` 同语义：
   唯一命中才替换）。约束一点没松 —— 仍是一次调用、无工具、无验证、无重试 ——
   但混淆项没了。这一次它的失败是：

   ```
   src/flask/blueprints.py: search 片段没有命中（它写的代码和真实代码对不上）
   ```

   **这才是一个诚实的基线失败**：它不知道那段代码长什么样。
   2,489 tokens、2.2 秒，卡在定位和知识上 —— 正是一个没有工具的单轮方案该卡的地方。

如果没查这一步，报告里会写"基线 A 0%"，而那 0% 有一大半是我们自己造成的，
主方案的数字会凭空虚高。**基线做得太弱，抬高的是自己的数字，骗的是自己。**

### 消融必须成对

分别跑两批不同实例得到"完整版 36%、去 Reviewer 30%"，你只知道有 6 个点的差。
在**同一批实例**上成对比较，得到的是四个格子：

| 对比 | n | 完整版独赢 | 简化版独赢 | 同时成功 | 同时失败 |
|---|---:|---:|---:|---:|---:|
| 有/无 Reviewer | … | … | … | … | … |
| 有/无 Explorer | … | … | … | … | … |
| 状态机 / ReAct | … | … | … | … | … |

"6 个点"是（独赢 − 独输）的净值，它把**简化版独赢**那一列藏起来了 ——
而那一列正是「这个结构反而帮了倒忙」的实例，也正是最该追问的地方。

---

## 6. "Reviewer 真的有用吗？"

"我们有 Reviewer" 只是一句架构描述。要证明它有效，得能回答四个问题，
而一个"通过/不通过"的布尔值一个都答不了。所以 Reviewer 输出结构化结果：

```json
{
  "decision": "accept",
  "issue_addressed": true,
  "tests_sufficient": false,
  "scope_minimal": true,
  "risks": ["没有覆盖空输入情况"],
  "requested_actions": ["增加空输入测试"]
}
```

`requested_actions` 是其中最重要的字段：它既是送回 EXECUTE 的下一轮指令
（Reviewer 因此从"终点"变成了"环"），也是判断它是不是在说车轱辘话的依据
——同一个要求提第二次，停机策略直接终止重修。

报告统计：驳回次数 / 挽救实例（驳回后转 resolved）/ 疑似误杀 / 额外 token。
"疑似"两个字是认真的：真正的误杀要看**被驳回的那一版**能不能过官方判分，
而我们只对最终补丁判了分 —— 不夸大。

---

## 7. "resolved 就等于补丁是好的吗？"

不等于。`repopilot/patch_audit.py` 在判分**之前**扫一遍补丁，分两件事：

**作弊检测** —— 把测试改绿有很多不体面的方法：

`test_tampering_detected` `suspicious_skip_added` `assertion_removed`
`evaluation_file_touched` `dependency_pin_changed` `hardcoded_test_input`

官方判分会在打分前还原测试文件，所以这些手段**拿不到分**。但"没得逞"不等于
"没发生"：一个反复试图改测试的 agent 和一个老实改源码的 agent，
即使 resolved 率相同，也不是同一个 agent。

**质量指标** —— 改了几个文件、增删几行、有没有夹带纯格式化、有没有新增依赖、
有没有改公共 API、有没有留下 `print` 调试、是不是大范围重构。
这些都不影响 resolved，但都影响"能不能合进主干"。

关于 gold 的红线：只报**修改范围的重合度**（文件集合的 Jaccard），
**绝不用文本相似度判定正确性** —— 同一个问题有多种正确修法，
按相似度打分等于奖励抄标准答案的形状。

---

## 8. "失败了，然后呢？"

v1 报告只有一句"3/8 resolved"。这句话不指导任何行动 ——
它不告诉你该去改 Explorer 还是改 Reviewer。

`failures.py` 把每个失败实例归入**一个**主因（只归一类：三个都记等于没记，
而下一步只能做一件事），判据全部是可观测量，不是让模型自述失败原因
（它会给一个听起来很合理的故事）：

```
10 个失败
├── 4 个根因定位错误        [ROOT_CAUSE_WRONG]   → 去改 Explorer 和代码检索
├── 2 个修改不完整          [INCOMPLETE_FIX]     → 去改收敛策略
├── 2 个工具循环            [TOOL_LOOP]          → 去改停机策略
├── 1 个引入回归            [REGRESSION]
└── 1 个超时                [TIMEOUT]
```

分类顺序即优先级（越靠前越根本）：环境 → gold 不可判定 → 线束崩溃 →
超时/预算 → 补丁打不上 → 空补丁 → 改测试作弊 → 回归 → 工具循环 →
Reviewer 驳回 → 改错文件 → 修不完整 → 根因错。

其中两条最需要区分：**一条 F2P 都没转绿**是根因整个判断错了，
**转绿了一部分**是方向对没修完 —— 对应的下一步完全不同。

---

## 9. "环境伪影怎么办？"

这是本地评测最大的噪声源，两条**机械规则**（不是手工版本表）把它压了下去：

**解释器版本** 按仓库自己声明的 `requires-python` 选，偏好从 3.9 起。
2013–2023 年的代码丢进 Python 3.12，会触发当年不存在的 DeprecationWarning，
而很多仓库配了 `filterwarnings = error` —— 一个警告把 58 个测试全变成 ERROR。

**依赖版本** `uv pip install --exclude-newer <base_commit 日期>`，
只允许安装提交那天之前发布的版本。2023 年的 flask 2.3 配 2025 年的
werkzeug 3.x，第一行就 `AttributeError`。

为什么强调"机械"：v1 维护过一张手工钉版本表
（`{"pallets__flask-4992": ["werkzeug<2.3"]}`）。它能work，但每一条都是我可以
随时调到"这条终于过了"为止的魔法数字。**一张能被调的表，就是一个能被调的分子。**
现在两条规则的依据分别是仓库元数据和提交日期，都是公开事实，谁跑都一样。

实测效果（`pallets__flask-5014`）：Python 3.12 + 今天的依赖 → `gold_ok=False`
（连官方标准答案都过不了）；换成 3.9 + 2023-03-11 之前的依赖 → `gold_ok=True`，
基线全绿。

**gold 校准在推理之前跑。** 事后跑就有了"哪条对我不利就说它是伪影"的空间。

没装 `uv` 会退回当前解释器和今天的依赖 —— 但这条退化路径会写进
`env.json` 的 `install_note` 并出现在报告里，而不是默默发生。

### dev 划分的实测结果（推理开始前，与 agent 表现无关）

| | 条数 | |
|---|---:|---|
| 冻结分母 | **15** | 不许动 |
| 环境失败 | 4 | requests-1724（pytest 2.4.2 跑不了 py3.9）、sphinx-7910/8120（uv 的独立 CPython 不带 `_testcapi`）、xarray-3151（日期约束无解，退回今天的 numpy，`np.unicode_` 已删） |
| gold 也过不了 | 2 | requests-6028、pytest-5631 |
| **gold 可判定** | **9** | 判分器在这 9 条上可信 |

环境就绪率从 4/15 爬到 11/15，中间修掉的五个 bug **全是我自己造的**，
形状完全一样：**对仓库做假设，而不是读它自己声明了什么**。留档在这里，
因为它是这类线束最典型的失效模式：

| bug | 代价 |
|---|---|
| `re.match(r"^(setuptools\|wheel)\b", "setuptools-scm[toml]")` **是匹配的**（`-` 是非单词字符），于是自己的过滤器剔掉了 `setuptools-scm` | 3 条 pytest 挂在 `No module named '_pytest._version'` |
| 关掉构建隔离后没装 `[build-system] requires` | 构建"成功"但产物不全 |
| 现代 setuptools 留在了运行环境里，`import pkg_resources` 抛弃用警告 × 仓库的 `filterwarnings = error` | xarray / sphinx 收集阶段崩 |
| extras 的字典写法 `"testing": [...]`（冒号不是等号）没认 | pytest 少装 hypothesis |
| 部分收集失败一票否决 | 丢掉一条收集成功 1989 个测试的实例 |

最后一条最值得记：**我的筛选标准比判分本身还严**。判分只跑 F2P/P2P 指定的
那几个文件，而我因为两个无关文件 import 失败就扔掉整条实例 ——
用一个比判分更严的标准去筛样本，白白缩小可用分母。

### 跨变体污染：`reset()` 自己漏了一步

第一次 sweep 跑完才发现的第六个 bug，也是危害最大的一个。

`capture_patch()` 用 `git add -A -N` 把未跟踪文件加进索引 —— 不这样
`git diff` 看不见 agent 新建的文件。但被 `-N` 加进索引之后：

    git clean -fd      不删【已进索引】的文件
    git checkout -- .  又把它从索引恢复出来

三条命令各自都对，组合起来留下一个谁也删不掉的文件。于是 `full` 变体里
agent 建的 `tmp/test_repro.py` 活过了复位，一路传染到后面每一个变体：

- 把 `no-reviewer` / `no-explorer` 的干净工作区门禁直接崩掉
- 被当成 `baseline-a` 的"补丁"交上去判分，判分报
  `already exists in working directory`，一条本可能 resolved 的实例被记成补丁打不上

**跨变体污染正是 `reset()` 存在的唯一理由，而它自己漏了这一步。**
修法是一行（前置 `git reset -q`），但真正的修复是
`tests/test_eval_environment.py` 里那条不修就会挂的测试：
它断言 `capture_patch()` 之后 `reset()` 必须真的把新建文件删掉。
这条不变量此前只存在于我的意图里。

受污染的 10 条 实例×变体 已按事前定死的三条机器可查判据
（崩溃 / 判分报 `already exists` / 补丁只含幽灵文件）识别并重跑，
每条留 `rerun.json` 说明原因与版本。改的是线束的工作区卫生，
`repopilot/` 一行未动 —— `prompt_hash` 与 `tool_schema_hash` 不变。
重跑后结论未变。

---

## 10. 缓存策略：什么能复用，什么绝对不能

| 可以缓存（属于环境） | 绝对不能跨版本复用（属于 agent 产出） |
|---|---|
| 仓库克隆、base commit | agent 的代码分析结论 |
| 依赖安装层、venv | Explorer 的答案 |
| adapter 探测结果 | 失败后的模型上下文 |
| 基线测试结果、gold 校准结果 | 上一次的 agent 补丁 |

保证机制很朴素：每次推理**开始前和结束后**都对工作树做一次强制复位
（`environment.reset()` = `git checkout -- . && git clean -fd`）。
少了这一步，缓存就变成了污染 —— `no-reviewer` 版会站在 `full` 版的补丁上开跑。

---

## 用法

```bash
pip install -e ".[dev]" && pip install uv pyarrow

# 一次性：冻结实例划分（已提交进 git，正常情况下不用再跑）
python eval/run.py freeze

# 环境层（可缓存、与 agent 版本无关；不花 API 的钱）
python eval/run.py prepare   --split dev
python eval/run.py calibrate --split dev        # 必须在 infer 之前

# 推理（这个进程里不会出现 test_patch / F2P / P2P）
python eval/run.py infer --split dev --variant full        --run-id v0.5.0-full
python eval/run.py infer --split dev --variant no-reviewer --run-id v0.5.0-noreview
python eval/run.py infer --split dev --variant baseline-b  --run-id v0.5.0-react

# 判分（独立进程，此时才载入答案）
python eval/run.py grade --run-id v0.5.0-full

# 报告
python eval/run.py report  --run-id v0.5.0-full
python eval/run.py compare --a v0.5.0-full --b v0.5.0-noreview --extra v0.5.0-react
```

变体：`full` / `no-reviewer` / `no-explorer` / `no-halting` /
`baseline-a` / `baseline-b`。

产物布局：

```
eval/
  splits/verified_{dev,test,holdout}.json   冻结清单（进 git）
  envs/<instance_id>/                       venv + 工作树 + gold 校准（缓存，不进 git）
  runs/<run_id>/manifest.json               不可变清单
  runs/<run_id>/<instance_id>/
      inference_input.json   真正发给 agent 的东西（泄露断言的物证）
      agent.patch            补丁
      inference.json         停机码、账本、审查轮次、补丁审计
      grade.json             resolved / F2P / P2P / gold / 泄露扫描
  runs/<run_id>/REPORT.md
```

---

## 已知边界（写出来，不藏着）

- **环境层不是官方 Docker Harness。** 两条机械规则把伪影压下去了，但比不上
  官方的 per-instance 镜像。这是路线图上的下一步，也是扩大抽样总体的前提。
- **总体限于 7 个仓库、106 条实例。** 这是事前声明的限制，写在
  `splits/*.json` 的 `sampling_frame` 里 —— 但它确实意味着结论不能外推到
  需要编译扩展的科学计算栈。
- **Reviewer 误杀只能给"疑似"。** 真正的误杀要对被驳回的那一版单独判分，
  目前没做。
- **`resolved` 仍然只是"指定测试通过"。** 补丁审计补上了一部分质量维度，
  但没有人工复核。
