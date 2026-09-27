# Complete-delivery allocation boundary

## Outcome

For newly started tasks, Pi gets one substantial opportunity to implement and verify a complete, independently reviewable outcome before the Codex main task reviews it. Pi may run and repair tests within that opportunity. If the reviewed delivery has a substantive quality defect, the existing Codex main task takes implementation ownership of that outcome, reassesses the whole result, and implements the coherent repair itself. This is an ownership change, not acceptance of the Pi candidate.

The default quality-failure limit for a new task is **1**. At dispatch, a narrowly scoped task with a known local repair path may explicitly choose **2**. The choice is pinned with the task; a later contract revision, phase rename, pause, resume, or retry cannot increase it or reset a failed delivery. Acceptance before the limit resets the count for the next outcome. Once takeover is reached, the same Pi task remains closed; a later outcome needs a separate dispatch. Existing tasks without the pinned choice retain their former limit of **3**, so upgrading the plugin cannot retroactively seize a running worker.

## What counts

- Count one exact, main-reviewed `review_required` or `phase_blocked` event per round when its decision is `changes_requested` or `reject` with `failure-kind quality`. A complete candidate with failed or missing real behavior/checks is a quality failure even if its summary or check coverage appears green.
- Do not count Pi's internal red tests, routine implementation repairs, progress echoes, duplicate events for one round, or a genuine external prerequisite recorded with the existing `failure-kind external` and an unlock condition.
- Keep the existing single automatic same-session continuation for a *mechanical missing receipt* before main review, within the original budget. It cannot cross an accepted phase, repair semantic defects under an evidence label, or reset the quality count. If that continuation remains incomplete and the main reviewer records a quality failure, the pinned limit applies.
- Only Codex may accept a phase after reviewing the exact candidate and original evidence. Neither Pi exit 0, readiness, nor queue delivery constitutes acceptance. Preserve the project's separate independent reviewer requirement.

## Takeover and safety

At the limit, persist `codex_takeover_required` for the exact outcome and refuse Pi `continue`, automatic continuation, and worker start for that outcome. Do not cancel an active or unknown worker automatically. Before writing, Codex verifies the Pi worker and descendants released the worktree, preserves commits, dirty files and evidence, then performs a bounded whole-outcome design and source audit. It implements and verifies directly in the existing desktop task. A user pause and any newer instruction take precedence. No new Codex model worker, heartbeat, MCP service, or change to the DeepSeek Flash pin or CLI queue transport is part of this change.

The board and each actionable compact event must show the pinned limit, counted failed deliveries, current implementation owner and reason. A fresh phase after an accepted pre-limit result may use the same pinned task; after takeover, a future independently authorized outcome is a separate task, never a renamed retry.

## Acceptance

Exercise real board decisions, task registration and `continue` against temporary Git repositories. Cover default one-failure takeover, explicit two-failure scope, unchanged three-failure legacy tasks, duplicate-round and contract-revision resistance, accepted-phase reset, external blocker, pause/resume, and pre-review missing-receipt auto-continuation. Run affected tests and the full Python suite, then validate the plugin. Do not mutate managed plugin caches. Defer installation while an active desktop task has a pinned hook path that would be removed by a cachebuster reinstall.
