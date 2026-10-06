# Review Context Contract（评审上下文契约）

> 2026-09-22 hallong 决策：自动化触发的 review 不能只啃 diff。
> CI、AI bot / AI reviewer 评论、人工 review 评论、以及相关 issue 上下文必须**当作证据源读入并逐条裁决**，
> 而不是当成噪音跳过，也不是当成结论照抄。

本文件是 `code-review` skill 的强制子流程。PR/MR 模式、以及任何被 Monica 自动化路由过来的 review 都必须执行。
本地 `local_full` / `local_branch` 模式只需执行 S4（issue 上下文），其余不可用时写明残余风险。

---

## 为什么需要这道契约

单独看 diff 的 review 有三类系统性漏判：

1. **重复劳动** —— `coderabbitai` / Copilot / Codex 已经指出过的问题，我们的 review 又独立重新推导一遍，浪费一轮；或者反过来，因为"bot 已经说了"就完全不看，bot 的误报和正报混在一起没人裁。
2. **脱离 CI 事实** —— 只知道"CI 红"，不知道红在哪个 job、哪个 step、日志说什么、是不是 flaky、是不是 required。于是 review 给出与 CI 矛盾的结论。
3. **脱离需求上下文** —— 不知道这条 PR 要解决哪条 issue、那条 issue 的验收标准是什么、上一轮 NACK 了什么、是不是有别的 PR 正在动同一批文件。于是 review 只能评"代码写得规不规范"，评不出"解决了问题没有"。

---

## 四类信号源（S1–S4）

### S1 — CI / check-runs

必须读到**结论 + 证据**，不能只读状态色：

| 字段 | 为什么 |
|------|--------|
| 检查名 + 是否 required | optional 失败不阻塞合并，required 失败阻塞 |
| conclusion（`success` / `failure` / `cancelled` / `timed_out` / `action_required` / `skipped` / `neutral`） | `skipped` 与 `neutral` 不是成功 |
| 失败 job 的**日志尾部** | 报错行、断言输出、缺依赖、平台差异都在这里 |
| 同一 head 上同一检查的历史结论 | 用于识别 flaky |

规则：

- **pending / queued / in_progress = 等待态**，不是通过也不是失败。此时 verdict 只能是 `HOLD` / `NACK` / `PRE-REVIEW ONLY`。
- **terminal failure = 阻塞态**。必须在 review 里作为 finding 输出，附 job 名 + 日志关键行。
- **flaky 判定**：同一 head、无代码变化的前提下，同一检查先失败后成功（或重跑即过）→ 标 `flaky`，写入 finding 但**不标 blocking**，同时要求修 flaky 或加 retry，别把它说成代码缺陷。
- **required 检查缺失**（项目配了 required 但 check-run 里没有）→ 视为等待态，不能当绿灯。项目没有 required 配置时，写明本次实际采用的检查集合。

### S2 — Bot 与 AI reviewer 评论

**先分类，再裁决。分类错了后面全错。**

| 类别 | 识别方式 | 处置 |
|------|---------|------|
| `ai_reviewer` | `coderabbitai[bot]`、`github-advanced-security`、`copilot`、`codex`、`greptile`、`cursor`、`sourcery-ai`、`deepsource`、`codiumai`、`bugbot` 等 | **逐条裁决**（见下） |
| `ci_bot` | `github-actions[bot]`、`codecov[bot]`、`coveralls`、`dependabot[bot]`、`renovate[bot]`、`sonarcloud[bot]` | 当**信号**读，不单列 finding；Codecov 的 `Added line #L106 was not covered by tests` 这类行级提醒要落到 S1/CI 缺口 |
| `human` | `user.type != "Bot"` | 逐条裁决，且 unresolved 即阻塞 |
| `self_echo` | 我们自己 agent 镜像过去的 Monica → GitHub 评论 | **跳过**，不作为输入，避免自证循环 |
| `container` | 无 `file:line` 锚点 + 计数/导航型正文 | 计入 `out_of_scope`，**并计入 S2 总条数** |

