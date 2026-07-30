# examples/

| 文件 | 用途 |
|------|------|
| `tinydb_issue.md` | 一个真实可复现的边界值 bug（`<=` 查询漏掉边界记录），用来演示 Verifier 的基线对比 |
| `scenario_mobile_booking.json` | 结构化复现脚本示例：移动端点击不跳转 |

## 怎么用复现脚本

```bash
repo-pilot solve --repo ../targets/shop \
                 --issue-file issue.md \
                 --scenario examples/scenario_mobile_booking.json
```

给了 `--scenario` 会隐含开启 `--with-runtime`，流程变成：

```
基线测试 → 启动应用 → 跑复现脚本（必须失败，否则说明 issue 不成立）
        → 计划 → 改代码 → 重跑测试 + lint/typecheck/build
        → 跑【同一份】复现脚本（必须通过）→ 独立审查 → 报告 → 清理
```

### 为什么要写成 JSON 而不是让 agent 自己去点

因为「修改前点了三下、修改后点了两下」这种事在对话历史里看不出来。
把步骤和断言固化成数据，修改前后跑的就是**同一份**，
「修好了」才成为可比较的事实，而不是模型的一句自我评价。

### 字段说明

**steps** —— `action` 可选：

| action | 参数 | 说明 |
|--------|------|------|
| `open` | `url` | 相对路径会自动拼上服务地址 |
| `click` | `ref` 或 `role`+`name` | 优先用语义定位，别写 CSS 选择器 |
| `fill` | `text` + (`ref` 或 `label`)，可选 `submit` | `submit=true` 填完按回车 |
| `select` | `value` + (`ref` 或 `label`) | 下拉框 |
| `reload` | — | 刷新 |
| `viewport` | `width`、`height` | 中途改窗口尺寸 |
| `screenshot` | `name` | 存图 |

**assertions** —— `type` 可选：

| type | value | 说明 |
|------|-------|------|
| `url` / `url_not` | 正则 | 当前 URL 匹配 / 不匹配 |
| `text_present` / `text_absent` | 文本 | 页面上有 / 没有这段文字 |
| `element_exists` / `element_absent` | 文本或 `role=button:名称` | 元素在 / 不在 |
| `element_enabled` | 同上 | 元素存在**且可点** —— 抓「看起来正常但点不动」 |
| `no_console_errors` | — | 没有 JS 控制台错误 |
| `no_failed_requests` | — | 没有 4xx/5xx 或连接失败的请求 |

**功能问题请用断言，不要靠截图。** 按钮画得再漂亮，点不动就是坏的；
反过来页面看着乱，可能只是少了一张图。截图只用来看视觉问题（错位、遮挡、响应式）。
