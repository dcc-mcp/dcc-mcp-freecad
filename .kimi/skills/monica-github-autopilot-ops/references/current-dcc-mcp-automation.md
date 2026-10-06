# Current dcc-mcp Monica Automation Snapshot

Snapshot date: 2026-06-06 Asia/Shanghai.

This file is a convenience reference, not source of truth. Always verify with `monica` and `gh` before editing.

## Monica workspace

- Server: `https://api.monica.ai`
- Workspace ID: `8f26209c-7057-4997-acbb-131520933094`
- Top-level squad: `dcc-mcp 开发小组`
- Top-level squad ID: `8338fbed-3883-45a9-9ed1-5b3cf1038393`

Specialized squads:

- Backend/Core: `dcc-mcp 后端/Core 小队` / `227b3059-10fa-4796-bdb4-358a86cee975`, leader `爱写代码的小龙`.
- Frontend/UI: `dcc-mcp 前端/UI 小队` / `898f53c7-fd21-4275-826a-2c41a9d48e59`, leader `传林`.
- CI/DevOps/Release: `dcc-mcp CI/DevOps/Release 小队` / `4d2b46d0-a048-4a19-81e5-21d31b8fbd87`, leader `代码测试`.
- Docs/Knowledge: `dcc-mcp Docs/Knowledge 小队` / `e43d4273-902f-4a00-9e73-f887834525a5`, leader `小白`.

## Autopilots

Issue intake:

- ID: `04268462-fe99-4585-b15f-5475a0ca519f`
- Title: `GitHub Issue 同步到 dcc-mcp 总队 intake`
- Mode: `run_only`
- Status: `active`
- Assignee type: `squad`
- Trigger label: `GitHub issues opened`
- Filter: `issues / opened`
- GitHub hook shape: one org hook on `dcc-mcp`, event `issues`, content type `json`

PR CI failure handling:

- ID: `21f7e577-0e90-46b8-b83d-1a9a56e37b63`
- Title: `GitHub PR CI 失败 -> CI/DevOps/Release 小队`
- Mode: `run_only`
- Status: `active`
- Assignee type: `squad`
- Assignee: `dcc-mcp CI/DevOps/Release 小队` (`4d2b46d0-a048-4a19-81e5-21d31b8fbd87`)
- Active webhook triggers: one per active dcc-mcp repo
- Disabled legacy trigger: `GitHub workflow_run failed (legacy disabled)`
- Filter: `workflow_run / failure,timed_out,cancelled,action_required`
- GitHub hook shape: repo-level hook per active repo, event `workflow_run`, content type `json`
- Completion gate: after a developer fixes and pushes or force-pushes, they still own the PR until the current head SHA has required checks green and public metadata clean. While checks are pending, do not post `已完成，请复核` with a live loonghao mention. Verify checks with `gh pr checks --required`; verify PR title/body plus commit messages with `gh pr view` and `gh api repos/<owner>/<repo>/pulls/<pr>/commits`.

PR CI success review handoff:

