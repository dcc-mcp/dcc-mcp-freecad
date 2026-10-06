# Monica GitHub Autopilot Ops

Use this skill when the user asks to create, repair, verify, or extend Monica automations that connect GitHub webhooks to the dcc-mcp team, especially for GitHub issue intake and PR CI failure handling.

## Goal

Turn a user requirement such as "new GitHub issues should become Monica issues" or "PR CI failure should ask the dcc-mcp team to fix it" into a working, verified Monica autopilot setup.

## Required tools

- Use `monica` for workspace state, autopilots, triggers, skills, agents, squads, and issues.
- Use `gh` for GitHub org/repo hooks, deliveries, pings, PRs, checks, and workflow logs.
- Use the Monica docs for current webhook event semantics when behavior is unclear: https://monica.ai/docs/zh/autopilots

## Operating rules

- Treat live Monica and GitHub state as source of truth. Inspect before editing.
- Prefer updating an existing autopilot over creating duplicates.
- Use `application/json` webhook content type in GitHub.
- Never print or store full Monica webhook URLs, webhook tokens, PATs, `GITHUB_TOKEN`, local paths, or internal hostnames in public GitHub text.
- Do not put Monica webhook URLs into issue/PR bodies or comments.
- Do not put Monica issue IDs, internal issue IDs, internal tracking keys, comment/task/autopilot/run IDs, or agent routing history into GitHub PR bodies, issue comments, commits, reviews, or release notes.
- Do not use GitHub closing keywords (`fixes`, `closes`, `resolves`) with any internal/Monica issue ID.
- Use redacted URLs in summaries: `https://api.monica.ai/api/webhooks/autopilots/[redacted]`.
- Chinese GitHub comment boundary: if a GitHub PR/issue comment, PR review, or PR inline review comment is written in Chinese and the author is `loonghao`, or the PR submitter/head author is `loonghao`, treat the comment as internal review/coordination. Do not reply publicly on GitHub. Mirror or continue the conversation in the associated Monica issue with a compact internal comment.
- Exception: if the PR/issue submitter is not `loonghao` or the commenter is an external/non-loonghao contributor who needs a public answer, reply on GitHub only with public-safe content, preferably concise English or the contributor's language. Never expose Monica issue IDs, routing history, or internal discussion.
- GitHub public comment default: do not write GitHub issue/PR comments or reviews for internal Monica workflow, review handoff, CI waiting, implementation status, or loonghao-owned/internal PR coordination. Keep that content in Monica. Only write a GitHub public reply when it is explicitly needed for an external contributor or public maintainer communication, and then it must be concise English and public-safe. Never use Chinese for GitHub public workflow/review/status comments.
- If an agent accidentally posts a Chinese/internal GitHub comment, delete or edit it away when permitted, record the correction inside Monica, and do not post a public replacement unless a human explicitly asks for an English public-safe note.
- PR state guard: every PR-handling autopilot or patrol must live-read `gh pr view ... --json state,mergedAt,headRefOid,mergeStateStatus,reviewDecision` before review/fix routing. If the PR is `MERGED` or `CLOSED`, stop review/fix routing, do not mention the next reviewer, mark review handoff issues `done`, mark obsolete fix issues `cancelled`, and add only a compact internal state comment.
- PR merge executor rule: webhook/patrol autopilots should not merge directly, but the `loonghao` review task must close the loop. If code review passes and merge gates are satisfied, `loonghao` must merge the PR or enable the repo's merge queue/auto-merge, then mark the Monica issue done with evidence. If review fails, `loonghao` must route the issue back to exactly one implementer with a live mention or reassignment, set status back to `todo`/`in_progress`, and include the actionable checklist. Do not leave a rejected PR in `in_review` without waking the fixer.
- Merge mode rule: final DCC MCP PR merges should use rebase merge or the repository's configured rebase/merge-queue equivalent. If a PR is behind, dirty, or conflicting, do not review/merge it as ready; route base-sync/conflict repair first, then require CI and exact-head review again.
- Monica internal review comments follow the same rule as GitHub reviews. A plain Monica issue comment that says `不足`, `不能合并`, `not ready`, `request changes`, or lists blocking feedback does not enqueue work unless it contains a live mention, reassignment, or rerun. Reviewers must perform that routing before ending the task; patrol must repair it when they forget.
- Do not paste raw webhook payloads, full autopilot prompts, or full `monica issue get` JSON into issue descriptions/comments. Create compact issue descriptions instead: association block, live GitHub URLs, current gate, next owner, and short evidence. Keep normal webhook-created issue descriptions around 1-2 KB.
- If an issue contains the generated wrapper block beginning with `Autopilot run triggered`, `Webhook event:`, or `Webhook payload:`, treat that block as ignorable transport metadata. Do not read it into context, summarize it, quote it, copy it, or hand it to another agent. Prefer an existing canonical compact issue for the same GitHub PR/run; if none exists and the event is actionable, create or update one compact issue and cancel/close the wrapper duplicate.
- Store only a compact association block on canonical issues. Set `metadata.payload_storage=compacted` and `metadata.ignore_raw_webhook_block=true` when cleaning old wrapper issues.
- When inspecting large Monica issues, project fields instead of printing the full object: `monica issue get <id> --output json | ConvertFrom-Json | Select-Object id,number,title,status,assignee_id,assignee_type,metadata`.
- Verify with GitHub webhook ping and delivery status after configuration.
- If org-level `workflow_run` causes `429 rate limit exceeded`, delete or disable that hook and switch to repo-level hooks with separate Monica triggers.

## Harness And Timer Design

Design GitHub automations as an agent harness, not as isolated prompts.

