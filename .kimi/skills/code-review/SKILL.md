---
name: code-review
description: "用户请求代码评审时使用：装配 diff + CI 证据 + AI bot/人工评论 + 关联 issue 上下文，逐条裁决后按严重度给证据；保留 exact-head 审核门禁。"
---

# 代码点评 Skill

## 触发格式

```
# 模式一：本地项目全量 review（WorkBuddy / CodeBuddy / 本地 IDE 使用）
/code-review
/review
/点评

# 模式二：当前分支 diff review（只看本分支相对 master/main 的改动）
/code-review branch
/review branch
/点评 branch

# 模式三：远程仓库 review（指定 git URL）
/code-review <git_url> [子目录...]
/点评 https://git.woa.com/org/repo /agent /agent_smart
/review https://git.woa.com/org/repo /src**

# 模式四：PR / MR review（通过 PR 或 MR URL，用 git diff 分析）
/code-review https://git.woa.com/<ns>/<proj>/-/merge_requests/<id>
/review https://github.com/<owner>/<repo>/pull/<id>
/点评 <MR_URL_或_PR_URL>
```

**安装后首次响应**：输出以下示例用法：

```
✅ 代码点评 Skill 已就绪！

用法示例：
  /code-review              ← 当前项目全量 review
  /code-review branch       ← 只 review 当前分支的改动
  /点评 https://git.woa.com/org/repo
  /点评 https://git.woa.com/org/repo /agent /agent_smart
  /review https://git.woa.com/org/repo /src**

  # PR / MR 专项 review（用 git diff 分析改动）
  /review https://git.woa.com/lightbox/proj/-/merge_requests/14
  /review https://github.com/owner/repo/pull/913

支持语言：Python · Rust · Node.js · TypeScript · Go · 微服务混合项目
支持的触发指令：/点评  /代码点评  /review  /code-review
子目录支持通配符（如 /src** 匹配 src、src_common 等）
```

---

## 执行流程

### 第一步：解析指令并判断模式

**优先级判断（从上到下，第一个匹配即用）：**

| 用户输入 | 模式 | 说明 |
|---------|------|------|
| `/code-review`（无其他参数） | **本地全量** | 使用当前 IDE 项目根目录，扫全仓库 |
| `/code-review branch` | **本地 diff** | 使用当前 IDE 项目根目录，只看当前分支 diff |
| URL 包含 `/-/merge_requests/` | **MR review** | 工蜂 MR，通过 API + git diff 分析 |
| URL 包含 `/pull/` 且域名为 github.com | **PR review** | GitHub PR，通过 API + git diff 分析 |
| `/code-review <https://...>`（普通 git URL） | **远程仓库** | 克隆指定 URL，扫全仓库（或指定子目录） |

提取字段：
- `mode`：`local_full` / `local_branch` / `remote` / `pr` / `mr`
- `git_url`：仅 remote 模式时有值
- `pr_url`：PR/MR 模式时有值
- `focus_dirs`：URL 之后所有以 `/` 开头的路径参数（remote 模式）

通配符规则：`/src**` 匹配所有以 `src` 开头的目录

**PR/MR 模式识别正则（Python）：**
```python
import re

def is_pr_or_mr_url(url: str) -> bool:
    return bool(
        re.search(r'/-/merge_requests/\d+', url) or  # 工蜂 MR
        re.search(r'github\.com/.+/pull/\d+', url)   # GitHub PR
    )
```

---

### 本地模式：定位当前项目根目录

**仅在 `local_full` 或 `local_branch` 模式下执行此步骤。**

按以下优先级查找项目根目录：

1. 环境变量 `GF_IDE_DEFAULT_PROJECT_ROOT`（OpenClaw / WorkBuddy 注入）
2. 环境变量 `WORKSPACE_ROOT` / `PROJECT_ROOT`
3. 当前工作目录（`os.getcwd()`）向上查找第一个含 `.git/` 的目录
4. 若都找不到，告知用户「无法定位项目根目录，请用 `/code-review <git_url>` 指定仓库」

