# ADR-003: Investigation State Storage

| Field | Value |
|---|---|
| Status | **Proposed** (not accepted; approval authority **[Open]**, BD Q15) |
| Date | 2026-11-08 |
| Applies to | All alternatives (shared control plane) |
| Related | [Technology Selection](../technology-selection.md), ADR-002, ADR-005 |

## 1. Context and Requirements

The platform must store investigation state, evidence with provenance, results with version metadata, reviews, approvals, escalations and audit events. Snowflake is **not** a candidate for this operational store: it is historical context only **[Decision D5]**.

| Requirement | Source |
|---|---|
| Investigation states and transitions, with integrity rules | FR-WFS-01; WF §10 |
| Evidence items with source, record ID, retrieval time and as-of time | D15; FR-EVD-01 |
| Results with version metadata; earlier results kept on re-run | FR-REC-04, FR-REC-05; NFR-REL-04 |
| Approvals with approver identity; SoD check results | D18; FR-APR-01, FR-APR-03 |
| Audit events with common fields, written fail-closed | D9; FR-AUD-01; NFR-REL-02 |
| Audit records tamper-evident, not editable by users | FR-AUD-03 **[Assumption]**, BD §9.3 |
| Reconstruct a case's audit trail in order | FR-AUD-04 |
| Compute KPI durations and gates G1–G9 | FR-KPI-01 to 04 |
| RPO: 0 committed audit events; ≤ 15 min in-flight state | NFR-AVL-04 **[Proposal]** |
| Volumes: about 0.33–4.95 GB audit and 0.55–8.25 GB evidence per month | NFR §12.4 **[Assumption]** |
| Encryption at rest; retention per policy | NFR-SEC-03; NFR-PRV-04 (retention TBD, BD Q20) |

## 2. Alternatives Considered

| Option | Strengths | Weaknesses |
|---|---|---|
| **Amazon Aurora PostgreSQL** | Transactions let a state change and its audit event commit together (fail-closed); constraints and foreign keys enforce integrity; SQL for KPI and gate reporting; Multi-AZ; point-in-time recovery | Capacity sizing and patching (reduced with managed or serverless capacity); relational schema changes need migrations |
| Amazon DynamoDB | Serverless scaling; low operational load | KPI and gate reporting need extra pipelines; cross-item integrity rules harder to enforce; volumes here do not need its scale |
| Step Functions execution history only | No extra store | Limited retention (verify); not queryable for reporting; not an audit store |
| Amazon S3 only (JSON objects) | Cheap, durable; Object Lock for immutability | No transactions or query-time integrity; poor fit for live state |
| Snowflake | Already present | Excluded: historical context only (D5), not an operational system |

**Audit tamper evidence options:** append-only table with insert-only grants; **S3 Object Lock (WORM) archive**; a ledger database service. Combined with the primary store, the archive gives retention that platform users and application roles cannot alter.

## 3. Proposed Decision and Rationale

**[Proposal]**

1. **Aurora PostgreSQL** (Multi-AZ, encrypted with KMS, point-in-time recovery) as the primary store for investigation state, evidence, results, reviews, approvals and escalations.
2. **Audit events in an append-only table** in the same database. Each state change and its audit event commit in **one transaction**, so a result cannot be shown without its audit record (NFR-REL-02). The application role has insert and select only on the audit table; no update or delete.
3. **Continuous export of audit events to S3 with Object Lock** for tamper-evident retention. Retention period **TBD** (BD Q20); Object Lock mode and duration set by Privacy/Compliance.
4. **Reporting** for KPI and gates from read replicas or exports. Whether to export platform data to Snowflake for analytics is **[Open]** and would need approval, because Snowflake must never become a source of current status for investigations.

**Rationale:** the platform's hardest storage requirements are integrity (allowed transitions, SoD, fail-closed audit) and queryability (KPI and gates), not scale. Volumes are small (NFR §12.4). A relational store with transactions meets these with the least custom code, and the S3 archive covers tamper evidence.

## 4. Consequences and Risks

| Consequence or risk | Mitigation |
|---|---|
| Database outage blocks all results (fail-closed) | Multi-AZ; RTO 4 h and RPO targets (NFR-AVL-03, 04); degradation row "Audit store" in NFR §4 |
| Capacity planning | Size from NFR §7 design concurrency; load test NT-09 |
| PII in evidence | Field allow-lists (FR-EVD-08); masking in logs; encryption at rest |
| Export pipeline lag to S3 | Monitor lag; primary audit table is authoritative until archived |
| Retention undecided | Configurable lifecycle; set once BD Q20 is answered |

## 5. Validation Requirements

| Validation | Pass criterion | Test |
|---|---|---|
| Fail-closed audit | No result shown if the audit insert fails | NT-04 |
| Immutability | Application and user roles cannot update or delete audit rows or archived objects | NT-11; FR-AUD-03 (TS-N25) |
| Recovery | RTO ≤ 4 h; 0 committed audit events lost | NT-02 |
| Integrity rules | Disallowed transitions and self-approval rejected at the data layer as well | TS-N14, TS-N17 to N19 |
| Reporting | KPI durations and gates G1–G9 computable | TS-P17; NT-12 |
| Load | Meets NFR-LAT-02 and NFR-LAT-03 at design concurrency | NT-07, NT-09 |

## 6. Status and Traceability

**Status:** Proposed. Acceptance requires the validation above and sign-off by the approval authority (BD Q15).

| Traces to | IDs |
|---|---|
| Decisions | D5, D9, D15, D18 |
| Functional | FR-WFS-01, FR-EVD-01, FR-EVD-08, FR-REC-04, FR-REC-05, FR-APR-01, FR-APR-03, FR-AUD-01 to 05, FR-KPI-01 to 04 |
| Non-functional | NFR-AVL-03, NFR-AVL-04, NFR-REL-02, NFR-REL-04, NFR-LAT-02, NFR-LAT-03, NFR-SEC-03, NFR-PRV-04 |
| Architecture | AA §2 |
| Open | BD Q15, BD Q20, export of platform data to Snowflake |
