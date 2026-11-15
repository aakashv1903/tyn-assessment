# Release Readiness Report

**Target build:** release `R2.4`, commit `b7a91f2`, prompt `p7`, environment `production`, service `atlas-assistant`

## Overall decision: **BLOCKED**

## Requirement-by-requirement decisions

### TYN-400 — [REQ-12] Streaming completion integrity
**Decision:** FAIL

QA verification (REC-5957) found a defect where streaming case STREAM-50 produced a duplicated final message.

**Evidence referenced:**
- `REC-5957` — Defect confirmed in QA run for target release and commit

### TYN-352 — [REQ-05] Model usage cost
**Decision:** PASS

The cost criterion (exceeding USD 0.012) was formally waived by release management for this release.

**Evidence referenced:**
- `REC-5716` — Requirement waived by release management per comment

### TYN-329 — [REQ-07] Sensitive log redaction
**Decision:** PASS

The redaction retest (REC-9745) explicitly superseded a failed test and confirmed 28 of 28 cases pass on the target release.

**Evidence referenced:**
- `REC-9745` — Redaction retest confirmed 28 of 28 cases pass

### TYN-263 — [REQ-02] Customer document isolation
**Decision:** FAIL

Isolation retest (REC-1001) failed on the target release due to a cross-customer document exposure, and the defect was reopened.

**Evidence referenced:**
- `REC-1001` — Retest failed due to cross-customer exposure; defect remains open

### TYN-225 — [REQ-03] Answer quality for deployed prompt
**Decision:** PASS

The evaluation achieved 96.2 percent accuracy, meeting the 90 percent requirement for the target release.

**Evidence referenced:**
- `REC-4323` — Evaluation passed with 96.2 percent accuracy

### TYN-162 — [REQ-11] Accessible keyboard handoff
**Decision:** INSUFFICIENT_EVIDENCE

The provided QA evidence (REC-8801) is for release R2.3, not the target release R2.4.

**Evidence referenced:**
- `REC-8801` — Evidence is for release R2.3; no R2.4 data found

### TYN-143 — [REQ-09] Rollback rehearsal
**Decision:** PASS

Rollback rehearsal (REC-6011) succeeded on the target release and production environment.

**Evidence referenced:**
- `REC-6011` — Rehearsal successful on target release

### TYN-89 — [REQ-01] Automated regression tests
**Decision:** PASS

Regression run (REC-2081) confirmed 146 of 146 mandatory regression cases pass on the target release.

**Evidence referenced:**
- `REC-2081` — All regression cases passed on target release

### TYN-73 — [REQ-06] Production security approval
**Decision:** FAIL

Security Owner Asha Rao approved for staging only, not production as required by the assessment rules.

**Evidence referenced:**
- `REC-2828` — Approval explicitly limited to staging

### TYN-57 — [REQ-10] Document parsing completeness
**Decision:** PASS

Parsing retest (REC-9611) confirmed 60 of 60 cases pass on the target release.

**Evidence referenced:**
- `REC-9611` — Parsing retest confirmed all cases passed

### TYN-53 — [REQ-04] Response latency
**Decision:** PASS

Performance run (REC-6024) confirmed a p95 latency of 1.40 seconds, well within the 2.0-second limit.

**Evidence referenced:**
- `REC-6024` — Performance metrics confirm latency is under 2.0s

### TYN-48 — [REQ-08] Human handoff delivery
**Decision:** FAIL

Queue adapter verification (REC-9635) found that 0 of 12 escalated conversations reached the support queue; the defect (HND-DEF-4) is open.

**Evidence referenced:**
- `REC-9635` — Queue adapter fails to deliver escalations; defect open

## Notes / known issues encountered during the run
Regression test record TYN-316 was listed as In Progress but contained the definitive passing result required.