```python
import os
from pathlib import Path

def find_project_root() -> Path:
    for env_key in ("GF_IDE_DEFAULT_PROJECT_ROOT", "WORKSPACE_ROOT", "PROJECT_ROOT"):
        val = os.environ.get(env_key)
        if val and Path(val).exists():
            return Path(val)
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        if (parent / ".git").exists():
            return parent
    return None
```

---

### 本地 branch 模式：获取 diff 文件列表

**仅在 `local_branch` 模式下执行。**

```python
import subprocess
from pathlib import Path

def get_branch_diff_files(repo_dir: Path):
    """返回 (diff_files, base_branch, current_branch)"""
    for base in ("master", "main", "origin/master", "origin/main"):
        r = subprocess.run(["git", "rev-parse", "--verify", base],
                           capture_output=True, cwd=repo_dir)
        if r.returncode == 0:
            break
    else:
        base = "HEAD~1"

    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, cwd=repo_dir
    ).stdout.strip()

    diff_files = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...HEAD"],
        capture_output=True, text=True, cwd=repo_dir
    ).stdout.strip().splitlines()

    diff_content = subprocess.run(
        ["git", "diff", f"{base}...HEAD"],
        capture_output=True, text=True, cwd=repo_dir
    ).stdout

    return diff_files, base, branch, diff_content
```

> **branch 模式的分析范围**：`analyze.py` 仍扫全仓库提供整体背景，但深度阅读和点评**聚焦在 diff 涉及的文件**，评分以 diff 内容为主，整体数据作补充。

---

### 第二步：定位 workspace 目录（仅 remote 模式）

```python
import os
from pathlib import Path

workspace = (
    Path(os.environ["SKILL_WORKSPACE"])
    if "SKILL_WORKSPACE" in os.environ
    else Path.cwd() / "code_review_tmp"
)
workspace.mkdir(parents=True, exist_ok=True)
```

### 第二点五步：PR/MR 模式 — 获取 diff 和源码上下文

**仅在 `pr` / `mr` 模式下执行此步骤。**

先使用 `scripts/fetch_pr_diff.py` 获取 diff 和 PR 元数据：

```python
import subprocess, sys, json, os
from pathlib import Path

skill_dir = Path(__file__).parent  # SKILL.md 所在目录
fetch_script = skill_dir / "scripts" / "fetch_pr_diff.py"

# 调用方式：输出 JSON 到 stdout
result = subprocess.run(
    [sys.executable, str(fetch_script), pr_url],
    capture_output=True, text=True
)

if result.returncode != 0:
    # 脚本在 stderr 输出了错误信息，转告用户
    print(result.stderr)
    # fallback 到克隆模式
else:
    pr_info = json.loads(result.stdout)
    diff_text = pr_info["diff_text"]
    files_changed = pr_info["files_changed"]
    base_sha = pr_info["base_sha"]
    head_sha = pr_info["head_sha"]
    title = pr_info["title"]
    author = pr_info["author"]
```

然后必须尽量物化完整源码上下文，不能只看 diff：

1. 如果当前工作目录或 IDE 项目根是同一个 git 仓库源码目录，用 `git worktree` 创建临时分析目录：
   - 先确认 remote 和 PR/MR 仓库匹配；
   - fetch PR/MR head 到本地 ref；
   - `git worktree add --detach <temp_dir> <head_sha>`；
   - 在 `<temp_dir>` 中阅读相关源码、测试、配置和 CI；
   - 分析完成后执行 `git worktree remove --force <temp_dir>`，必要时 `git worktree prune`。
2. 如果当前目录是空目录、非源码目录、或不是目标仓库，允许在临时 workspace 中 clone/fetch 目标仓库后 checkout `head_sha`，再结合源码上下文分析。
3. 如果网络或权限导致无法创建 worktree/clone，才 fallback 到 diff-only；输出里必须明确说明“源码上下文不足”。

临时目录建议放在系统 temp 或 `SKILL_WORKSPACE/code_review_worktrees/` 下，目录名包含仓库名和 PR/MR 编号。不要在用户当前工作树里 checkout PR head，不要污染用户已有分支。

**认证处理：**

