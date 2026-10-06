# code-review Skill

> 基于 OpenClaw AgentSkill 规范的 Python 代码评审技能，支持一键对 Git 仓库进行 SOLID 原则审查、代码异味检测和量化评分。
> 兼容 OpenClaw / CodeBuddy / WorkBuddy / Codex / Claude Code 等主流 AI 编程助手，支持 macOS / Linux / Windows。

## 安装

```bash
openclaw skills install code-review
```

或通过 `.skill` 文件手动安装：

```bash
openclaw skills install ./code-review.skill
```

安装成功后，发送任意消息触发 Skill，会收到使用示例提示。

---

## 使用方式

### 基础用法

```
/点评 https://git.woa.com/org/repo
```

### 指定审查子目录

```
/点评 https://git.woa.com/org/repo /agent /agent_smart
/review https://git.woa.com/org/repo /src /lib
/代码点评 https://git.woa.com/org/repo /backend
```

### 通配符支持

```
/点评 https://git.woa.com/org/repo /agent**
```

`/agent**` 会自动匹配仓库中所有以 `agent` 开头的目录（如 `agent`、`agent_smart`、`agent_v2`）。

### 支持的触发指令

| 指令 | 说明 |
|------|------|
| `/点评` | 中文简写 |
| `/代码点评` | 中文全称 |
| `/review` | 英文简写 |
| `/code-review` | 英文全称 |
| `帮我review <url>` | 自然语言 |

---

## 输出示例

```
点评：

【优点】
项目将复杂流程合理拆解为多个独立 Agent，工厂函数模式统一，模块边界清晰。
DebugCollector 的引入对排查问题很有帮助，Pydantic model_validator 对非法输入的主动拦截体现了防御性编程意识。

【不足】
有几个问题需要立即处理：agent.py 和 agent_idea.py 里有 exit() 调用，上服务会直接把进程干掉；
idea_genreate_agent 这个 key 拼错了，会在运行时 ValueError；LANGFUSE_SCERET_KEY 少写了一个字母，监控会静默不生效。

工程层面：同一组环境变量在 34 个文件里重复读取了 169 次，没有统一配置管理；
agent/utils/ 和 agent_smart/utils/ 有 13 个同名文件各维护一份，已经开始分叉；
agent.py 和 agent_chain.py 并行维护了两套相同调度逻辑但版本已不同步，建议尽快废弃其中一个。
```

---

## 评审维度

| 维度 | 权重 | 考察重点 |
|------|------|---------|
| 单一职责 SRP | 20% | 类/函数是否只做一件事 |
| 开闭原则 OCP | 15% | 扩展是否需要修改核心类 |
| 里氏替换 LSP | 10% | 子类是否可安全替换基类 |
| 接口隔离 ISP | 10% | 是否存在"胖接口" |
| 依赖倒置 DIP | 15% | 高层是否依赖具体实现 |
| 契约精神 | 15% | 函数签名是否诚实、类型注解覆盖率 |
| 代码异味 | 15% | 危险调用、重复代码、超长函数 |

---

## 评审上下文（Review Context Contract）

只啃 diff 的 review 会漏三类东西：CI 的实际失败原因、AI bot / 人工评论里已经指出的问题、
以及这条 PR 到底要解决哪条 issue。本 skill 在 PR/MR 模式下强制装配四类信号：

| 源 | 内容 |
|----|------|
| S1 CI / check-runs | 每个检查的 conclusion、是否 required、失败 job 的**日志尾部** |
| S2 Bot / AI 评论 | `coderabbitai` / Copilot / Codex / Greptile / Codecov… 分类与 resolved 状态 |
| S3 人工评论与前序轮次 | unresolved thread、评论锚定的 head SHA、上一轮 review 结论 |
| S4 关联 issue 上下文 | `closes/fixes` 引用的 issue、Monica canonical issue、同文件碰撞的其它 open PR |

AI reviewer 的评论是**输入**，不是结论也不是噪音：逐条对照当前 head 的代码核实后，
落到 `confirmed` / `refuted` / `stale` / `already_addressed` / `out_of_scope` 五态之一，
`confirmed` 的必须标注来源。规范全文见 `references/review-context.md`。

```bash
python scripts/collect_pr_context.py https://github.com/owner/repo/pull/913
python scripts/collect_pr_context.py https://github.com/owner/repo/pull/913 -o ctx.json --max-log-lines 40
```

摘要输出到 stderr，JSON 输出到 stdout 或 `-o` 指定文件。GitHub 侧优先用 `gh` CLI
（能拿到 review thread 的 resolved 状态和失败日志），工蜂侧走 GitLab API v4。

## 文件结构

```
code-review/
├── SKILL.md                       # Skill 主文件，含完整执行流程
├── README.md                      # 本文件
├── scripts/
│   ├── analyze.py                 # 静态分析脚本（可独立运行）
│   ├── fetch_pr_diff.py           # PR/MR diff 与元数据获取
│   └── collect_pr_context.py      # 评审上下文采集（CI / bot 评论 / 关联 issue）
└── references/
    ├── scoring.md                 # 各维度评分细则
    └── review-context.md          # Review Context Contract（四类信号源与裁决规则）
```

### 独立运行分析脚本

无需 AI 助手，也可直接在命令行运行：

```bash
# macOS / Linux
python3 scripts/analyze.py /path/to/repo
python3 scripts/analyze.py /path/to/repo --focus agent agent_smart
python3 scripts/analyze.py /path/to/repo --focus "/agent**"

# Windows
python scripts\analyze.py C:\path\to\repo
python scripts\analyze.py C:\path\to\repo --focus agent agent_smart
```

脚本无第三方依赖，只需 Python 3.10+。

### 独立运行上下文采集

```bash
python3 scripts/collect_pr_context.py https://github.com/owner/repo/pull/913 -o ctx.json
```

仅标准库；`gh` CLI 可选但建议安装（缺它拿不到 unresolved 状态与失败日志）。

---

## 平台兼容性

| 平台 | 支持情况 |
|------|---------|
| OpenClaw (内网) | ✅ 全功能，自动获取工蜂 token |
| CodeBuddy / WorkBuddy | ✅ 标准 git clone |
| Codex / Claude Code | ✅ 标准 git clone |
| macOS / Linux CLI | ✅ 脚本可独立运行 |
| Windows | ✅ 脚本可独立运行 |

> 内网工蜂仓库需要有读取权限。OpenClaw 环境会自动从 MCP 获取认证 token，其他环境需提前配置 git 凭据。

---

## 适用语言

当前主要针对 **Python** 项目优化，Go / TypeScript 项目可用但部分检测项会跳过。

---

## 贡献

欢迎提交 MR 改进评审规则或扩展语言支持，评分细则见 `references/scoring.md`。