- ID: `569c5123-ae4a-4966-8f15-08c4e68a0d87`
- Title: `GitHub PR CI 全绿 -> loonghao code review`
- Mode: `run_only`
- Status: `active`
- Assignee type: `agent`
- Assignee: `loonghao` (`4ef9454a-16f4-45dd-b6a3-46b3c3619b9c`)
- Active webhook triggers: one per active dcc-mcp repo
- Filter: `workflow_run / success`
- GitHub hook shape: repo-level hook per active repo, event `workflow_run`, content type `json`
- Purpose: when a PR-associated CI workflow completes successfully, verify all required PR checks are green and hand the PR directly to loonghao for `code-review` and merge decision. The autopilot does not merge PRs by itself.
- Dedupe rule: because `workflow_run success` can fire once per successful workflow, the agent must search active Monica issues for the same GitHub PR URL or `[repo#PR]`. If an issue already exists but the current head SHA has not yet been handed to loonghao after required checks turned green, add a new single-mention loonghao comment to the existing issue and cancel/no-op the duplicate. If the same head SHA was already handed off, only update the existing issue without another live mention.
- Public metadata gate: before a loonghao review handoff, inspect PR body/title and PR commit messages for internal keys such as `PIP-*`, Monica IDs/URLs, routing history, local paths, or private provenance. Route back to the implementer if any are present.
- S2 review-context gate (2026-10-06, PIP-4283): before any review handoff, run `code-review/scripts/collect_pr_context.py <pr_url> --output pr_context.json --strict-identity` and attach that JSON to the canonical Monica issue as evidence **only when the exit code is 0** (exit 2 means an identity equation failed; no JSON is written in that case, so do not fall back to a stale file). The handoff comment must carry the S2 summary (comment totals by class plus unresolved counts). If `unresolved_ai_reviewer > 0` or `unresolved_human > 0`, do **not** hand off to review — single-hop route back to the current implementer and name the specific items in the issue. Bot/AI and unresolved human comments are inputs to the review, not merge signals: merge eligibility still reads only Monica metadata (`review_status` / `approved_head_sha`), never GitHub PR reviews.
- Verdict ledger: pass `--ledger <path>` so repeated bot findings reuse a prior verdict instead of being re-adjudicated. When `ledger_refuted_hotspots` reports a bot opinion refuted >= 3 times, raise it once to hallong (add a suppression rule or disable that bot) rather than re-asking every round. Verdicts land in Monica only — never reply to bots, resolve threads, or react on GitHub.

Exact-head merge applicator:

- ID: `80daf5ee-872b-487c-87ef-5a8dd1a6227f`
- Title: `PR Merge Applicator / exact-head 自动合并终检`
- Mode: `run_only`
- Status: `active`
- Assignee type: `agent`
- Assignee: `loonghao` (`4ef9454a-16f4-45dd-b6a3-46b3c3619b9c`)
- Trigger ID: `4e3a5d9d-be85-4d11-9c60-fc96a092e811`
- Schedule: `25,55 * * * *` Asia/Shanghai
- Purpose: final apply/router lane for PRs that already passed exact-head loonghao review, required checks, public metadata, and mergeability gates.
- It does not review or fix code. If gates fail, it routes the exact gap back to CI/DevOps, implementer, loonghao re-review, or hallong.
- Exact-head rule: merge only when the current `headRefOid` matches a loonghao GitHub approval commit or canonical Monica metadata `loonghao_reviewed_head_sha`.

Core release downstream iteration:

- ID: `5a5cc21e-bcfd-4dbd-bd14-ac2a3b73e164`
- Title: `dcc-mcp-core release -> 下游专队迭代`
- Mode: `create_issue`
- Status: `active`
- Assignee type: `squad`
- Assignee: `dcc-mcp 开发小组` (`8338fbed-3883-45a9-9ed1-5b3cf1038393`)
- Trigger ID: `7e855e9a-3bfe-47a2-97b9-4338937b3b5f`
- Trigger label: `GitHub release published dcc-mcp-core`
- Filter: `release / published`
- GitHub hook: repo hook `636978139` on `dcc-mcp/dcc-mcp-core`, event `release`, content type `json`
- Purpose: when release-please publishes a new `dcc-mcp-core` GitHub Release/tag, create a squad issue that reads the core release log and drives downstream DCC MCP repositories through dependency/API/docs/test/release iteration.
- Routing: top-level intake splits downstream work to Backend/Core for dependency/API/contract/adapter implementation, CI/DevOps/Release for release validation, Docs/Knowledge for docs/skills/release notes/public boundary, and Frontend/UI for web/UI work.
- Fallback: the hourly self-loop autopilot checks the latest core release and manually triggers this autopilot if no active Monica issue exists for the latest tag or release URL.
- Core follow-up labels: `core-release-followup`; add `adapter-blocker` when the issue blocks or complicates downstream adapter maintenance.

## Active repo-level CI hook coverage

