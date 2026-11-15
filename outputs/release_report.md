# Release Readiness Report

**Target build:** release `R2.4`, commit `b7a91f2`, prompt `p7`, environment `production`, service `atlas-assistant`

## Overall decision: **BLOCKED**

## Requirement-by-requirement decisions

### REQ-12 — [REQ-12] Streaming completion integrity
**Decision:** FAIL

QA verification (REC-5957) reported a failure: 49 of 50 cases passed, with one case producing a duplicated final message.

**Evidence referenced:**
- `REC-5957` — Failed streaming verification run

### REQ-05 — [REQ-05] Model usage cost
**Decision:** PASS

Although the measured cost (REC-5716) of USD 0.018 exceeds the threshold, an explicit waiver was issued by release management (REC-5716, comment 3).

**Evidence referenced:**
- `REC-5716` — Contains both the failure and the official waiver

### REQ-07 — [REQ-07] Sensitive log redaction
**Decision:** PASS

Initial failure (REC-9745) was superseded by a successful retest on the target build (REC-9745, comment 2).

**Evidence referenced:**
- `REC-9745` — Successful retest on target build

### REQ-02 — [REQ-02] Customer document isolation
**Decision:** FAIL

Isolation retest (REC-1001) confirmed a cross-customer document exposure, which blocks the release.

**Evidence referenced:**
- `REC-1001` — Confirmed isolation failure

### REQ-03 — [REQ-03] Answer quality for deployed prompt
**Decision:** INSUFFICIENT_EVIDENCE

No QA evaluation matches the full target configuration. REC-4323 reports 96.2 percent on release R2.4, commit b7a91f2, service atlas-assistant, and production, but its EVAL-55 harness was configured with prompt p6. REC-1600 uses prompt p7 but is for commit 9c3e1aa in staging. Neither establishes the required 90 percent threshold for the deployed p7 prompt on the target production build.

**Evidence referenced:**
- `REC-4323` — 96.2% on target release/commit/service/production, but evaluation used prompt p6
- `REC-1600` — Prompt p7 evaluation, but wrong commit (9c3e1aa) and staging environment

### REQ-11 — [REQ-11] Accessible keyboard handoff
**Decision:** PASS

QA verification REC-8801 confirms keyboard navigation passes on the production environment.

**Evidence referenced:**
- `REC-8801` — Successful QA verification

### REQ-09 — [REQ-09] Rollback rehearsal
**Decision:** INSUFFICIENT_EVIDENCE

Two QA Lead records report contradictory rollback rehearsals for the exact target release R2.4, commit b7a91f2, service atlas-assistant, and production. REC-6011 reports success with no data loss; REC-6198 reports failure with 214 session records lost. Neither record explicitly supersedes the other or resolves the conflict, so the manifest requires INSUFFICIENT_EVIDENCE.

**Evidence referenced:**
- `REC-6011` — RB-401 reports successful restore and no data loss at 11:00
- `REC-6198` — RB-402 reports failure and 214 lost session records at 11:30

### REQ-01 — [REQ-01] Automated regression tests
**Decision:** PASS

QA confirmed (REC-2081) that 146 of 146 mandatory cases passed on the target build.

**Evidence referenced:**
- `REC-2081` — Successful regression run

### REQ-06 — [REQ-06] Production security approval
**Decision:** INSUFFICIENT_EVIDENCE

Security Owner Asha Rao approved for staging (REC-2828), but explicitly stated that production sign-off is pending.

**Evidence referenced:**
- `REC-2828` — Staging approval only; production pending

### REQ-10 — [REQ-10] Document parsing completeness
**Decision:** PASS

Initial failure was superseded by a successful retest on the target build (REC-9611, comment 2).

**Evidence referenced:**
- `REC-9611` — Successful retest on target build

### REQ-04 — [REQ-04] Response latency
**Decision:** PASS

Production run REC-6024 confirmed p95 latency of 1.40 seconds, well within the 2.0s limit.

**Evidence referenced:**
- `REC-6024` — Successful production performance run

### REQ-08 — [REQ-08] Human handoff delivery
**Decision:** FAIL

Verification (REC-9635) failed: 0 of 12 escalations reached the queue due to a confirmed open defect.

**Evidence referenced:**
- `REC-9635` — Failed handoff delivery verification

## Reviewer correction notes

Post-run reviewer correction: REQ-03 and REQ-09 were revised after reviewing the linked Jira records. This 98-call Gemini run fetched REC-6198, but still misclassified both requirements. The report decisions below are reviewer corrections, not the model output.
