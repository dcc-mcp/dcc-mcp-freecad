---
name: monica-github-autopilot-ops
description: "Operate existing Monica GitHub automations: event routing, deduplication, CI repair, and authorized review/release gates."
---

# Monica operations router

Read live state for the specific target before mutation. Preserve task authority, existing assignments, reviewer gates, runtime/profile and notification policy unless the user asks to change them.

## Always applicable boundaries
- Internal delivery, routing and ACK/NACK belong in Monica. Public GitHub/Gongfeng text contains technical, destination-appropriate material only, without internal IDs, private paths or secrets.
- Handoffs use at most one live mention. Check existing canonical issues and current PR head before routing; deduplicate unchanged ACK/NACK heads. A rerun on the same head is not a new review request unless explicitly marked ready.
- Required CI must be terminal-green on the current head before final ACK or merge. Pending/failing checks permit PRE-REVIEW ONLY and repair, never conditional approval. An old approved_head_sha does not authorize a changed head.
- Preserve configured release-value, milestone-blocker, human approval and exact-head gates. A passing check, found PR or this Skill is not independent authorization to merge or publish.
- run_only does not authorize new wrapper/tracking issues; mutate only existing authorized state unless creation was explicitly enabled. A no_action result follows the automation's existing notification policy.
- Skill edits affect new tasks. Read back saved content and Agent attachments; running-task behavior is a separate verification gate. Do not store credentials in instructions, comments or custom env.

## Load by task
Use [workflow details](workflow-details.md) as a reference; locate only the sections needed for this operation:
- Routine live maintenance: Discovery checklist; Event mapping; GitHub issue and PR association; Dynamic assignee and status flow; Recommended automation shape; CLI patterns.
- PR/review/CI: canonical issue association, review dedup, PR CI handoff, single-hop mention and GitHub boundaries.
- Release/deployment: milestone blockers, release-please value, exact-head finalization and deployment verification. Read all applicable gate sections before any release mutation.
- Automation scheduling: timer governor, event classification, bounded recovery and overlap checks; no new timers without a scheduling request.
- Provider failure: bounded retry using an already configured role-compatible fallback, without changing identity or final approval authority.

Additional domain rules and existing reference links remain in the detailed guide. Specific user/role approval gates are never optional. General templates and sample schedules apply only when the task actually needs them.