分类器由 `collect_pr_context.py:classify_author()` 实现，判定顺序
`self_echo > ai_reviewer > ci_bot > 兜底`，`container` 在最前面且独立于作者身份。
安全/依赖扫描类 bot（`github-advanced-security`、`snyk`、`semgrep`、`trivy`、`osv-scanner`、
`stepsecurity`、`ellipsis`、`graphite`、`codspeed`）属 `ai_reviewer` —— 它们产出的是可裁决的
finding，降格成 `ci_bot` 会让 CodeQL / secret scanning 的发现永远不被逐条裁决。
`self_echo` 必须在 `ai_reviewer` 之前：后者含 `claude` / `anthropic`，我们自己同系的 agent
对外发过评论的话会被误判成外部 AI reviewer 拿去裁决，那正是自证循环。

**容器评论（container comments）一律计入 `out_of_scope`**（2026-09-22 试点后补钉）

同一批 bot 评论里混着两类**不含独立 finding**的评论：

- **汇总型** —— `Actionable comments posted: N`、`X issues found`、`This PR was reviewed` 之类只报计数的；
- **入口型** —— change-stack / walkthrough / "View reviewed changes" 这类只给导航入口的。

识别点：评论体只有计数或导航链接，**没有锚定到具体 `file:line` 的 finding**。
`collect_pr_context.py` 的 `is_container_comment()` 已实现该判定（2026-10-06）：有 `file:line` 锚点
或正文自带 `file:line` 提示即不是容器；剥掉 HTML 注释/标签后正文为空且带自动生成标记
（如 coderabbit 的 `review_stack_entry_start`）也算容器。

规则：

1. 容器评论**计入 `out_of_scope`**，且**计入 S2 总条数**。这样 `总条数 = 五态之和` 恒成立，不同 PR 的裁决分布行可以横向比较。试点 1 与试点 2 曾因一个计入、一个写在分布行之外，导致两条分布不可比 —— 这是本条的起因。
2. 容器评论**不要求逐条裁决**，但输出里要有一句"已识别并排除 N 条容器评论"，否则读的人无法核对总数。

**裁决五态**（每一条 `ai_reviewer` / `human` finding 都必须落到其中一态，不允许"没看到"）：

| 态 | 含义 | 输出要求 |
|----|------|---------|
| `confirmed` | 对照**当前 head 的代码**核实，问题真实存在 | 升格为 review finding，带 `P0/P1/P2`，**必须标注来源**（`来源: coderabbitai @ L123`） |
| `refuted` | 对照代码核实，问题不成立 | 写明反证（文件路径 + 行号 + 为什么 bot 判断错），一行即可，不要长篇反驳 |
| `stale` | 评论锚定的代码在当前 head 已不存在，或评论早于当前 head | 标 `stale`，不进 findings；若该问题在新代码里换了个形式还在，按 `confirmed` 重新落 |
| `already_addressed` | 已在当前 diff 或后续提交中修掉 | 引用修掉它的那一行/那个 commit，不计入 findings |
| `out_of_scope` | 与本 PR 意图无关（例如 bot 对未改动的旧代码发议论） | 一句话说明，不进 findings |

三条硬规则：

1. **不照抄** —— bot 说有 bug 不等于有 bug。必须打开代码核实。AI reviewer 的误报率不低，照抄会让 review 变成 bot 的复读机。
2. **不无视** —— bot 说有 bug 也不等于没 bug。跳过不裁决是漏判，尤其是并发、资源释放、边界条件这类 bot 反而比人细的地方。
3. **风格类 bot 意见不构成阻塞** —— 命名、格式、可选的"建议重构"最多 `P3`，且不得作为 NACK 理由。

### S3 — 人工 review 评论与前序轮次

- 读全部 review 提交（`APPROVED` / `CHANGES_REQUESTED` / `COMMENTED`）及其 **提交时的 head SHA**。
- **unresolved thread = 阻塞**。存在 unresolved blocking thread 时不得给 ACK。
- 已 resolve 的 thread 不重复输出，但要在上下文摘要里记一笔"上轮 N 条已闭环"。
- **前序轮次去重**：若本 PR 上一轮已被我们 NACK 过，本轮只输出"仍未修复"的部分，并引用上一轮的 finding 编号或标题；不要把同一批问题换个说法重写一遍，也不要因为上一轮说过这轮就不核实了。

### S4 — 关联 issue 上下文

**这一项是 diff-only review 最大的盲区。**