- Webhooks are the fast path. They classify one event, resolve live GitHub state, and create/update one compact canonical Monica issue.
- Scheduled heartbeats are the recovery path. They catch missed webhooks, stale review feedback, unresolved CI gates, dirty PRs, missed releases, and stuck Monica statuses.
- Gardeners are the maintenance path. They clean up code/docs/automation entropy and verify the harness itself still works.
- Timer layout should be staggered: core release fallback on minute `0,30`; PR/self-loop heartbeat on `5,35`; status recovery on `10,40`; automation health on `20 */4`; docs gardener at `04:15`; code cleanup at `03:30` Asia/Shanghai.
- Every scheduled autopilot must check recent runs for overlap, limit each run to a bounded batch, and store enough metadata for the next run to continue.
- Event handlers and heartbeats must never paste raw webhook payloads or full CLI JSON into issue descriptions. Use compact association blocks and live GitHub/Monica links.
- Agent-first means the default output of an actionable automation is a state mutation or PR, not a report. Escalate to hallong only for product/risk/credential/policy decisions.

## Provider Balance Retry Rule

Patrols and recovery autopilots must handle model/provider balance errors before escalating.

- Match `API Error: 402 Insufficient Balance`, `insufficient balance`, `quota exceeded`, and equivalent billing/quota text in task logs, run summaries, issue comments, or agent outputs.
- First occurrence: keep the issue actionable, add compact Monica metadata/comment, then rerun the current owner or route to a role-compatible cheaper/available model/runtime if that is already configured.
- Free fallback policy: use `opencode/deepseek-v4-flash-free` through `小白` for low-risk patrol, docs, issue compaction, metadata cleanup, and audit evidence gathering. It can prepare merge preflight evidence but must not replace loonghao's final review/merge authority.
- Second consecutive occurrence for the same issue/provider/model: stop automatic retries, set `human_decision_required=true`, `waiting_on=hallong`, `provider_error=402_insufficient_balance`, and mention hallong once with provider/model, affected issue/PR, attempted retry, and recommended fallback.
- Never mirror provider balance failures to GitHub public comments.
- Do not label the PR/issue `needs-human` publicly solely because of a private provider balance error.

## Cleanup And Documentation Automations

Keep maintenance explicit:

- `Harness GC / agent-first 代码清理巡检` should look for dead code, stale generated files, duplicate wrapper issues, obsolete metadata, invalid public references, and repo hygiene drift. It should create small cleanup issues/PRs, not large refactors.
- Completed implementation/review/CI tasks should clean safe temporary worktrees. Remove only agent-created task worktrees after verifying no uncommitted/unpushed changes, no active process, and no active Monica issue/task still points at the path. Never delete a primary user repo or ambiguous path.
- New repo work should start from current remote main through `vx wt`/clean worktree unless the task explicitly targets an existing PR branch. Rebase existing task branches onto remote main before new edits when safe.
- `Harness Docs Gardener / 文档系统巡检` should keep `AGENTS.md`, README, docs, skills, runbooks, ADRs, release notes, and public/private boundary docs aligned with live code and automation behavior.
- `Automation Health / 定时器审计` should inspect autopilot runs, stale `last_run_at`, overlapping runs, disabled triggers, repeated `429`/delivery failures, and payload-size regressions.
- Cleanup and docs automation must respect active project gates, public-safe GitHub boundaries, and the same single-hop mention rule as PR automation.

## ClawSweeper-Inspired Maintenance Pattern

Use `references/clawsweeper-patterns.md` when improving issue cleanup, PR repair, or auto-merge automation.

Required adaptations:

- Separate review/proposal from apply/mutation. Agents may classify and repair; every close, cancel, push, label, or merge must re-read live state immediately before mutation.
- Bind merge readiness to an exact PR head SHA. A loonghao approval or review marker is valid only for the SHA it reviewed.
- Pending checks are wait states, not repair failures. Terminal required-check failures are repair states.
- Use one canonical Monica issue per GitHub item as the durable status surface. Wrapper issues and raw payload blocks are cancellable once a compact canonical issue exists.
- Cap automatic repair loops by PR and by head SHA; route to human review when the cap is reached.
- Close/cancel only high-confidence duplicate, superseded, implemented-on-main, fixed-by-candidate, or wrapper-duplicate cases after live `updatedAt`/head-state validation.

## Discovery checklist

1. Confirm CLI auth:
   - `monica auth status`
   - `gh auth status`
2. Find the dcc-mcp squad:
   - `monica squad list --output json`
   - Expected squad name: `dcc-mcp 开发小组`
3. Inspect existing autopilots:
   - `monica autopilot list --output json --full-id`
   - `monica autopilot get <autopilot-id> --output json`
4. Inspect GitHub hooks:
   - `gh api orgs/dcc-mcp/hooks`
   - `gh repo list dcc-mcp --json name,isArchived,isPrivate,url --limit 200`

## Event mapping

GitHub sends the event name in `X-GitHub-Event`. Monica filters use that event plus an action value from the webhook payload.

Common rules:

- New GitHub issue: event `issues`, action `opened`.
- GitHub PR/issue conversation comment: event `issue_comment`, action `created`.
- GitHub PR review submitted: event `pull_request_review`, action `submitted`.
- GitHub PR inline review comment: event `pull_request_review_comment`, action `created`.
- CI run completed: event `workflow_run`, action `completed`; check `workflow_run.conclusion` in the autopilot prompt before acting.
- GitHub release published: event `release`, action `published`. Prefer `published` for release-driven automation because it covers the publish point for stable releases and prereleases.

For GitHub Actions CI webhooks, do not filter Monica triggers on `success` or `failure`. GitHub's `workflow_run` webhook action is `completed`; the result lives in `workflow_run.conclusion`. Configure both CI-success and CI-failure autopilot triggers as `workflow_run` + `completed`, then no-op inside the prompt when the conclusion is not for that automation.

For CI failure work, keep a second guard in the autopilot instructions:

- Only act when `workflow_run.conclusion` is one of `failure`, `timed_out`, `cancelled`, or `action_required`.
- Do not treat an empty `workflow_run.pull_requests` array as final. Some dcc-mcp repos run CI on `push`, so PR branch runs and post-merge default-branch runs may both arrive with `pull_requests=[]`.
- Resolve PR association before deciding:
  1. Use payload PRs when present.
  2. Otherwise call `gh api -H "Accept: application/vnd.github+json" repos/<owner>/<repo>/commits/<head_sha>/pulls`.
  3. If that returns no open PR and `head_branch` is not the repo default branch, search open PRs by head branch and matching `headRefOid`.
  4. If the run is a default-branch success with only merged/closed PRs or no PR, record `no_action_main_post_merge_success`.
  5. If the run is a default-branch failure, create/update a CI/DevOps issue for `[repo@branch] CI failed`; do not silently ignore it.

## GitHub issue and PR association

Every Monica issue created from GitHub, or later found to contain a GitHub URL, must keep a durable association block in the Monica description or a top-level comment:

- `GitHub:` issue or PR URL
- `Repo:` full repository name
- `Number:` GitHub issue or PR number
- `Type:` `issue` or `pr`
- `Labels:`
- `Milestone:`
- `GitHub assignees:`
- `Author:`
- `Linked GitHub issues:` for PRs that mention `fixes #N`, `closes #N`, or issue URLs
- `Routing signal:`
- `Monica next owner:`

Use `gh issue view` / `gh pr view` to refresh GitHub state before routing. Treat GitHub assignees as routing signals, not as Monica assignees. Preserve unknown GitHub usernames in the Monica comment and ask `loonghao` or `倩倩` to choose the Monica owner.

Default routing from GitHub metadata:

- `docs`, `README`, `AGENTS`, `llms`, `skills`, `release notes`, `public boundary` -> `dcc-mcp Docs/Knowledge 小队` or leader `小白`
- `frontend`, `UI`, `web`, `docs site`, `dashboard`, `UX` -> `dcc-mcp 前端/UI 小队` or leader `传林`
- `backend`, `core`, `rust`, `go`, `python`, `protocol`, `API`, `schema`, `adapter integration` -> `dcc-mcp 后端/Core 小队`
- `CI`, `GitHub Actions`, `release`, `release-please`, `tag`, `branch protection`, `test`, `e2e`, `flaky`, `coverage` -> `dcc-mcp CI/DevOps/Release 小队` or leader `代码测试`
- `product`, `spec`, `needs-triage` -> `晓黎`
- cross-module or unclear ownership -> top-level `dcc-mcp 开发小组`, `爱写代码的小龙`, or `loonghao`

Current dcc-mcp squads:

- Top-level intake/orchestration: `dcc-mcp 开发小组` / `8338fbed-3883-45a9-9ed1-5b3cf1038393`, leader `倩倩`.
- Backend/Core: `dcc-mcp 后端/Core 小队` / `227b3059-10fa-4796-bdb4-358a86cee975`, leader `爱写代码的小龙`.
- Frontend/UI: `dcc-mcp 前端/UI 小队` / `898f53c7-fd21-4275-826a-2c41a9d48e59`, leader `传林`.
- CI/DevOps/Release: `dcc-mcp CI/DevOps/Release 小队` / `4d2b46d0-a048-4a19-81e5-21d31b8fbd87`, leader `代码测试`.
- Docs/Knowledge: `dcc-mcp Docs/Knowledge 小队` / `e43d4273-902f-4a00-9e73-f887834525a5`, leader `小白`.

When writing a Monica comment through CLI, use effective mention markdown such as `[@loonghao](mention://agent/4ef9454a-16f4-45dd-b6a3-46b3c3619b9c)`, not plain text `@loonghao`.

Keep GitHub public text public-safe and English-only. Default to no GitHub public comment. Never mention Monica webhook URLs, trigger tokens, PATs, local paths, internal hostnames, private provenance, raw private payloads, Monica IDs, or agent routing in GitHub issues, PRs, comments, or reviews.

Monica issue IDs and comments are internal-only. Do not put Monica issue IDs, Monica issue URLs, comment/task/autopilot/run IDs, internal short IDs, internal issue keys, agent routing history, or internal discussion excerpts into GitHub PR bodies, GitHub comments, reviews, commits, or release notes. In particular, never use `#123`, `fixes #123`, `closes #123`, or `resolves #123` for a Monica/internal issue; GitHub will treat it as a GitHub issue reference and may create wrong links or close the wrong issue. Use `fixes/closes/resolves #N` only after verifying `#N` with `gh issue view` in the target GitHub repo.

## Dynamic assignee and status flow

Use assignee changes for exclusive ownership transfer, not for lightweight notification.

For the shared status/activity policy, read `references/status-flow-policy.md`. It maps UI statuses 审核中/进行中/阻塞中 to `in_review`/`in_progress`/`blocked`, explains how to use squad activity records, and defines review/blocked recovery rules.

Monica behavior to account for:

- Assigning a non-`backlog` issue to an agent or squad enqueues a task for that assignee.
- Assigning a `backlog` issue parks the work until status becomes `todo` or `in_progress`.
- Reassigning cancels all active tasks on that issue before enqueuing the new owner.
- A comment mention enqueues the mentioned agent without changing assignee or status.

Automation routing rules:

- New durable work from GitHub issue webhooks should usually be assigned to top-level `dcc-mcp 开发小组` for intake, then routed to the right specialized squad or concrete first owner, with status `todo` only after intake is ready.
- If the GitHub issue is not implementation-ready, keep status `in_review`, set `metadata.pipeline_phase=intake_review`, assign to the PM/intake owner or squad, and wait for intake recovery before implementation.
- PR CI failure issues should be assigned first to `dcc-mcp CI/DevOps/Release 小队`, status `todo` initially, then `in_progress` while the fixer owns the PR or waits for CI.
- PR CI success review handoff should be status `in_review` and assigned to `loonghao` only after required checks are green and public metadata is clean.
- If an active issue already exists and only needs a quick nudge, add one single-mention comment and keep assignee unchanged.
- If an active issue is assigned to the wrong owner and no active task should continue, post a short reason, set the right status, then reassign as the final mutation.
- If work truly needs parallel owners, create child issues or separate tracking issues instead of repeatedly reassigning the same issue.
- Use `monica issue rerun <issue-id> --output json` for recovery when the current assignee should retry from a fresh task. Use `monica issue cancel-task <task-id> --issue <issue-id> --output json` for a specific bad run.