| 平台 | Token 来源（优先级） |
|------|-------------------|
| 工蜂 (git.woa.com) | 环境变量 `GF_PRIVATE_TOKEN` / `GF_TOKEN` / `GITLAB_TOKEN`，或 `git config gongfeng.privateToken` |
| GitHub | 环境变量 `GITHUB_TOKEN` / `GH_TOKEN`，或 `git config github.token` |

> 若 Token 不存在，脚本会尝试匿名访问（工蜂公开项目可用，私有项目会失败）。遇到认证失败时，提示用户设置环境变量后重试。

**Token 设置方式（告知用户）：**
```bash
# 工蜂
export GF_PRIVATE_TOKEN=你的Token  # 临时
git config --global gongfeng.privateToken 你的Token  # 持久

# GitHub
export GITHUB_TOKEN=你的Token
git config --global github.token 你的Token
```

获取源码上下文后，继续进入第四步和第六步；PR/MR 模式仍以 diff 为主，但必须用完整源码验证类型、契约、测试和调用链。

### 第二点六步：评审上下文装配（Review Context Contract）

**PR / MR 模式必做。本地模式只做 S4（issue 上下文）。**

diff 只告诉你"改了什么"，不告诉你"这条改动在真实世界里处境如何"。本步把四类外部证据一次抓全，
后续所有 finding 都要能在这些证据里找到落点。完整规范见 `references/review-context.md`。

```python
import subprocess, sys, json
from pathlib import Path

collect_script = skill_dir / "scripts" / "collect_pr_context.py"
result = subprocess.run(
    [sys.executable, str(collect_script), pr_url,
     "--output", str(workspace / "pr_context.json"),
     "--max-log-lines", "40"],
    capture_output=True, text=True
)
# 摘要输出到 stderr；JSON 写入 --output
ctx = json.loads(Path(workspace / "pr_context.json").read_text(encoding="utf-8"))
```

四类信号源：

| 源 | 内容 | 落地方式 |
|----|------|---------|
| **S1 CI / check-runs** | 每个检查的 conclusion、是否 required、失败 job 的**日志尾部** | 失败即 blocking finding，附 job 名 + 报错行 |
| **S2 Bot / AI 评论** | `coderabbitai` / `copilot` / `codex` / `greptile`… 与 `codecov` / `dependabot` 等 | **逐条裁决**（见下），不照抄也不无视 |
| **S3 人工评论与前序轮次** | unresolved thread、评论锚定的 head SHA、上一轮 review 结论 | unresolved 即阻塞；前序 NACK 去重后复核 |
| **S4 关联 issue 上下文** | `closes/fixes` 引用的 issue、Monica canonical issue、同文件碰撞的其它 open PR | 提取验收标准，回答"解决了问题没有" |

**S2 分类 → 裁决五态**（每一条都必须落到一态，不允许"没看到"）：

- `ai_reviewer`（coderabbitai / copilot / codex / greptile / cursor…）→ **逐条裁决**
- `ci_bot`（codecov / dependabot / github-actions…）→ 当**信号**读，落到 S1，不单列 finding
- `human` → 逐条裁决，unresolved 即阻塞
- `self_echo`（我们自己 agent 镜像过去的评论）→ **跳过**，避免自证循环
- `container`（汇总型/入口型：只报计数或给导航入口，无 `file:line` 锚点）→ 固定落 `out_of_scope`，
  但**计入 S2 总条数**。不识别会让总条数随 PR 漂移，两条裁决分布无法横向比较

裁决结果：`confirmed`（对照**当前 head 的代码**核实为真，升格为 finding 并标 `来源:`）/ `refuted`（写明反证）/
`stale`（锚定代码已不在当前 head）/ `already_addressed`（已在本 PR 修掉，引用那一行）/ `out_of_scope`（与本 PR 意图无关）。

三条硬规则：

1. **不照抄** —— bot 说有 bug 不等于有 bug，必须打开代码核实。照抄会让 review 变成 bot 的复读机。
2. **不无视** —— bot 说有 bug 也不等于没 bug。尤其是并发、资源释放、边界条件，bot 往往比人细。
3. **风格类意见不构成阻塞** —— 命名、格式、可选重构最多 `P3`，不得作为 NACK 理由。

