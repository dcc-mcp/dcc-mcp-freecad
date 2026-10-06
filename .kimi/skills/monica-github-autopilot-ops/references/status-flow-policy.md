# Monica Status Flow Policy

This policy turns Monica issue statuses and squad activity records into actionable workflow signals.

## Source Of Truth

Monica issue statuses are intentionally flexible. A status change is a signal, not a trigger by itself. Work starts when an issue is assigned, an agent is mentioned, an issue is rerun, a chat task is sent, or an autopilot fires.

Use status plus metadata plus recent comments/runs to decide the next action.

## Status Mapping

- UI `审核中` = API/CLI `in_review`
- UI `进行中` = API/CLI `in_progress`
- UI `阻塞中` = API/CLI `blocked`

## Activity Records

Squad leader activity such as `action`, `no_action`, and `failed` is useful timeline evidence.

- `action`: the leader delegated or took action. Verify that a real trigger happened: assignment, mention, rerun, child issue, or status/metadata update.
- `no_action`: preserve the decision, but patrol may reopen/reroute if new evidence arrives.
- `failed`: treat as recovery input. Inspect failure reason and rerun/reassign if still actionable.

Do not treat an activity note as enough by itself. If no task was queued and no next owner was woken, the loop is not complete.

## Review Flow

`in_review` must name the current gate:

- `metadata.pipeline_phase=intake_review`
- `metadata.review_status=awaiting_loonghao`
- `metadata.review_status=changes_requested`
- `metadata.review_status=approved`
- `metadata.review_status=qa_review`
- `metadata.review_status=product_acceptance`

Rules:

- If implementation is ready and PR gates are green, trigger the reviewer exactly once for the current head SHA.
- If review passes and merge gates pass, loonghao must merge or enable auto-merge, then mark done with evidence.
- If review fails, the reviewer must route feedback back to exactly one implementer in the same turn using a live mention, reassignment, or rerun. Set `review_status=changes_requested` and move status to `todo` or `in_progress`.
- A plain Monica comment that says `不足`, `不能合并`, `not ready`, `request changes`, or lists blocking feedback does not start work unless it also wakes the next owner.

## In Progress Flow

`in_progress` means the current assignee owns execution or is waiting for CI/external validation they started.

- Active task exists: do not disturb.
- No active task and no valid wait reason: rerun current assignee or reassign to the correct owner.
- Waiting for CI: do not ping reviewer while pending. If CI fails, route back to fixer or CI/DevOps. If CI passes, move to review gate.

## Blocked Flow

`blocked` must be classified:

- Agent-actionable blocker: CI/test failure, code defect, review feedback, missing repo binding, stale CodeGraph/Headroom setup, log/resource lookup, or creating/reading a GitHub issue. Move to `todo` or `in_progress`, add an unblock plan, and assign/rerun the owner.
- Human-decision blocker: credentials, paid/budget choice, external account settings, release policy, sensitive/private boundary, local DCC/UI-only operation, high-risk merge/rollback, or product/business ambiguity. Keep `blocked`, mention hallong once, and set `human_decision_required=true`, `waiting_on=hallong`, `decision_owner_member_id=c73ed444-85e7-407f-ac76-cc71fd7c2648`, `pipeline_phase=human_decision`.
- External wait blocker: keep `blocked`, set `blocked_reason`, `waiting_on`, and `next_check_at`. Patrol re-checks after `next_check_at`.

## Safe Mutation Order

1. Add a compact comment with evidence and at most one live mention.
2. Update metadata.
3. Update status.
4. Assign/reassign or rerun as the final mutation.

## Dedicated Recovery Automation

Autopilot: `Monica 状态机回收：审核中/进行中/阻塞中`

ID: `c402be53-ab90-4bd9-bc82-bf633b1c6f8b`

Schedule: every 15 minutes from 09:00 to 23:59 Asia/Shanghai.

It scans `in_review`, `in_progress`, and `blocked`, then repairs stale gates, missing wakeups, lost active runs, and unresolved blockers.