Important: high agent concurrency does not enqueue work. GitHub and patrol automation must still assign, mention, or rerun tasks.

Patrol recovery responsibilities:

- `run_only` autopilot mode is not read-only. It only means Monica should not create a wrapper issue for the autopilot run. A patrol run may update authorized existing state through `monica` and `gh` when it finds actionable gaps; new issue creation requires explicit automation permission. It may update existing Monica issues, add exactly one live mention when needed, assign/reassign/rerun, update status/metadata, and add public-safe GitHub labels when the rule calls for it. GitHub comments are exceptional and allowed only for external/public communication in concise English.
- The dedicated status recovery autopilot `c402be53-ab90-4bd9-bc82-bf633b1c6f8b` handles Monica UI states 审核中/进行中/阻塞中 (`in_review`/`in_progress`/`blocked`) every 15 minutes. PR/GitHub patrols should cooperate with it by setting clear metadata: `pipeline_phase`, `review_status`, `blocked_reason`, `waiting_on`, `next_check_at`, and GitHub PR association keys.
- Do not finish a patrol with report-only output when a real dcc-mcp PR or issue needs routing. Demo/example repositories and Renovate/configure-only PRs can be ignored unless the user explicitly includes them.
- Route unassigned active `todo` issues for dcc-mcp/core/adapter/GitHub PR/CI work to a concrete owner, the relevant specialized squad, or the top-level dcc-mcp squad when intake is still missing.
- For `in_review` issues with `metadata.pipeline_phase=intake_review`, promote automatically only when repo/project, work type, acceptance criteria, priority, first owner, blockers, and GitHub association are clear. Then set `pipeline_phase=implementation_ready`, set status `todo`, and assign the concrete owner as the final mutation.
- Repair stale `in_review` by checking PR CI/review state and triggering loonghao only when the current head SHA passes the PR CI handoff gate.
- If the latest `loonghao` review/comment contains blocking feedback such as request changes, not ready, 不足, 不能合并, or needs fix, and the issue has no active implementer run, route back to the current implementer/squad with one live mention or rerun the current assignee. Set status to `todo` or `in_progress` and metadata `review_status=changes_requested`.
- Treat loonghao's Monica issue comments the same as PR reviews. If the latest blocking feedback is inside Monica and has no `mention://agent`, it still must be routed back to the implementer; otherwise no agent task will start.
- If the live PR is already `MERGED` or `CLOSED`, repair stale `in_review` / `in_progress` by closing the loop internally instead of starting another review. Do not trust a stale Monica issue title, cached `mergeStateStatus`, or old comment saying the PR was open.
- Repair stale `in_progress` by checking issue runs; rerun the current assignee when the work should continue and no active run exists, or route to the next owner with evidence.
- Treat `waiting_local_directory` as a repo binding/runtime availability problem. Fix local directory mapping or route to an agent with the repo path.
- Keep real external blockers as `blocked` and do not repeatedly wake agents for missing credentials, PyPI/GitHub settings, local DCC UI, or human approval.

Blocked issue recovery:

- If the blocker is agent-actionable, such as CI/test failure, code defect, review feedback, missing local repo binding, stale codegraph/headroom setup, or needing to create/read a GitHub issue, do not keep it indefinitely in `blocked`. Add a compact unblock plan, set `metadata.blocked_actionable_by_agent=true`, move status to `todo` or `in_progress`, and assign/rerun the right owner.
- If the blocker needs real human decision, keep `blocked` but mention `hallong` exactly once and store `human_decision_required=true`, `waiting_on=hallong`, `decision_owner_member_id=c73ed444-85e7-407f-ac76-cc71fd7c2648`, and `pipeline_phase=human_decision`.
- If the blocker is an external wait, keep `blocked` with `blocked_reason`, `waiting_on`, and `next_check_at`; patrol should re-check after `next_check_at` and promote to `todo`/`in_progress` when it becomes actionable.

## Human decision escalation

When an autopilot or agent cannot safely choose because of credentials, paid/budget tradeoffs, external account settings, release policy, sensitive/private information boundaries, local-only DCC/UI access, cross-repo high-risk merge or rollback, or ambiguous product/business choices, escalate to the human owner instead of silently stalling.

Add one Monica comment with exactly one live member mention:

```markdown
[@hallong](mention://member/c73ed444-85e7-407f-ac76-cc71fd7c2648)
```

The comment must state the exact decision needed, current evidence, options A/B/C, the recommended option, risk/cost/deadline impact, and what the squad will do after the answer.

Set recovery metadata:

- `metadata.human_decision_required = true`
- `metadata.waiting_on = hallong`
- `metadata.decision_owner_member_id = c73ed444-85e7-407f-ac76-cc71fd7c2648`
- `metadata.pipeline_phase = human_decision` when the decision is a review phase.

Set status `blocked` if the decision blocks execution. Set `in_review` plus `pipeline_phase=human_decision` if it is a decision review. Do not repeatedly mention hallong for the same unresolved question; add no-mention progress unless new evidence changes the choice.

GitHub public boundary: if a human-decision blocker must be mirrored publicly, write only a safe gate summary. Do not include Monica issue IDs, internal comments, internal issue keys, local paths, private payloads, or agent routing history.

## Product research and RFC gate

The daily PM research/RFC autopilot is for product discovery and human decision only.