**脚本不可用时**（缺 `gh`、无 token、工蜂 API 受限）不得跳过本步：改用 `gh pr view/checks`、
`gh api` 手工取，并在输出末尾写明**上下文缺口**。**不得假设 CI 通过、不得假设"bot 没说话 = 认可"**。

**恒等式机械校验**（2026-10-06，PIP-4283）：裁决分布不再靠自觉算对，脚本会校验两条等式，
不等即拒绝输出 Review context 块：

1. 分类恒等式 `total = ai_reviewer + ci_bot + human + self_echo + container`（容器评论计入）
2. 裁决恒等式 `X = confirmed + refuted + stale + already_addressed + out_of_scope`，
   其中 `X = ai_reviewer + human`

```python
# 把本轮裁决分布交给脚本校验；不等则 --strict-identity 以退出码 2 失败
subprocess.run([sys.executable, str(collect_script), pr_url,
                "--output", str(workspace / "pr_context.json"),
                "--verdicts", json.dumps({"confirmed": 2, "refuted": 1,
                                          "stale": 0, "already_addressed": 0,
                                          "out_of_scope": 1}),
                "--strict-identity"])
```

**裁决台账**（不重裁同一条 bot 意见）：加 `--ledger <path>`，脚本按
`(repo, bot_login, file, 归一化行范围, 文本指纹)` 查历史裁决，命中则回填 `prior_verdict` 直接引用。
`ledger_refuted_hotspots` 里累计 `refuted >= 3` 次的同类 bot 意见，汇总后走一次 hallong 决策
（加抑制规则或停用该 bot），不在每轮重复问。裁决结论一律落 Monica —— 不在 GitHub 回 bot、
不 resolve thread、不点赞。

### 第三步：克隆仓库（仅 remote 模式）

若平台支持工蜂认证，先获取 `private_token`（`mcporter call gongfeng.get_current_user`）：

```python
import subprocess
from pathlib import Path

repo_name = git_url.rstrip("/").split("/")[-1].removesuffix(".git")
repo_dir = workspace / f"code_review_{repo_name}"

if not repo_dir.exists():
    clone_url = git_url if git_url.endswith(".git") else git_url + ".git"
    cmd = ["git", "-c", f"http.extraHeader=PRIVATE-TOKEN: {private_token}",
           "clone", clone_url, str(repo_dir)] if private_token else ["git", "clone", clone_url, str(repo_dir)]
    subprocess.run(cmd, check=True)
```

> 若克隆失败（无权限 / 网络问题），报告原因并停止。

**本地模式跳过此步骤**，直接用上面找到的 `project_root` 作为分析目录。

### 第四步：语言 & 项目类型检测

**自动判断，无需用户指定：**

| 判断文件 | 识别结果 |
|---------|---------|
| `Cargo.toml` | Rust |
| `package.json` + `.ts` 文件 | Node.js/TypeScript |
| `package.json`（无 `.ts`） | Node.js/JavaScript |
| `go.mod` | Go |
| `*.py` 为主 | Python |
| 多个 `Cargo.toml` / `package.json` / `*.py` 混合 | 微服务（多语言） |

微服务识别额外标志：
- 根目录有 `docker-compose.yml` / `k8s/` / `helm/`
- 多个子目录各自有独立的 `Cargo.toml` / `package.json` / `pyproject.toml`
- 根目录有 `proto/` 或 `*.proto` 文件

```python
def detect_project_type(repo_dir: Path) -> dict:
    # 返回 {"lang": "rust|node|python|go|multi", "is_microservice": bool,
    #        "has_proto": bool, "services": [list of service dirs]}
```

### 第五步：运行静态分析脚本

```python
import subprocess, sys
from pathlib import Path

skill_dir = ...  # SKILL.md 所在目录
analyze_script = skill_dir / "scripts" / "analyze.py"

cmd = [sys.executable, str(analyze_script), str(repo_dir), "--lang", detected_lang]
if focus_dirs:
    cmd += ["--focus"] + focus_dirs

result = subprocess.run(cmd, capture_output=True, text=True)
print(result.stdout)
```

