# ClawSweeper Patterns For DCC MCP Monica

Source model: `openclaw/clawsweeper`, a conservative maintenance bot for OpenClaw repositories. Use these patterns when improving DCC MCP Monica automations for issue cleanup, PR repair, and auto-merge.

## Core Shape

ClawSweeper is not a generic stale bot. It separates judgment from mutation:

- Review lane: model-backed review/classification. Produces durable reports and comments; does not close or merge.
- Apply lane: deterministic live-state recheck and GitHub mutation. Owns comments, labels, closes, pushes, and merges.
- Repair lane: maintainer-command or trusted-review triggered fix/automerge loop.
- Audit lane: read-only health and state drift checks.

Map this to Monica:

- Review/classification can be done by agents and squads.
- Apply/merge/close must re-read live GitHub and Monica state immediately before mutating.
- Repair can use agents for code edits, but GitHub mutation gates must stay deterministic and explicit in prompts/metadata.
- Audit/gardener loops should detect stale reviews, stale issues, disabled triggers, payload bloat, and missing handoffs.

## Exact-Head Merge Rule

Every merge decision is bound to one PR head SHA.

- Record `metadata.current_head_sha`.
- Record `metadata.loonghao_reviewed_head_sha` only after loonghao review applies to that exact head.
- A push or force-push invalidates earlier approval/review markers.
- If `current_head_sha != loonghao_reviewed_head_sha`, route back to loonghao review after checks pass.
- Merge commands must use the current reviewed head and must re-read live PR state before merge.

## Checks: Wait, Repair, Or Merge

Do not treat all non-green check states the same.

- Pending/in-progress/no-checks-yet: wait state. Keep owner in progress or set `next_check_at`.
- Terminal required-check failure: repair state. Route to CI/DevOps/Release or implementer with failure evidence.
- Ignored non-gating automation checks: do not block unless GitHub branch protection blocks the merge.
- GitHub branch protection remains final authority at merge time.

## Bounded Repair Loop

Every repair/automerge path needs caps.

- Default max repairs per PR: 10.
- Default max repairs per PR head SHA: 2.
- If the cap is reached, set `metadata.repair_cap_reached=true`, status `blocked` or `in_review`, and escalate with exact evidence.
- One active repair/adopted job per PR. Do not enqueue duplicate repair workers for the same PR/head.
- After a repair push, immediately require exact-head re-review and green checks before merge.

## Closure Policy

Close or cancel only after a live recheck.

Allowed close/cancel classes:

- duplicate of a clear canonical item;
- superseded by a clear canonical item;
- implemented on current main with source evidence;
- fixed by a specific merged PR or candidate fix that owns validation;
- low-signal/unmergeable PR only when policy allows and contributor credit/reopen path are preserved.

Never close/cancel:

- active maintainer discussion;
- unique reproduction, platform, version, or user impact;
- assigned work in progress;
- useful contributor PR code that should be merged, repaired, or credited;
- security-sensitive items;
- anything whose target changed after review.

For Monica wrapper issues, cancellation is allowed when a compact canonical issue exists for the same GitHub URL/head/run and the wrapper contains only transport metadata.

## Structured Decision Metadata

Agent output should be converted into durable metadata before later automation acts:

- `job_intent`: `review`, `repair_pr`, `automerge_pr`, `implement_issue`, `issue_cleanup`, `docs_gardener`, `automation_health`.
- `decision`: `keep_open`, `route_repair`, `route_review`, `merge_ready`, `close_candidate`, `cancel_duplicate`, `needs_human`.
- `confidence`: `high`, `medium`, `low`.
- `target_updated_at`: live GitHub item `updatedAt` at review time.
- `current_head_sha`: current PR head SHA.
- `reviewed_head_sha`: head SHA covered by trusted review.
- `merge_preflight`: checks, review, metadata, mergeability, security, and unresolved-comments evidence.
- `repair_count_pr` and `repair_count_head`.
- `next_check_at`.

## Public Surface

ClawSweeper uses marker-backed GitHub comments edited in place. DCC MCP should map this to compact Monica canonical issues first, because internal routing is sensitive.

- GitHub public comments remain exceptional, concise, English, and public-safe.
- Monica issue metadata is the primary status surface.
- Do not copy Monica IDs, internal routing, private comments, local paths, or raw payloads to GitHub.

## Human Gate

Escalate instead of mutating when the item involves:

- security-sensitive evidence;
- credentials or account settings;
- paid/budget tradeoffs;
- product or release policy;
- ambiguous canonical choice;
- contributor-credit ambiguity;
- high-risk rollback/merge.