- Autopilot title: `每日 DCC MCP 产品调研/RFC 提案`.
- Assignee: `晓黎` (`4ef9454a-16f4-45dd-b6a3-46b3c3619b9c`).
- Schedule: 09:00 Asia/Shanghai every day.
- Research recent competitor signals, MCP skills, DCC/AI automation news, GitHub releases/issues/PRs, and official release notes/docs relevant to DCC MCP.
- Tie every proposed RFC to the current DCC MCP org and include sources, dates, affected repos/modules, expected benefit, effort/risk, owner/squad, acceptance criteria, blockers, and priority.
- If the proposal needs a real decision, set `pipeline_phase=product_rfc_review`, `human_decision_required=true`, `waiting_on=hallong`, and `decision_owner_member_id=c73ed444-85e7-407f-ac76-cc71fd7c2648`, then mention hallong exactly once.
- Until hallong explicitly approves, do not create implementation child issues, assign developer agents/squads, open public GitHub issues, or create PRs.
- After approval, split into implementation child issues and route to Backend/Core, Frontend/UI, CI/DevOps/Release, or Docs/Knowledge.

## Context optimization rule

All GitHub issue, PR, CI, and release automations should reduce context before reasoning over large outputs.

- Use `vx codegraph status <repo>`, `vx codegraph init -i <repo>`, and `vx codegraph sync <repo>` so repo exploration starts from an index instead of repeated raw scans.
- Use CodeGraph queries for symbol lookup, architecture tracing, callers/callees, impact analysis, and affected tests before broad `rg`.
- Use GitHub structured reads first: `vx gh pr view --json ... --jq ...`, `vx gh run view --json ... --jq ...`, and `vx gh pr checks --json ...`.
- Use `vx rg -n -m <N>` for bounded log/error search and `vx --compact gh run view <run> --log` only when a broad log view is required.
- Use Headroom MCP tools to compress large logs, search results, API payloads, and long docs before analysis; retrieve originals by hash only for the exact missing detail.
- Do not compress or publish secrets, webhook URLs, PATs, private payloads, or exact sensitive text. Keep `.codegraph/` and Headroom caches out of commits.

## Single-hop mention safety

Webhook and patrol autopilots must use one live Monica mention per new issue comment. The live mention is only for the current executor. Future next owners must be written as plain text such as `NEXT_OWNER: loonghao` or `完成后请在新评论中触发 loonghao`, not as `[@loonghao](mention://agent/...)`.

Example for a docs fix assigned to 小白:

```markdown
[@小白](mention://agent/a7020420-eb91-4f40-baaa-d47697db6091) 触发原因：...
需要你做：...
NEXT_OWNER: loonghao
```

After 小白 finishes, 小白 creates a new comment that contains the single live mention for loonghao. If an agent is triggered only by an accidental future-owner mention and the comment clearly assigns the task to someone else, it should no-op silently and not post a “not for me” comment.

## Open PR patrol and squad handoff

The periodic self-loop autopilot must scan open GitHub PRs, not only Monica issues. It should route PRs into the dcc-mcp squad self-loop; it should not directly merge PRs itself. Use:

```powershell
gh repo list dcc-mcp --limit 200 --json name,isArchived
gh pr list --repo dcc-mcp/<repo> --state open --json number,title,url,isDraft,mergeable,mergeStateStatus,reviewDecision,labels,assignees,author,updatedAt
gh pr view <number> --repo dcc-mcp/<repo> --json number,title,url,state,isDraft,mergeable,mergeStateStatus,reviewDecision,reviews,comments,labels,assignees,author,body
gh pr checks <number> --repo dcc-mcp/<repo> --required --json name,bucket,state,link
```

Autopilot responsibilities:

- If a PR is open, non-draft, mergeable/clean or plausibly ready, and required checks are green, ensure a Monica tracking issue/comment exists for `loonghao` code-review and merge decision. If it is not ready, route the exact missing gate to CI/DevOps/Release, Backend/Core, Frontend/UI, or Docs/Knowledge.
- Include PR URL, repo, PR number, checks summary, review state, labels, author, and exact missing gate in the Monica handoff.
- Do not add `needs-human` merely because loonghao review is missing. Missing loonghao/code-review approval is the normal squad self-loop path.
- The dcc-mcp squad decides whether to merge. loonghao must use code-review skill. When project gates pass, loonghao should merge or enable auto-merge instead of only writing an approval comment. Gates: explicit approval, developer feedback resolved, required checks green, no conflicts/blockers, and no internal issue references in public PR text.

## Renovate and release-please gate

Classify dependency and release PRs before routing implementation work.

- `release_please_only`: head branch starts with `release-please--`, label includes `autorelease: pending`, or the diff is release manifest/changelog/version/generated docs. These PRs should go to `loonghao` review/merge after CI is green; do not route to developer code work unless CI failure proves a code defect.
- `renovate_only`: author/head/labels indicate Renovate/Dependabot, or the diff only bumps dependency constraints, lockfiles, or generated install docs. A dependency bump alone is not adapter implementation work.
- `watch_only`: additive upstream/core change with no downstream consumer and no failing CI evidence.
- `code_required`: breaking API/schema/contract/runtime behavior, downstream code consumes the changed contract, or a dependency/release PR fails CI with evidence that adapter code must change.

For `renovate_only` PRs that are dirty/conflicting or have no checks, do not route conflict recovery to Backend/Core. Mark/update the Monica issue as `renovate_only` / `no_action_dependency_only`; add the public-safe `dependencies` label when useful; wait for Renovate/dependency automation. If Renovate is missing, create a CI/DevOps configuration issue rather than an adapter code task.

For green `renovate_only` or `release_please_only` PRs, use the normal CI-success handoff to `loonghao` for review/merge. For failed ones, route to CI/DevOps first; create developer implementation work only after the failure is proven to require code/API/schema changes.

If the PR is blocked or abnormal in a way the squad automation cannot safely handle, prefer Monica-only routing. Add a GitHub public comment only when an external contributor needs a public answer; that comment must be concise English and public-safe. Adding a public-safe label such as `needs-human` is allowed:

```powershell
gh pr edit <number> --repo dcc-mcp/<repo> --add-label needs-human
```