若找不到 `analyze.py`，跳过脚本，直接用 Agent 分析能力完成第六步。

**`local_branch` 模式额外**：脚本扫全仓库提供数据背景，但深度阅读时只加载 diff 涉及的文件。

**脚本输出的量化指标（按语言）：**

| 指标 | Python | Rust | Node/TS | Go |
|------|--------|------|---------|-----|
| 超长函数 >50 行 | ✅ | ✅ | ✅ | ✅ |
| 危险调用 | ✅ eval/exec | ✅ unsafe/unwrap() | ✅ eval/exec | ✅ unsafe |
| 注释掉的代码 | ✅ | ✅ | ✅ | ✅ |
| 拼写错误 | ✅ | ✅ | ✅ | ✅ |
| 类型注解/签名覆盖率 | ✅ | N/A(强类型) | ✅ TS any 覆盖率 | N/A |
| Docstring/JSDoc 覆盖率 | ✅ | ✅ | ✅ | ✅ |
| 重复文件对 | ✅ | ✅ | ✅ | ✅ |
| panic!/unwrap() 裸调用 | - | ✅ | - | - |
| `any` 类型滥用 | - | - | ✅ | - |
| 硬编码配置/端口 | ✅ | ✅ | ✅ | ✅ |
| proto 文件一致性 | - | 微服务 | 微服务 | 微服务 |

### 第六步：深度阅读核心文件

**模式分层读取策略：**

| 模式 | 阅读范围 |
|------|----------|
| `local_full` / `remote` | 全仓库：入口文件 + 最大业务文件 + 基础设施层（每类最多 3 个） |
| `local_branch` | 重点阅读 **diff 涉及的文件**，仔细阅读新增 / 修改的函数和类 |
| `pr` / `mr` | 以 `diff_text` 为主线，在临时 worktree/clone 的完整源码中阅读变更文件、相邻实现、相关测试和 CI 配置 |

> `local_branch` / `pr` / `mr` 模式下，如果 diff 文件数超过 15 个，只阅读变动最大的 5 个文件。

### 第六点五步：代码意图与测试覆盖核查

每次 review 都必须检查测试是否根据实际代码意图而写，而不是只看“有没有测试文件”或覆盖率数字。

执行顺序：

1. 从 diff 和相邻源码推导本次改动的真实意图：行为变化、外部契约、错误路径、状态迁移、数据格式、并发/权限/平台差异、向后兼容边界。
2. 找出新增或修改的测试：单元测试、集成测试、E2E/HTTP/CLI/UI/真实运行时测试、CI validation 命令。
3. 建立“意图 → 测试”映射：每个关键行为至少应有一个正确层级的测试；公共 API、跨进程、网络、数据库、文件系统、DCC/浏览器/UI、网关/REST/MCP 这类变更通常需要集成或 E2E 路径。
4. 检查测试是否真的断言了契约：fixture 是否符合真实生产数据，是否只测 happy path，是否把字段语义造错，是否只覆盖实现细节，是否 mock 掉了最容易出错的边界。
5. 把测试缺口当成 review finding：说明缺的是单测、集成测试还是 E2E；给出应覆盖的真实场景和建议落点。

判断标准：
- 单测适合纯函数、解析、校验、错误分支、边界条件和低层状态机。
- 集成/E2E 适合跨模块契约、公开 API、CLI、HTTP/MCP/REST、数据库/文件系统、并发锁、真实宿主运行时、权限和平台差异。
- 只有覆盖率提升不等于测试正确；测试必须使用与代码契约一致的真实样例。
- 如果改动不需要 E2E，也要说明原因；如果 E2E 因环境缺失无法验证，输出残余风险。

**PR/MR 模式下的 diff 解析：**
```python
import re

def parse_diff_files(diff_text: str) -> list[dict]:
    """从 unified diff 中提取每个文件的变更内容"""
    files = []
    current = None
    for line in diff_text.splitlines():
        if line.startswith('diff --git '):
            if current:
                files.append(current)
            m = re.search(r'b/(.+)$', line)
            current = {"path": m.group(1) if m else "", "hunks": [], "additions": 0, "deletions": 0}
        elif current:
            if line.startswith('+') and not line.startswith('+++'):
                current["additions"] += 1
                current["hunks"].append(line)
            elif line.startswith('-') and not line.startswith('---'):
                current["deletions"] += 1
                current["hunks"].append(line)
            else:
                current["hunks"].append(line)
    if current:
        files.append(current)
    # 按变更行数排序，最多 15 个
    return sorted(files, key=lambda x: x["additions"] + x["deletions"], reverse=True)[:15]
```