Repos covered at snapshot time for PR CI failure and PR CI success review hooks:

- `.github`
- `dcc-mcp-3dsmax`
- `dcc-mcp-blender`
- `dcc-mcp-core`
- `dcc-mcp-fpt`
- `dcc-mcp-houdini`
- `dcc-mcp-maya`
- `dcc-mcp-openusd`
- `dcc-mcp-photoshop`
- `dcc-mcp-unreal`
- `dcc-mcp-zbrush`
- `demo-repository`
- `marketplace`
- `adobepy`
- `dcc-mcp-maya-mgear`
- `loonghao/vx` through dedicated repo-level workflow_run triggers

## Harness Timers And Gardeners

Approved staggered timer layout:

- Core release fallback: `0,30 * * * *` Asia/Shanghai.
- PR/self-loop heartbeat: `5,35 * * * *` Asia/Shanghai.
- Status recovery heartbeat: `10,40 * * * *` Asia/Shanghai.
- Automation health audit: `20 */4 * * *` Asia/Shanghai.
- Exact-head merge applicator: `25,55 * * * *` Asia/Shanghai.
- Docs gardener: `15 4 * * *` Asia/Shanghai.
- Agent-first code cleanup / harness GC: `30 3 * * *` Asia/Shanghai.

Harness gardener autopilots:

- `Automation Health / 定时器审计`: `616be4bd-42aa-48db-8f1d-fb15917fc08b`, run-only, assignee `代码测试` (`ce3a0e33-00ad-4db8-aca9-0717a451fc71`), trigger `ae14cde8-d041-43c9-9769-39c6dbe37246`.
- `PR Merge Applicator / exact-head 自动合并终检`: `80daf5ee-872b-487c-87ef-5a8dd1a6227f`, run-only, assignee `loonghao` (`4ef9454a-16f4-45dd-b6a3-46b3c3619b9c`), trigger `4e3a5d9d-be85-4d11-9c60-fc96a092e811`.
- `Harness GC / agent-first 代码清理巡检`: `aaf8b4ad-8963-4b9b-a165-3bd442216baa`, run-only, assignee `倩倩` (`a7020420-eb91-4f40-baaa-d47697db6091`), trigger `3d30bec4-503d-407d-aa57-a3334fc27974`.
- `文档检查 -> Docs/Knowledge leader`: `6211067a-9fae-47d6-9fbf-bb3b913780b4`, create-issue, assignee `小白` (`a7020420-eb91-4f40-baaa-d47697db6091`), trigger `a767de7b-d7f1-4bca-8659-27d04545470b`.

Scheduled autopilots must check recent runs before scanning. If another run is active or a recent run is inside the overlap window, exit with `no_action_timer_overlap`. Use bounded batches and compact association metadata so the next heartbeat can continue without reading raw payloads.

## Known operational note

An org-level `workflow_run` hook produced `429 rate limit exceeded` during setup because it concentrated all CI traffic into one Monica webhook token. The stable pattern is repo-level `workflow_run` hooks with separate Monica triggers.

Current CLI builds always accept agent IDs/names for `monica autopilot create/update --agent`; some updates also accept squad IDs. If a squad update returns `assignee must be a valid agent in this workspace`, create/update with a temporary valid agent and patch `assignee_id` plus `assignee_type = squad` through the API.

Specialized routing:

- GitHub issue intake remains top-level first, then routes by component.
- PR CI failure is assigned first to CI/DevOps/Release.
- PR CI success remains direct to loonghao after green checks and public metadata cleanup.
- Docs check remains assigned to 小白, who is the Docs/Knowledge leader.
- Hourly patrol scans the top-level squad and all four specialized squads.

PR implementers must not post a live loonghao review mention while CI is pending after a push or force-push. They keep ownership until the current head SHA has clean public metadata and required GitHub checks are green; pending waits may use no-mention progress comments only.

Dynamic assignment/status decision:

- Changing assignee is an exclusive phase transfer, not a generic notification. It enqueues the new assignee for non-`backlog` issues and cancels active tasks on the same issue.
- Use status as the board state: `todo` for ready owner work, `in_progress` while the owner executes or watches CI, `in_review` for a review gate, `blocked` for external decisions, `done` only after evidence, `cancelled` for duplicate/no_action.
- Distinguish `in_review` with metadata such as `pipeline_phase=intake_review`, `code_review`, `qa_review`, or `product_acceptance`.
- Safe handoff order is: add evidence comment, set next status, then reassign as the final mutation and stop. For lightweight input, use a single live mention comment and keep assignee unchanged.
- Use `monica issue rerun <issue-id>` for current-assignee recovery and `monica issue cancel-task <task-id> --issue <issue-id>` for one bad run.

Intake review automation:

- `status=in_review` + `metadata.pipeline_phase=intake_review` means the issue is waiting for intake to collect enough information before implementation.
- Intake is complete when repo/project, work type, acceptance criteria, priority, first owner, blockers, and GitHub association are clear.
- The hourly patrol may then comment the intake summary, set `metadata.pipeline_phase=implementation_ready`, set status `todo`, and assign the concrete first owner as the final mutation.
- If intake data is incomplete, keep `in_review` and single-mention 晓黎/倩倩/loonghao for the missing decision instead of sending vague work to developers.

Human decision escalation:

- `hallong` is the human owner escalation target: `[@hallong](mention://member/c73ed444-85e7-407f-ac76-cc71fd7c2648)`.
- Use this for credentials, paid/budget choices, external account settings, release policy, sensitive/private information boundaries, local-only DCC/UI access, cross-repo high-risk merge or rollback, and ambiguous product/business tradeoffs.
- The escalation comment must include the exact decision question, context/evidence, options A/B/C, the recommended option, risk/cost/deadline impact, and what the squad will do after the answer.
- Set `metadata.human_decision_required=true`, `metadata.waiting_on=hallong`, `metadata.decision_owner_member_id=c73ed444-85e7-407f-ac76-cc71fd7c2648`, and `metadata.pipeline_phase=human_decision` when the issue is in decision review.
- Use `blocked` if the decision blocks execution; use `in_review` plus `pipeline_phase=human_decision` if the issue is waiting for human decision review.
- Do not repeatedly mention hallong for the same unresolved question. Keep metadata/status current and add no-mention progress unless new evidence changes the decision.
- Keep GitHub public text safe: do not copy Monica issue IDs, internal comments, internal keys, local paths, sensitive payloads, or agent routing history into GitHub.

Context optimization:

- DCC MCP agents have `headroom` and `codegraph` MCP configured.
- For repo work, use `vx codegraph status <repo>`, initialize with `vx codegraph init -i <repo>` when missing, and refresh with `vx codegraph sync <repo>` after edits or branch changes.
- Prefer CodeGraph for symbol/architecture/call-path/impact questions before raw grep.
- Prefer `vx gh --json --jq`, `vx rg -n -m <N>`, `vx git diff --stat`, `vx git diff --name-only`, and `vx --compact` before reading raw logs or giant files.
- Use Headroom MCP to compress large CI logs, search results, generated outputs, long docs, and API payloads, then retrieve exact originals only when needed.
- Keep `.codegraph/` and Headroom caches out of commits; never compress/publish secrets or exact sensitive payloads.

Why tasks may not advance despite high agent concurrency:

- `max_concurrent_tasks` is only a per-agent execution ceiling; it does not assign, mention, rerun, or unstick issues.
- Active `todo` issues with no assignee need routing before any agent can work.
- Squad-owned issues require the leader/patrol to choose the next concrete owner.
- `in_review` and `in_progress` can become stale when the expected next trigger is missing.
- `waiting_local_directory` and real external blockers need configuration/human action, not more concurrency.

The hourly patrol should repair unassigned dcc-mcp/core/adapter/GitHub PR/CI work, stale `in_review`, stale `in_progress`, and missing review handoffs before considering concurrency changes.