1. **Monica canonical issue**（必做）
   - 定位方式：`metadata.pr_url`、issue 标题/描述中的 PR URL、`[repo#PR]`、`#<number>`。
   - 必读：`review_status` / `review_head_sha` / `approved_head_sha` / `blocked_reason` / `claim_owner`，以及评论线程里的**上一轮 NACK 结论**和**人工决策**。
   - 用途：判断这是第几轮、上轮卡在哪、有没有 hallong 的显式决策改变了验收标准。
2. **GitHub / 工蜂 关联 issue**（PR body 里 `closes` / `fixes` / `resolves` / `Closes PIP-XXXX`）
   - 读原始 bug 报告或需求描述，**提取验收标准**。
   - review 必须回答：这个 PR 真的满足了那条 issue 的验收标准吗？只评代码质量而漏掉"没解决问题"是最贵的漏判。
3. **同文件碰撞的其它 open PR**
   - 若另一个 open PR 正在改同一批文件，写明冲突风险；不构成阻塞，但要在 `Open questions` 里提出。

---

## 执行顺序

```
1. 取 PR 元数据 + head SHA（fetch_pr_diff.py）
2. 抓上下文（collect_pr_context.py）→ CI / 评论 / 关联 issue，一次拿全
3. 建立"意图 → 测试"映射（第六点五步）
4. 分类 S2 评论 → 五态裁决 → 逐条对照当前 head 代码核实
5. 读 S1 失败日志 → 区分 flaky / 真实缺陷 / 环境配置
6. 读 S4 → 提取验收标准 + 上一轮结论 → 复核是否已闭环
7. 合并 S1–S4 的 confirmed 项与 diff 独立发现 → 按严重度排序输出
8. 输出头部固定带 Review context 摘要块
```

---

## 输出契约

review 正文**开头**必须有这个块，让读的人一眼看到证据覆盖面：

```
Review context:
- head: <sha短码> | base: <branch> | files: N
- CI: N required / M total | 结论: <green|N failing|pending> | 失败 job: <名>（若有）
- Bot/AI 评论: X 条 → confirmed Y / refuted Z / stale W / already_addressed V / out_of_scope U
- 人工评论: N 条 | unresolved blocking: K
- 关联 issue: <Monica issue / GitHub #N> | 验收标准: <一句话> | 上轮结论: <NACK 于 <sha> / 首轮>
- 上下文缺口: <读不到的信号，如"工蜂 CI 日志需 token" / "无">
```

**恒等式自检**：`X = Y + Z + W + V + U`。不等即分布行写错，必须改到相等为止（容器评论计入 `U`）。

这两条等式现在由脚本机械校验，不再只靠自觉（2026-10-06，PIP-4283）：

- 分类恒等式 `total = ai_reviewer + ci_bot + human + self_echo + container`
- 裁决恒等式 `X = confirmed + refuted + stale + already_addressed + out_of_scope`，`X = ai_reviewer + human`

`collect_pr_context.py --verdicts <json> [--strict-identity]` 会校验并把结果写进
`comments.identity`；不等时 `--strict-identity` 以退出码 2 失败，**拒绝输出 Review context 块**。

**裁决台账**（同一条 bot 意见不重裁）：`--ledger <path>` 按
`(repo, bot_login, file, 归一化行范围, 文本指纹)` 落盘五态裁决 + 反证 + 当时 `head_sha` + 时间戳，
二次命中直接引用历史裁决。`refuted` 累计 ≥3 次的同类意见由 `ledger_refuted_hotspots` 汇总，
走一次 hallong 决策（是否加 `.coderabbit.yaml` 抑制规则或停用该 bot），不在每轮重复问。
台账只落本地 / Monica —— 不在 GitHub 回 bot、不 resolve thread、不点赞。

Findings 里凡是从外部信号来的，必须带 `来源:`：

```
- [P1] <标题> — <file>:<line>
  来源: coderabbitai L88 / CI job <name> / Monica issue 上轮 NACK
  <为什么是问题；触发条件；最小修复方向。>
```

**禁止**：
- 在 required checks 未完成/失败、或存在 unresolved blocking thread 时给出 ACK / `approved_head_sha`。
- 读了 CI 和评论却不在正文里体现——没体现就等于没读。
- 把"bot 没说话"当成"bot 认可"。bot 静默可能只是没跑。

**信号读不到时**（无 token、工蜂 API 受限、网络失败）：在 `上下文缺口` 里写明，并写入残余风险。**不得假设通过**。