**重点阅读（每类最多 3 个），按语言调整入口：**

| 语言 | 入口文件 | 核心业务层 | 基础设施层 |
|------|---------|-----------|----------|
| Python | `main.py` `app.py` `server.py` | 最大的非 test 业务文件 | `config/` `utils/` `base.py` |
| Rust | `main.rs` `lib.rs` | `src/domain/` `src/service/` | `src/infra/` `src/config.rs` |
| Node/TS | `index.ts` `app.ts` `server.ts` | `src/services/` `src/handlers/` | `src/config/` `src/db/` |
| Go | `main.go` | `internal/service/` `internal/domain/` | `internal/infra/` `pkg/` |
| 微服务 | 每个服务的入口文件 | 各服务的领域层 | API Gateway / proto 定义 |

**架构层次核查（Clean Architecture 视角）：**
- 是否有清晰的 Domain / Application / Infrastructure 分层
- 依赖方向是否由外向内（Infrastructure → Application → Domain），不反向
- Domain 层是否零基础设施依赖（不直接 import DB 驱动、HTTP 框架）
- 是否有明确的 Port/Adapter 或 Repository 抽象

**微服务专项核查：**
- 服务间通信是否通过 proto/OpenAPI 定义（而非直接共享代码）
- 是否有循环依赖服务（A 调 B，B 又调 A）
- 各服务的错误响应格式是否统一
- 配置是否集中管理（环境变量 / ConfigMap），还是散落在各服务代码里
- 是否有统一的日志格式和追踪 ID 规范（traceId/requestId）


### 第六点六步：CI / Review Signal Gate

PR/MR review 在输出结论前，必须消费**第二点六步**采集到的上下文，不能只看 diff。
判定细则见 `references/review-context.md`，此处只列不可让渡的硬规则：

1. **CI 要读日志，不只读颜色**。terminal failure 必须给出失败 job 名 + 日志关键报错行（`##[error]` / `FAILED` / `AssertionError` / `error[E…]`）；
   只写"CI 红了"不算证据。
2. **区分 flaky 与真实缺陷**。同一 head、无代码变化的前提下重跑即过 → 标 `flaky`，写入 finding 但**不标 blocking**，
   同时要求修 flaky 或加 retry；不要把它说成代码缺陷，也不要因为"重跑能过"就当没发生。
3. **required 与 optional 分开**。optional 失败不阻塞；项目没有 required 配置时，写明本次实际采用的检查集合。
4. 检查已有 review comments、inline comments、bot comments 和 unresolved threads，尤其是类似
   `Added line #L106 was not covered by tests` 的行级覆盖率/质量提醒——这类信号落进【测试意图核查】。
5. 如果 CI 或评论指出新增行未覆盖、测试缺失、lint/security/类型错误，即使代码本身看起来合理，也要作为 review finding 输出。
6. finding 必须写清楚**来源**（`来源: coderabbitai L88` / `CI job <name>` / `Monica issue 上轮 NACK`）、
   文件/行号、失败信号和最小修复建议。
7. required checks pending 是等待态；terminal failure 是阻塞态。不要在 required checks 未完成、失败、
   或已有 unresolved blocking comment / unresolved AI reviewer finding 时给出“可以合并/通过”的结论。
8. 如果无法读取 CI 或评论，必须在输出末尾写明**上下文缺口**与残余风险，而不是假设 CI 通过。

#### AI bot 反馈的处理（2026-09-22 hallong）

AI reviewer（`coderabbitai` / Copilot / Codex / Greptile 等）的评论是**输入**，不是结论，也不是噪音：