Do not label same-repo/internal merge conflicts as `needs-human` on first sight. If live PR state is `mergeable=CONFLICTING`, `mergeable_state=dirty`, or `mergeStateStatus=DIRTY`, first route conflict recovery to the current implementer or Backend/Core squad: update the Monica issue with compact evidence, set `review_status=merge_conflict_required`, `pipeline_phase=implementation_feedback`, `merge_conflict_required=true`, status `in_progress`, then rerun or assign the owner. Use `needs-human` only after conflict recovery fails, permissions block the push, the branch belongs to an external contributor, release/product policy is ambiguous, or ownership cannot be routed. Also use it for failed/pending required checks after enough time, invalid public `fixes #N` / `closes #N` references, branch protection errors, security/privacy concerns, or unresolved review that automation cannot route. Skip PRs already labeled `needs-human` until a human removes the label. Avoid duplicate automation comments by checking existing PR comments first.

## PR CI handoff gate

Any automation or agent that updates a PR branch must keep ownership until the PR is actually ready for review.

Before publishing a Monica comment with a live `loonghao` mention:

- Re-read PR state with `gh pr view <pr> --repo <repo> --json number,title,url,state,isDraft,mergeStateStatus,reviewDecision,headRefOid,body`.
- Read required checks with `gh pr checks <pr> --repo <repo> --required --json name,bucket,state,link`. If no required checks are configured, inspect `statusCheckRollup` and say so.
- Reject pending, queued, in-progress, waiting, failed, cancelled, missing, or unexpectedly skipped checks.
- Inspect public metadata:
  - `gh pr view <pr> --repo <repo> --json body,title`
  - `gh api repos/<owner>/<repo>/pulls/<pr>/commits --jq '.[].commit.message'`
  - reject `PIP-*`, Monica issue IDs/URLs, internal routing history, local paths, or private provenance in public PR text or commit messages.
- Only after the current head SHA is clean and required checks are green should the comment include one live `loonghao` mention.

While checks are pending, do not write `已完成，请复核` with a live review mention. Either keep polling or add a no-mention progress comment. If checks fail, route the failing evidence to the fixer, not the reviewer.

## Recommended automation shape

Use three core GitHub webhook autopilots for dcc-mcp.

Issue intake autopilot:

- Mode: `run_only`
- Priority: `medium`
- Trigger: webhook
- Event filter: `issues / opened`
- GitHub hook: org-level hook on `dcc-mcp`, events `issues`, content type `json`
- Prompt duties:
  - Extract `repository.full_name`, `issue.number`, `issue.title`, `issue.html_url`, `issue.body`, labels, milestone, assignees, author, and sender.
  - Rename/update the Monica issue as `[repo#number] GitHub issue title`.
  - Preserve the GitHub issue association block and a short triage summary.
  - Route from GitHub assignees, labels, and component signals to the top-level squad, specialized squads, or concrete first owner using effective Monica mention markdown.
  - If repo/work type/acceptance criteria/priority/first owner are clear, keep status `todo` while it waits for the routed owner.
  - If any intake field is missing, set status `in_review`, set `metadata.pipeline_phase=intake_review`, and route to 晓黎/倩倩/loonghao for the missing decision. Do not send to implementation until intake recovery marks it `implementation_ready`.
  - Set `cancelled` for duplicates, irrelevant events, or no_action.
  - Decide whether it belongs to dcc-mcp; if not, explain and mark no action.

PR CI failure autopilot:

- Mode: `run_only`
- Priority: `high`
- Assignee: `dcc-mcp CI/DevOps/Release 小队`
- Trigger: webhook
- Event filter: `workflow_run / completed`; distinguish by `workflow_run.conclusion` in the prompt.
- GitHub hooks: prefer repo-level hooks for every active dcc-mcp repository, events `workflow_run`, content type `json`
- Prompt duties:
- Because this is `run_only`, do not rely on a wrapper issue. Create or update a durable Monica issue only after the event is actionable.
- When creating/updating the durable issue, use a compact description. Include `GitHub PR`, `Repo`, `PR`, `Workflow run`, `Conclusion`, `Head branch/SHA`, `Current owner`, `Status`, `Routing signal`, and a short gate checklist. Do not include the raw webhook payload or the full autopilot prompt.
  - Only act for failure-class conclusions: `failure`, `timed_out`, `cancelled`, or `action_required`.
  - For non-failure conclusions, return `no_action_conclusion_mismatch` without creating an issue.
  - If `workflow_run.pull_requests` is empty, first resolve association with `gh api -H "Accept: application/vnd.github+json" repos/<owner>/<repo>/commits/<head_sha>/pulls`; then search open PRs by non-default `head_branch` and matching `headRefOid`. Do not mark `no_action` until both fallbacks are exhausted.
  - For default-branch failures with no open PR, create/update `[repo@branch] CI failed: workflow name` and assign CI/DevOps; main branch CI failure is an actionable repo health issue.
  - Extract repo, PR number, workflow URL, workflow name, conclusion, head branch, head sha, actor, and sender.
  - Rename/update the Monica issue as `[repo#PR] CI failed: workflow name`.
  - Preserve the PR association block, including linked GitHub issues when PR body or commits reference them.
  - Use `gh pr view`, `gh pr checks`, `gh run view --log-failed`, and related commands to inspect the real failure.
  - Create a clean worktree/branch from remote main or the PR branch as appropriate.
  - Fix, push, and keep ownership until the current PR head has clean public metadata and required CI/checks are green.
  - Do not live-mention `loonghao` while CI is pending. If waiting is long, add a no-mention progress comment and keep the issue in progress.
  - If CI fails again, fix or route the failing check to CI/DevOps/Release, Backend/Core, Frontend/UI, or Docs/Knowledge according to root cause. Route review to `loonghao` only after the PR CI handoff gate passes.
  - Keep the issue status `in_progress` while the fixer owns the PR or waits for CI. When the PR CI handoff gate passes, set status `in_review` before the final review handoff.

PR CI success review autopilot:

- Mode: `run_only`
- Priority: `medium`
- Assignee: `loonghao`
- Trigger: webhook
- Event filter: `workflow_run / completed`; distinguish by `workflow_run.conclusion` in the prompt.
- GitHub hooks: repo-level hook for every active dcc-mcp repository, events `workflow_run`, content type `json`
- Prompt duties:
- Because this is `run_only`, do not rely on a wrapper issue. Create or update a durable Monica issue only after the event is actionable.
- When creating/updating the durable issue, use a compact description. Include `GitHub PR`, `Repo`, `PR`, `Workflow run`, `Conclusion`, `Head branch/SHA`, `Current owner`, `Status`, `Routing signal`, and a short gate checklist. Do not include the raw webhook payload or the full autopilot prompt.
  - For non-success conclusions, return `no_action_conclusion_mismatch` without creating an issue.
  - Only act when `workflow_run.conclusion == success` and the workflow run is tied to an open PR.
  - If `workflow_run.pull_requests` is empty, first resolve association with `gh api -H "Accept: application/vnd.github+json" repos/<owner>/<repo>/commits/<head_sha>/pulls`; then search open PRs by non-default `head_branch` and matching `headRefOid`. Do not mark `no_action` until both fallbacks are exhausted.
  - If the only associated PR is already merged/closed, or the run is a default-branch post-merge success, record `no_action_main_post_merge_success`; do not ask loonghao to review an already merged PR.
  - Deduplicate first: `workflow_run success` can fire once per successful workflow. Search active Monica issues for the same GitHub PR URL or `[repo#PR]`.
  - If an active issue already exists and the current PR head SHA has not yet been handed to `loonghao` after all gates passed, add a new comment to the existing issue with one live `loonghao` mention, then mark the duplicate generated issue no_action/cancelled. Dedupe must not silently suppress the first green handoff for a new head SHA.
  - If an active issue already contains a `loonghao` review request for the same head SHA, only update/comment the existing issue without a live mention and mark the duplicate no_action/cancelled.
  - Use `gh pr view` and `gh pr checks --required` to verify the PR is open, non-draft, and all required checks are green.
  - Inspect PR body/title and commit messages for public-safety violations such as `PIP-*`, Monica IDs/URLs, internal routing history, local paths, or private provenance. If found, do not trigger `loonghao`; route back to the implementer with a single live mention and exact cleanup request.
  - Create/rename the Monica issue as `[repo#PR] CI green: code review needed`.
  - Assign directly to `loonghao` so the technical lead can use `code-review` skill and decide whether to merge, send feedback to developers/specialized squads, or escalate a large/complex change to `dcc-mcp 开发小组`.
  - Set status `in_review` for the loonghao review phase; set duplicate/no_action issues to `cancelled` after preserving a short audit comment.
  - Do not merge from the autopilot itself. The autopilot only hands off to loonghao. The loonghao review task must then either merge/enable auto-merge when gates pass, or route actionable feedback back to one implementer when gates fail.
  - Do not add `needs-human` for missing loonghao review; that is the normal success path. Use `needs-human` only for abnormal blockers that cannot be routed safely.

Core release downstream iteration autopilot:

- Mode: `create_issue`
- Priority: `high`
- Assignee: `dcc-mcp 开发小组`
- Trigger: webhook
- Event filter: `release / published`
- GitHub hook: repo-level hook on `dcc-mcp/dcc-mcp-core`, event `release`, content type `json`
- Fallback trigger: schedule every 30 minutes. This is required because `release.published` deliveries can return `429`; the scanner catches releases missed by webhook rate limiting.
- Purpose: when release-please publishes a new core release/tag, create a squad issue that reads the core release log and drives downstream DCC MCP repositories through dependency/API/docs/test iteration.
- Prompt duties:
  - Act for `repository.full_name == dcc-mcp/dcc-mcp-core` and `action == published` when a webhook payload exists; if the run is schedule/manual and has no payload, scan recent releases instead of no-op.
  - Re-read authoritative state with `gh release list --repo dcc-mcp/dcc-mcp-core --limit 10 --json tagName,name,isPrerelease,isDraft,publishedAt,createdAt` and `gh release view <tag> --repo dcc-mcp/dcc-mcp-core --json tagName,name,isPrerelease,publishedAt,body` before deciding.
  - Dedupe by release tag/range and metadata (`core_release_tag`, `core_release_latest_tag`) before creating work.
  - If multiple unprocessed releases exist, create one compact release-train issue for the tag range instead of one issue per tag.
  - Rename/update the Monica issue as `[dcc-mcp-core <tag-or-range>] downstream adapter iteration`.
  - Preserve an association block with `Core release`, `Tag`, `Release notes`, `Published at`, `Prerelease`, `Downstream repos`, `Routing signal`, and `Monica next owner`.
  - Default downstream repos: `dcc-mcp-maya`, `dcc-mcp-blender`, `dcc-mcp-houdini`, `dcc-mcp-3dsmax`, `dcc-mcp-photoshop`, `dcc-mcp-unreal`, `dcc-mcp-zbrush`, `dcc-mcp-openusd`, and `dcc-mcp-fpt`. Include `marketplace` only when release notes or repo state make it relevant.
  - For each downstream repo, decide `PR`, `issue`, or `no_action` for dependency bumps, API/contract alignment, skill/docs changes, CI/test updates, and release follow-up.
  - Split work by specialized squad: Backend/Core for dependency/API/contract/adapter implementation, CI/DevOps/Release for release and validation, Docs/Knowledge for docs/skills/release notes/public boundary, and Frontend/UI for web/UI work.
  - Use single-hop Monica comments or child issues to route work to exactly one current owner at a time.
  - If downstream work exposes a core bug, missing migration path, unstable contract, or avoidable maintenance burden, create a public-safe GitHub issue in `dcc-mcp/dcc-mcp-core` with `core-release-followup` and, if it blocks adapter work, `adapter-blocker`. The GitHub issue sync autopilot will bring that issue back into Monica.
  - For manual fallback runs without a release payload, scan recent releases with the same dedupe/range flow.