- 每条都必须对照**当前 head 的代码**核实后落到裁决五态（`confirmed` / `refuted` / `stale` / `already_addressed` / `out_of_scope`）。
- `stale` 判定：评论锚定的 `original_commit_id` 与当前 head 不一致，或锚定的行在当前 diff 中已不存在 —— 不要拿旧代码上的意见评新代码。
- 已 `resolved` 的 thread 不重复输出，但在上下文摘要里记一笔"上轮 N 条已闭环"。
- 本 PR 上一轮已被 NACK 过：只输出仍未修复的部分并引用上一轮 finding，不要把同一批问题换个说法重写一遍。
- 风格类 bot 意见最多 `P3`，不得作为 NACK 理由。

#### CI pending/failing verdict rule

CI pending/failing is not an approval state. When required checks are pending, queued, running, missing, skipped unexpectedly, cancelled, or failed:
- Verdict must be `HOLD`, `NACK`, or `PRE-REVIEW ONLY`; never `ACK`, `conditional ACK`, `approved_pending_ci`, or “CI green后可合并”.
- Do not set `review_status=approved`, `approved_pending_ci`, or `approved_head_sha` in Monica metadata.
- Do not trigger merge gate or reviewer handoff as if review passed.
- If the review still finds code issues, route exactly one current owner to fix them. If only CI is pending, keep the issue in progress and let the CI-green poller re-check the exact head.
- Only after live required checks are green, PR head is unchanged, unresolved blockers are absent, and findings are empty may the reviewer write `ACK` and pin `approved_head_sha`.

### 第七步：按维度评分

参考 `references/scoring.md` 中的评分细则，7 个维度加权得综合分：

| 维度 | 权重 |
|------|------|
| 单一职责 SRP | 20% |
| 开闭原则 OCP | 15% |
| 里氏替换 LSP | 10% |
| 接口隔离 ISP | 10% |
| 依赖倒置 DIP | 15% |
| 契约精神 | 15% |
| 代码异味 | 15% |

微服务项目额外叠加 Clean Architecture 检查（计入 DIP 和 OCP 维度）。

### 第七点五步：Codex Review Contract

输出采用 Codex 代码评审姿态：先问题，后背景。不要把优点放在 findings 前面冲淡风险。

- Findings first：按严重程度排序，先列 bug、回归、安全、数据丢失、契约破坏、缺测试导致的真实风险。
- 每条 finding 必须带 `P0/P1/P2/P3`、文件路径、行号或最窄代码位置；没有位置就说明证据来源（CI check、review thread、运行日志）。
- 不把风格偏好、泛泛重构、抽象建议当 finding；除非它已经造成可验证风险。
- 看到 CI、Codecov、CodeQL、lint、review comment 的阻塞信号时，优先作为 finding，而不是放到建议里。
- 如果没有发现问题，明确写“未发现阻塞问题”，再列剩余测试/CI 风险。
- Summary 和 change context 只能放在 findings 之后，保持短。

### 第八步：输出点评

输出必须 findings-first。默认中文，除非用户要求英文或 PR/MR 公共评论需要英文。

**有问题时：**
```
Findings:
- [P0/P1/P2/P3] <一句话问题标题> — <file>:<line>
  <为什么这是 bug/风险/回归；触发条件；最小修复方向。>

Open questions / assumptions:
- <只有确实影响判断时输出；没有就省略本节。>

Test / CI gaps:
- <测试意图是否覆盖；required checks、Codecov/coverage、review threads 是否仍有风险。>

Summary:
<1-3 句，放最后。>
```

**没有发现阻塞问题时：**
```
未发现阻塞问题。

Test / CI gaps:
- <仍未运行/无法读取/coverage warning 等残余风险；没有就写“未发现额外测试缺口”。>

Summary:
<1-3 句。>
```

PR/MR 模式在 Findings 前加上下文头，**必须包含 Review context 块**——没体现就等于没读外部证据：
```
MR/PR: <title> | <author> | <source> -> <target> | <files> files | <head_sha短码>

Review context:
- CI: <N required / M total> | 结论: <green|N failing|pending> | 失败 job: <名>（若有）
- Bot/AI 评论: X 条 → confirmed Y / refuted Z / stale W / already_addressed V / out_of_scope U
- 人工评论: N 条 | unresolved blocking: K
- 关联 issue: <Monica issue / GitHub #N> | 验收标准: <一句话> | 上轮结论: <NACK 于 <sha> / 首轮>
- 上下文缺口: <读不到的信号 / 无>
```

输出约束：
- 不输出评分表格；评分只用于内部判断严重度。
- 不先写【优点】；优点只在 Summary 中一句带过。
- 不给“可以合并/通过”结论，除非 required checks 已通过、无 unresolved blocking comments、findings 为空。
- 行号优先来自 diff 新增/修改行；无法定位时用最窄函数/检查名。

---
## 各语言专项评审要点

### Rust

- **`unwrap()` / `expect()` 裸用**：生产路径（非 test）每处 -5，超过 10 处 -20。应改用 `?` 或显式错误处理
- **`unsafe` 块**：每个未注释说明的 unsafe 块 -10
- **`clone()` 滥用**：大结构体在热路径上 clone，性能隐患
- **错误类型统一**：是否有统一的 `Error` enum（`thiserror`）；混用 `Box<dyn Error>` 和具体类型 -10
- **Panic in library**：library crate（`lib.rs`）中出现 `panic!` -15
- **依赖注入**：trait object（`dyn Trait`）是否用于解耦；直接 `new ConcreteType` 到处散落 -10

### Node.js / TypeScript

- **`any` 类型**：TypeScript 中每处显式 `any` -3，总计超 10 处 -20
- **`// @ts-ignore` / `// @ts-nocheck`**：每处 -5
- **未处理的 Promise rejection**：`.catch()` 缺失或 `async` 函数无 `try/catch` -10
- **`require()` 混用 `import`**：同一项目混用 -5
- **环境变量直接散落**：`process.env.XXX` 散布超 10 处（应统一到 config 模块）-15
- **接口 vs 类型别名**：可扩展的数据结构应用 `interface`，用 `type` 替代可扩展 interface -5
- **错误处理统一**：Express/Fastify 是否有统一的 error middleware；各路由各自 try/catch -10

### 微服务专项

- **Proto 文件是共享定义还是各服务自己维护一份**：各自维护 -15
- **服务间直接共享 model 代码（非 proto 生成）**：-20
- **跨服务循环调用**：A→B→A -30
- **硬编码服务地址**：`http://user-service:8080` 写死在代码里 -10
- **无统一错误码规范**：各服务 HTTP 错误结构不同 -10
- **日志无 traceId**：微服务下无法串联请求链路 -10

---

## 注意事项

- 本地模式优先读取 `GF_IDE_DEFAULT_PROJECT_ROOT` 环境变量定位项目根目录
- PR/MR review 优先使用当前源码仓库的临时 `git worktree` 分析，结束后清理；当前目录为空或不是目标仓库时再 clone/fetch 到临时 workspace
- 若仓库已存在于 workspace，先确认 remote/head/base 正确，再复用
- 无 focus 参数时扫描整个仓库
- 语言自动识别，无需用户指定；混合项目按多语言分别处理
- 对强类型语言（Rust/Go），类型注解覆盖率检测项跳过（语言强制保证）
- 无工蜂 MCP 的环境直接用标准 `git clone`
- 每次 review 都必须输出测试意图核查，不能只用“有测试/覆盖率通过”替代分析
- 每次 PR/MR review 都必须执行第二点六步装配上下文，并在输出头部给出 Review context 块
- AI bot 评论既不照抄也不无视：逐条核实后落到裁决五态，来源必须标注
- 点评风格：同行 review，不是正式报告
## Monica delivery surface contract

This skill can inspect or operate external systems, but Monica remains the required user-facing delivery surface. Do not end a task by only posting a GitHub/Gongfeng PR/MR/issue comment, commit message, terminal output, or run log. If an external comment is required, keep it public-safe and treat it as supporting context; then summarize the final result back to the Monica issue/comment thread.

When routing active work from Monica, use exactly one live `[@Name](mention://agent/<uuid>)` or `[@Squad](mention://squad/<uuid>)` mention. Future owners stay as plain text until their turn.