## CLI patterns

Create or update an autopilot:

```powershell
$description = Get-Content -Raw "<prompt.md>"
monica autopilot create --title "<title>" --description $description --agent "<agent-id-or-name>" --mode create_issue --priority high --issue-title-template "<template {{date}}>" --output json
monica autopilot update <id> --title "<title>" --description $description --agent "<agent-id-or-name>" --mode create_issue --priority high --status active --output json
```

Current CLI builds always accept agent IDs/names for `--agent`; some builds also accept squad IDs for existing squad-routed autopilots. If a squad update returns `assignee must be a valid agent in this workspace`, create/update with a temporary valid agent, then patch the autopilot:

```powershell
$cfg = Get-Content -Raw "$env:USERPROFILE\.monica\config.json" | ConvertFrom-Json
$headers = @{ Authorization = "Bearer $($cfg.token)"; "X-Workspace-ID" = $cfg.workspace_id; "Content-Type" = "application/json" }
$body = @{ assignee_id = "<squad-id>"; assignee_type = "squad" } | ConvertTo-Json
Invoke-RestMethod -Method Patch -Uri "$($cfg.server_url.TrimEnd('/'))/api/autopilots/<autopilot-id>" -Headers $headers -Body $body
```

Add a webhook trigger:

```powershell
monica autopilot trigger-add <autopilot-id> --kind webhook --label "<label>" --output json
```

Patch trigger event filters when CLI flags do not expose them:

```powershell
$cfg = Get-Content -Raw "$env:USERPROFILE\.monica\config.json" | ConvertFrom-Json
$headers = @{ Authorization = "Bearer $($cfg.token)"; "X-Workspace-ID" = $cfg.workspace_id; "Content-Type" = "application/json" }
$body = @{ enabled = $true; event_filters = @(@{ event = "issues"; actions = @("opened") }) } | ConvertTo-Json -Depth 8
Invoke-RestMethod -Method Patch -Uri "$($cfg.server_url)/api/autopilots/<autopilot-id>/triggers/<trigger-id>" -Headers $headers -Body $body
```

Create or update a GitHub org hook:

```powershell
@{ name = "web"; active = $true; events = @("issues"); config = @{ url = "<webhook-url>"; content_type = "json"; insecure_ssl = "0" } } |
  ConvertTo-Json -Depth 6 |
  gh api --method POST "orgs/dcc-mcp/hooks" --input -
```

Create or update a GitHub repo hook:

```powershell
@{ name = "web"; active = $true; events = @("workflow_run"); config = @{ url = "<webhook-url>"; content_type = "json"; insecure_ssl = "0" } } |
  ConvertTo-Json -Depth 6 |
  gh api --method POST "repos/dcc-mcp/<repo>/hooks" --input -

@{ name = "web"; active = $true; events = @("release"); config = @{ url = "<webhook-url>"; content_type = "json"; insecure_ssl = "0" } } |
  ConvertTo-Json -Depth 6 |
  gh api --method POST "repos/dcc-mcp/dcc-mcp-core/hooks" --input -
```

Verify a hook:

```powershell
gh api --method POST "repos/dcc-mcp/<repo>/hooks/<hook-id>/pings" --silent
gh api "repos/dcc-mcp/<repo>/hooks/<hook-id>/deliveries" --jq '.[0] | {event,status_code,status,delivered_at}'
```

## Current dcc-mcp setup reference

Read `references/current-dcc-mcp-automation.md` when you need the latest known autopilot IDs, trigger layout, and verification shape. Treat it as a snapshot; always verify live state before changing anything.
## Monica delivery surface contract

This skill can inspect or operate external systems, but Monica remains the required user-facing delivery surface. Do not end a task by only posting a GitHub/Gongfeng PR/MR/issue comment, commit message, terminal output, or run log. If an external comment is required, keep it public-safe and treat it as supporting context; then summarize the final result back to the Monica issue/comment thread.

When routing active work from Monica, use exactly one live `[@Name](mention://agent/<uuid>)` or `[@Squad](mention://squad/<uuid>)` mention. Future owners stay as plain text until their turn.
## Monica Milestone Release Gate

Use metadata first; do not add a new milestone object until metadata stops being enough.

Required metadata:

- `monica_milestone`: string, for example `0.9.0`.
- `monica_release_blocker`: boolean. `true` blocks release while unfinished; `false` is tracked in the milestone but does not block release.
- `monica_release_repo`: optional `owner/repo` when the issue belongs to one release-please repo.

Gate statuses: `backlog`, `todo`, `in_progress`, `in_review`, `blocked`.
Done/cancelled issues do not block.

Release gate command shape:

```powershell
$milestone = "0.9.0"
$blockingStatuses = @("backlog", "todo", "in_progress", "in_review", "blocked")
$blockers = foreach ($status in $blockingStatuses) {
  monica issue list `
    --metadata "monica_milestone=$milestone" `
    --metadata "monica_release_blocker=true" `
    --status $status `
    --output json | ConvertFrom-Json
}
$blockers = @($blockers | Where-Object { $_ })
if ($blockers.Count -gt 0) { throw "release blocked by $($blockers.Count) Monica milestone issues" }
```

When the gate is clear, trigger release-please per affected repo instead of publishing manually:

1. Collect repos from `monica_release_repo` on milestone issues; if missing, infer from linked GitHub PR/issue/project only when unambiguous.
2. For each repo, re-read GitHub default branch and existing release-please PRs.
3. Trigger the repo release-please workflow or merge the existing release-please PR according to repo policy.
4. Store compact metadata on the release coordination issue: `monica_release_gate_clear=true`, `monica_release_triggered_at`, `monica_release_triggered_repos`, `monica_release_please_prs`.
5. If a release-please PR or workflow fails, create/update one Monica issue with the same `monica_milestone` and `monica_release_blocker=true`.

Automation rule: scheduled release patrol may scan one active milestone, but it must not trigger release-please more than once per repo per milestone unless the previous trigger failed and was repaired.

