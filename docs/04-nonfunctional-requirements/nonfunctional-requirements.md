# NorthStar Retail Multi-Agent AI Platform: Non-Functional Requirements

| Field | Value |
|---|---|
| Document | Non-Functional Requirements: Delayed Delivery Initial Release |
| Module / Step | Module 1, Step 1.4 |
| Status | Draft for stakeholder review |
| Source context | Business Discovery (BD), Stakeholders and Workflows (WF), Functional Requirements (FR) |
| Prepared by | Principal AI Solution Engineer |
| Last updated | 2026-11-08 |

> **How to read this document**
>
> NorthStar Retail is **fictional**. This document **introduces no decisions**. Every target, threshold, volume and price is labeled:
>
> - **[Proposal]**: a recommended target that needs stakeholder approval.
> - **[Assumption]**: an input believed reasonable but not verified.
> - **TBD**: no evidence yet. The value must be set from measurement, contracts or stakeholder input before sign-off.
>
> References to **[Decision Dn]** point to BD §12. Open proposals **PP-1 to PP-4** (FR §2.2) remain **not approved**.
>
> **Architecture-neutral.** Every requirement applies equally to the deterministic, single-agent and multi-agent options (D10, D17). Nothing here requires a fixed number of AI agents, models or components. AI-specific requirements (tokens, inference cost) apply only if an AI-based option is chosen.

---

## 1. Scope and Measurement Conventions

| Term | Definition | Label |
|---|---|---|
| Service-hours calendar | **08:00–20:00** local time in NorthStar's primary customer service time zone (time zone TBD), **every day** including weekends and public holidays: 12 h/day | **[Assumption]**; actual CS calendar TBD (CS Operations Manager) |
| Service hours per month (H) | 12 h × days in the calendar month: **336 h** (28 days), **360 h** (30 days), **372 h** (31 days). Capacity and cost scenarios use a 30-day month, H = 360 h | Derived from the calendar |
| Planned maintenance | Scheduled outside service hours. Maintenance during service hours counts as downtime | **[Proposal]** |
| Investigation run | One execution of the platform from `investigation.requested` to `recommendation.generated` or `recommendation.insufficient_evidence` (WF §11). Re-runs (FR-REC-05) count as separate runs | **[Proposal]** |
| Platform processing time | Duration of one run, measured from those two audit event timestamps. Excludes human review and approval time | **[Proposal]** |
| End-to-end investigation time | The primary KPI: case opened to validated outcome recorded, per BD §8.1 | **[Decision D6, D13]** |
| Measurement window | Calendar month unless stated otherwise | **[Proposal]** |
| Systems of record (SoR) | OMS, Payment, Inventory and Shipping/Carrier APIs | **[Decision D4]** |

Dependency outages outside the platform's control are reported separately and are not counted against platform availability (NFR-AVL-02), but the platform's behavior during them is tested (Section 4).

---

## 2. Capacity Assumptions

All volumes are **[Assumption]**, chosen to bracket a plausible range. Actual case volume is unknown (BD Q1); replace these with measured data before sizing.

| Input | Symbol | S1 Low | S2 Medium | S3 High | Label |
|---|---|---|---|---|---|
| Delayed delivery investigations per month | N | 10,000 | 50,000 | 150,000 | **[Assumption]** |
| Re-run rate | r | 10% | 10% | 10% | **[Assumption]** |
| Runs per month, N × (1 + r) | R | 11,000 | 55,000 | 165,000 | Derived |
| Average runs per service hour, R / H | | 30.6 | 152.8 | 458.3 | Derived |
| Peak-to-average factor | k | 3 | 3 | 3 | **[Assumption]** |
| Peak runs per hour | | 91.7 | 458.3 | 1,375 | Derived |
| Peak runs per second (λ) | λ | 0.025 | 0.127 | 0.382 | Derived |
| Concurrent CS agent users | | TBD | TBD | TBD | TBD (staffing data needed) |

**Downstream calls per run [Assumption]:** case system 1 (only under PP-1), OMS 1, carrier 1.3 (average shipments per order), payment 1, inventory 1, Snowflake 2. Total ≈ **7.3 calls per run**.

| Peak downstream load | S1 | S2 | S3 |
|---|---|---|---|
| All calls per second (λ × 7.3) | 0.19 | 0.93 | 2.79 |
| Carrier calls per second (λ × 1.3) | 0.03 | 0.17 | 0.50 |
| Snowflake queries per second (λ × 2) | 0.05 | 0.25 | 0.76 |

Provider rate limits for each SoR are TBD (BD Q6). Each must exceed these peaks with headroom (NFR-RTY-01).

---

## 3. Availability, Reliability and Resilience

| ID | Requirement | Target | Label | Measurement | Test |
|---|---|---|---|---|---|
| NFR-AVL-01 | Platform availability during service hours | **99.5%** of service hours per calendar month. Error budget = 0.5% × H: **1.8 h** in a 30-day month (1.68 h for 28 days, 1.86 h for 31 days) | **[Proposal]** | Share of 1-minute windows within the service-hours calendar (Section 1) where synthetic investigations succeed and the platform's own error rate is below 5% | NT-01 |
| NFR-AVL-02 | Dependency availability reported separately | SoR and Snowflake availability, and the share of runs with any "unknown" status, reported monthly | **[Proposal]** | Per-dependency success metrics; `source.unavailable` events | NT-01 |
| NFR-AVL-03 | Recovery time objective (platform) | ≤ **4 h** | **[Proposal]** | Timed recovery drill | NT-02 |
| NFR-AVL-04 | Recovery point objective | **0** loss of committed audit events; ≤ **15 min** loss of in-flight investigation state | **[Proposal]** | Restore drill compares audit store before and after | NT-02 |
| NFR-REL-01 | Run completion | ≥ **99%** of runs produce a valid result (recommendation or "insufficient evidence"), excluding user cancellations | **[Proposal]** | Requested vs completed run events | NT-03 |
| NFR-REL-02 | Audit fail-closed | If an audit event cannot be persisted, the result is **not shown**. 0 results shown without their audit events | **[Proposal]** (supports gate G7, FR-AUD-01) | Fault injection on the audit store | NT-04 |
| NFR-REL-03 | Correctness under failure | **0** runs in which an unavailable, stale or partial source is shown as verified | **[Proposal]** (supports gate G4, D15) | Fault-injection suite with labeled expected outputs | NT-05 |
| NFR-REL-04 | Safe re-runs | Re-running an investigation creates a new result and never alters earlier results or audit records | **[Proposal]** (FR-REC-05) | Re-run test comparing stored results | NT-03 |
| NFR-RES-01 | Dependency isolation | Failure or slowness of one dependency does not block lookups to the others (separate timeouts and concurrency limits per dependency) | **[Proposal]** | Fault injection: one slow dependency, others measured | NT-05 |
| NFR-RES-02 | Circuit breaking | Per dependency: opens after ≥ 50% failures over the last 20 calls or 5 consecutive failures; half-opens after 30 s | **[Proposal]**; thresholds to be tuned | Fault injection; breaker state metrics | NT-06 |

---

## 4. Graceful Degradation

**Principle [Decision D15, D12]:** degradation reduces what the platform can conclude; it never reduces evidence standards. A degraded run reports unknowns or "insufficient evidence"; it never substitutes Snowflake or cached data for current status.

| Dependency unavailable | Platform behavior | User sees | Label |
|---|---|---|---|
| Carrier API | Tracking status "unknown"; continue with other sources | Shipping listed as unchecked | **[Decision D15]**; FR-EVD-03 |
| OMS | Order and shipments cannot be identified; no further lookups that depend on them | "Insufficient evidence: order system unavailable" | **[Proposal]** |
| Payment or Inventory API | That domain is "unknown"; no claim rules it out | Domain listed as unchecked | **[Decision D12]**; FR-LKP-01 |
| Snowflake | Continue without historical context | "Historical context unavailable" | **[Proposal]**; FR-HIST-03 |
| Case management system (under PP-1) | CS agent enters case ID and order ID manually | Manual entry prompt | **Pending PP-1**; FR-CASE-02 |
| AI model provider (AI options only) | Show the collected evidence with provenance, without an AI-generated conclusion; CS agent investigates manually | "Automated analysis unavailable" | **[Proposal]** |
| Approval routing | Approvals cannot be recorded; no resolution shown as ready | "Awaiting approval" persists | **[Proposal]**; FR-APR-06 |
| Audit store | Fail closed: no results shown (NFR-REL-02) | "Platform unavailable" | **[Proposal]** |
| Identity or authorization service | Fail closed: no retrievals | "Sign-in or authorization unavailable" | **[Decision D16]** |

| ID | Requirement | Target | Label | Test |
|---|---|---|---|---|
| NFR-DEG-01 | Each row above behaves as stated under fault injection | **100%** of degradation scenarios pass | **[Proposal]** | NT-05 |
| NFR-DEG-02 | Degraded runs are visible in reporting | Share of runs with at least one unknown domain, per dependency, reported monthly | **[Proposal]** | NT-01 |

---

## 5. Latency

Percentiles are computed over all runs (or requests) in the measurement window, from trace and audit timestamps. Targets apply to every architecture option.

| ID | Metric | p50 | p95 | p99 | Label | Test |
|---|---|---|---|---|---|---|
| NFR-LAT-01 | Platform processing time per run (Section 1) | ≤ **30 s** | ≤ **90 s** | ≤ **180 s** | **[Proposal]**; calibrate after evaluation | NT-07 |
| NFR-LAT-02 | Interactive actions (open result, review, approve, view audit trail) | ≤ **300 ms** | ≤ **1 s** | ≤ **3 s** | **[Proposal]** | NT-07 |
| NFR-LAT-03 | Audit event persisted after the action | TBD | TBD | ≤ **5 s** | **[Proposal]** for p99 | NT-04 |
| NFR-LAT-04 | Per-dependency lookup latency (each SoR, Snowflake, case system) | TBD | TBD | TBD | TBD: depends on provider SLAs (BD Q6); measured and reported | NT-07 |

**Timeout budgets [Proposal]:** 5 s per attempt and 15 s total per dependency including retries, whichever comes first. A dependency that exceeds its budget is treated as unavailable (FR-EVD-03). Budgets are to be tuned once NFR-LAT-04 is measured.

---

## 6. End-to-End Investigation Time

The definition, baseline and target come from BD and are preserved:

| Item | Value | Label |
|---|---|---|
| Definition | Case opened to validated outcome recorded, including review and approval wait (BD §8.1) | **[Decision D6, D13]** |
| Baseline | 30 minutes | **[Assumption, D7]** |
| Target | 10 minutes (−66.7%) | **[Proposal, D8]**; counts only if gates G1–G9 are met (**[Decision D14]**) |
| Which statistic the target applies to | Median, P90 or both | **[Open]** (BD Q16) |
| Escalated cases | Clock keeps running through escalation | **Pending PP-2** (WF §7.1); interim safeguards apply |
| Baseline comparison | Comparable stop event (elements 1, 3, 4, 5) | **Pending PP-3** (WF §4.1) |

| ID | Requirement | Target | Label | Test |
|---|---|---|---|---|
| NFR-E2E-01 | End-to-end investigation time, p50 | ≤ **10 min** | **[Proposal, D8]**; statistic pending BD Q16 | NT-08 |
| NFR-E2E-02 | End-to-end time, P90 | Reported; target TBD | TBD (BD §8.1 asks for P90 reporting) | NT-08 |
| NFR-E2E-03 | End-to-end time, p95 and p99 | Reported; targets TBD | TBD | NT-08 |
| NFR-E2E-04 | Approval wait time | Reported separately at p50, p95 and p99 | **[Proposal]** (BD §8.1 note) | NT-08 |
| NFR-E2E-05 | Platform share of end-to-end time | Platform processing p95 (NFR-LAT-01) ≤ **15%** of the 10-minute target, leaving time for human review and approval | **[Proposal]** | NT-08 |

**Measurement [Proposal]:** start and stop timestamps from the case record (or CS agent confirmation if PP-1 is rejected), state durations from audit events (FR-KPI-01). Report all cases and the escalated cohort separately (FR-KPI-03).

---

## 7. Scalability and Concurrency

Required concurrency uses Little's law: concurrent runs = λ × platform processing time. Using the p95 target of 90 s, with **2× headroom [Proposal]**:

| | S1 | S2 | S3 |
|---|---|---|---|
| λ × 90 s | 2.3 | 11.5 | 34.4 |
| Design concurrency (× 2, rounded up) | **5** | **25** | **70** |

| ID | Requirement | Target | Label | Test |
|---|---|---|---|---|
| NFR-SCL-01 | Sustain peak load | Meet NFR-LAT-01 at the design concurrency of the chosen scenario for 1 hour | **[Proposal]**; scenario choice TBD after BD Q1 | NT-09 |
| NFR-SCL-02 | Surge tolerance | At **1.5×** peak load for 15 minutes: no lost requests, no unsafe results; latency may degrade | **[Proposal]** | NT-09 |
| NFR-SCL-03 | Backpressure | When concurrency limits are reached, new runs are queued with a visible status, not dropped or failed silently | **[Proposal]** | NT-09 |
| NFR-SCL-04 | Horizontal scaling | Capacity increases by adding instances, with no change to requirements or results | **[Proposal]** | NT-09 |
| NFR-SCL-05 | Concurrent CS agent sessions | TBD | TBD (staffing data needed) | NT-09 |

---

## 8. Rate Limiting, Retries and Backoff

All SoR calls are reads (D11), so retries cannot cause duplicate actions. Retries must still not overload providers or exceed timeout budgets.

| ID | Requirement | Target | Label | Test |
|---|---|---|---|---|
| NFR-RTY-01 | Client-side rate limit per dependency | ≤ **80%** of each provider's published limit; provider limits TBD (BD Q6) | **[Proposal]** | NT-06 |
| NFR-RTY-02 | Retry only transient failures | Retry timeouts, HTTP 429 and 5xx. Never retry other 4xx, authorization failures or malformed responses | **[Proposal]** | NT-06 |
| NFR-RTY-03 | Retry count | At most **2** retries (3 attempts) per call, within the 15 s dependency budget | **[Proposal]** | NT-06 |
| NFR-RTY-04 | Backoff | Exponential with full jitter: base **200 ms**, cap **2 s**; honor `Retry-After` when present, within the budget | **[Proposal]** | NT-06 |
| NFR-RTY-05 | No retry storms | Retries stop while a dependency's circuit is open (NFR-RES-02) | **[Proposal]** | NT-06 |
| NFR-RTY-06 | Per-user request limit | ≤ **10** investigation runs per user per minute | **[Proposal]** | NT-06 |
| NFR-RTY-07 | AI provider limits (AI options only) | Requests are queued within the provider's token and request limits; limits TBD by provider and contract | TBD | NT-09 |

---

## 9. Security

| ID | Requirement | Target | Label | Test |
|---|---|---|---|---|
| NFR-SEC-01 | Authorization coverage | **100%** of retrievals checked against user and service permissions; **0** violations (gate G6) | **[Decision D16]**; target **[Proposal]** | NT-10 |
| NFR-SEC-02 | Read-only enforcement | **0** write-capable credentials for SoRs (and for the case system under PP-1) | **[Decision D11]**; target **[Proposal]**; case-system scope **Pending PP-1** | NT-10 |
| NFR-SEC-03 | Encryption | TLS 1.2 or later in transit; encryption at rest for evidence, results and audit data | **[Proposal]** | NT-10 |
| NFR-SEC-04 | Credentials | Short-lived service tokens (≤ **1 h**); secrets in a secrets manager; rotation ≤ **90 days** | **[Proposal]** | NT-10 |
| NFR-SEC-05 | Vulnerability remediation | Critical ≤ **7 days**; high ≤ **30 days** | **[Proposal]** | NT-10 |
| NFR-SEC-06 | Prompt-injection resistance (AI options only) | **100%** pass on the agreed injection test set (FR-EVD-07) | **[Proposal]**; test set TBD | NT-10 |
| NFR-SEC-07 | Independent security test | Penetration test passed with no open critical or high findings before production | **[Proposal]** | NT-10 |
| NFR-SEC-08 | Access review | Service scopes and approver roles reviewed at least quarterly | **[Proposal]** | NT-10 |

---

## 10. Privacy

| ID | Requirement | Target | Label | Test |
|---|---|---|---|---|
| NFR-PRV-01 | No cardholder data | **0** occurrences in results, logs, traces, audit events or model inputs | **[Proposal]** (BD §9.3 PCI assumption) | NT-11 |
| NFR-PRV-02 | PII minimization | Only fields on the approved field lists (FR-EVD-08); PII masked in logs and traces | **[Proposal]** | NT-11 |
| NFR-PRV-03 | Data sent to model providers (AI options only) | Only the minimum fields needed; provider terms must exclude training on NorthStar data | **[Proposal]**; contract terms TBD | NT-11 |
| NFR-PRV-04 | Retention | Audit, evidence and log retention periods | TBD (BD Q20) | NT-11 |
| NFR-PRV-05 | Data residency | Processing and storage locations for inference, logs and audit | TBD (BD Q12) | NT-11 |

---

## 11. Observability

| ID | Requirement | Target | Label | Test |
|---|---|---|---|---|
| NFR-OBS-01 | Correlation | **100%** of logs, traces and audit events for a run share a correlation ID | **[Proposal]** | NT-12 |
| NFR-OBS-02 | Tracing | **100%** of dependency calls traced, with latency, outcome and retry count | **[Proposal]** | NT-12 |
| NFR-OBS-03 | Metrics | Per dependency: rate, errors, latency percentiles, breaker state. Per run: outcome, platform processing time, unknown domains. AI options: tokens and cost per run | **[Proposal]** | NT-12 |
| NFR-OBS-04 | SLO burn-rate alerting | Alert when ≥ **2%** of the monthly error budget is consumed in 1 hour, or ≥ **10%** in 6 hours | **[Proposal]** | NT-12 |
| NFR-OBS-05 | Detection time | Full platform outage detected within **5 min** | **[Proposal]** | NT-12 |
| NFR-OBS-06 | Gate and KPI dashboards | Gates G1–G9 and KPI percentiles computable from telemetry (FR-KPI-04) | **[Proposal]** | NT-12 |
| NFR-OBS-07 | Cost attribution | Every cost item tagged by environment, architecture option and cost category (Section 12) | **[Proposal]** | NT-13 |

---

## 12. AI Token Usage and FinOps

### 12.1 Cost Categories

Costs are tracked in three separate categories, so the architecture options can be compared fairly (BD §8.5):

| Category | Includes | Applies to |
|---|---|---|
| **Inference** | Model input, output and any cached-token charges | AI-based options only; **0 for the deterministic option** |
| **Infrastructure** | Compute, storage (evidence, results, audit), networking, observability, secrets management | All options |
| **Snowflake** | Warehouse credits for historical-context queries, plus storage and cloud services if attributable | All options that use FR-HIST-01 |

### 12.2 Token Budgets (AI Options Only)

| ID | Requirement | Target | Label | Test |
|---|---|---|---|---|
| NFR-FIN-01 | Expected tokens per run | **20,000** input, **2,000** output | **[Assumption]**; replace with evaluation measurements | NT-13 |
| NFR-FIN-02 | Hard cap per run | **40,000** input, **4,000** output | **[Proposal]** | NT-14 |
| NFR-FIN-03 | Behavior at the cap | Stop further model calls; show verified evidence collected so far and return "insufficient evidence" for unresolved items. **Never** return unsupported claims (D12). Record a cap-reached event in the audit trail | **[Proposal]** (new audit event, to be added to WF §11) | NT-14 |
| NFR-FIN-04 | Token reporting | Tokens per run at p50, p95 and p99, by architecture option | **[Proposal]** | NT-13 |

The token budget applies to a whole run, however many model calls or agents a design uses. It does not imply any number of agents.

### 12.3 Cost Formulas

| Formula | Meaning |
|---|---|
| R = N × (1 + r) | Runs per month |
| C_inf = R × (T_in × P_in + T_out × P_out + T_cache × P_cache) / 1,000,000 | Monthly inference cost. T = tokens per run; P = price per million tokens |
| C_sf = Credits × P_credit (+ attributable storage and cloud services) | Monthly Snowflake cost |
| C_infra = C_compute + C_storage + C_network + C_observability + C_secrets | Monthly infrastructure cost |
| C_total = C_inf + C_infra + C_sf | Total monthly platform cost |
| Cost per investigation = C_total / N | Unit cost (BD §8.3) |

### 12.4 Monthly Cost Scenarios

**Prices are placeholders [Assumption]:** P_in = **$3.00** and P_out = **$15.00** per million tokens, used only to show the arithmetic. They are not a quote for any provider or model. Replace them with contracted prices for the model(s) chosen. P_cache = 0 in these examples. P_credit and all infrastructure unit prices are **TBD**.

**Inference (AI options only):**

| | S1 | S2 | S3 |
|---|---|---|---|
| Runs per month (R) | 11,000 | 55,000 | 165,000 |
| Input tokens per month at expected use | 220 M | 1,100 M | 3,300 M |
| Output tokens per month at expected use | 22 M | 110 M | 330 M |
| **Inference at expected use** ($0.09 per run) | **$990** | **$4,950** | **$14,850** |
| **Inference at hard cap** ($0.18 per run) | **$1,980** | **$9,900** | **$29,700** |
| Deterministic option | $0 | $0 | $0 |

**How the S1 figure is calculated:** 10,000 investigations × 1.1 runs (10% re-run rate) = 11,000 runs. Input: 11,000 × 20,000 = 220 M tokens × $3.00/M = **$660**. Output: 11,000 × 2,000 = 22 M tokens × $15.00/M = **$330**. Total **$990**. The only overhead above one run per investigation is the 10% re-run rate; without re-runs, S1 would be $900. This is why cost per investigation ($0.099) is 10% higher than cost per run ($0.09). T_in and T_out are assumed to include all prompt, tool-result and retried-call tokens in a run **[Assumption]**; no other overhead is added.

**Snowflake [Assumption]:** credits assume an X-Small warehouse consumes 1 credit per running hour and a Small warehouse 2 (verify with the Data Platform Owner). Credits are an upper bound with the warehouse running for all 360 service hours; auto-suspend may lower them.

| | S1 | S2 | S3 |
|---|---|---|---|
| Queries per month (R × 2) | 22,000 | 110,000 | 330,000 |
| Warehouse | X-Small | X-Small | Small |
| Credits per month (upper bound) | ≤ 360 | ≤ 360 | ≤ 720 |
| **Snowflake cost** | ≤ 360 × P_credit (TBD) | ≤ 360 × P_credit (TBD) | ≤ 720 × P_credit (TBD) |

**Infrastructure [Assumption]:** volumes assume about 30 audit events of 1 KB and about 50 KB of evidence and results per run.

| | S1 | S2 | S3 |
|---|---|---|---|
| Design concurrency (Section 7) | 5 | 25 | 70 |
| Audit data per month | 0.33 GB | 1.65 GB | 4.95 GB |
| Evidence and results per month | 0.55 GB | 2.75 GB | 8.25 GB |
| **Infrastructure cost** | TBD | TBD | TBD |

**Total:** C_total and cost per investigation remain **TBD** until infrastructure and Snowflake unit prices are known. Inference alone at expected use is about **$0.099 per investigation** (C_inf / N) in every scenario, under the placeholder prices.

### 12.5 Budget Guardrails

| ID | Guardrail | Target | Label | Test |
|---|---|---|---|---|
| NFR-FIN-05 | Monthly budget | Set per category by the Executive Sponsor | TBD | NT-15 |
| NFR-FIN-06 | Budget alerts | Alerts at **50%**, **80%** and **100%** of the monthly budget, on both actual and forecast spend | **[Proposal]** | NT-15 |
| NFR-FIN-07 | Spend anomaly | Alert when daily spend in any category is > **2×** its trailing 7-day average | **[Proposal]** | NT-15 |
| NFR-FIN-08 | Budget exhausted | New AI runs pause and CS agents investigate manually until the budget owner approves more spend. Evidence checks and safety controls are never reduced to save cost | **[Proposal]** | NT-15 |
| NFR-FIN-09 | Unit cost tracking | Cost per investigation reported monthly by category and architecture option, compared with human handling cost (TBD) | **[Proposal]** | NT-13 |

---

## 13. Acceptance Tests

| ID | Test | Method | Pass criteria | Covers |
|---|---|---|---|---|
| NT-01 | Availability and degradation reporting | Synthetic investigations every minute during service hours for a full month (pilot) | NFR-AVL-01 met; dependency and degraded-run reports produced | NFR-AVL-01, NFR-AVL-02, NFR-DEG-02 |
| NT-02 | Disaster recovery drill | Restore the platform and audit store in a non-production environment | RTO and RPO met; audit store matches pre-failure state | NFR-AVL-03, NFR-AVL-04 |
| NT-03 | Run completion and re-run safety | Evaluation set of ≥ 500 runs including re-runs | ≥ 99% valid results; earlier results unchanged | NFR-REL-01, NFR-REL-04 |
| NT-04 | Audit fail-closed | Make the audit store unavailable or slow during runs | No result shown without audit; p99 persist time met when healthy | NFR-REL-02, NFR-LAT-03 |
| NT-05 | Fault injection | Inject outage, timeout, slowness, stale, partial and malformed responses for each dependency | Every Section 4 row behaves as specified; 0 unknowns shown as verified; healthy dependencies unaffected | NFR-REL-03, NFR-RES-01, NFR-DEG-01 |
| NT-06 | Retry, backoff and rate limits | Mock dependencies returning 429 (with and without `Retry-After`), 5xx, 4xx and timeouts | Retry counts, backoff, budgets and rate limits as specified; no retries while a circuit is open | NFR-RES-02, NFR-RTY-01 to 06 |
| NT-07 | Latency | Measure from traces during the evaluation and a 1-hour load test at design concurrency | p50, p95 and p99 targets met | NFR-LAT-01, 02, 04 |
| NT-08 | End-to-end time | Pilot cases, case-record timestamps and audit events | NFR-E2E-01 met with gates G1–G9 met; P90, p95, p99 and approval wait reported; escalated cohort reported | NFR-E2E-01 to 05 |
| NT-09 | Load and surge | Load test at peak for 1 hour, then 1.5× peak for 15 minutes; scale out during the test | Latency met at peak; no lost requests or unsafe results at surge; queued runs visible | NFR-SCL-01 to 05, NFR-RTY-07 |
| NT-10 | Security | Credential and scope review, TLS scan, vulnerability scan, injection test set, penetration test | All NFR-SEC targets met | NFR-SEC-01 to 08 |
| NT-11 | Privacy | Automated scan of results, logs, traces, audit and model inputs; review of provider terms | 0 cardholder data; only approved fields; retention and residency confirmed once set | NFR-PRV-01 to 05 |
| NT-12 | Observability | Trace a sample of runs end to end; simulate an outage and a budget burn | 100% correlation and tracing; alerts and detection time met; gates computable | NFR-OBS-01 to 06 |
| NT-13 | Cost and token measurement | Run the same evaluation set for each architecture option | Tokens and cost per run reported by option and category; tags complete | NFR-FIN-01, 04, 09, NFR-OBS-07 |
| NT-14 | Token cap | Evaluation cases designed to exceed the cap | Run stops at the cap; no unsupported claims; cap event recorded | NFR-FIN-02, NFR-FIN-03 |
| NT-15 | Budget guardrails | Simulated spend crossing 50%, 80%, 100% and a 2× daily spike | Alerts fire; AI runs pause at 100%; safety controls unchanged | NFR-FIN-05 to 08 |

---

## 14. Open Items and TBDs

| Item | Needed for | Owner (from WF §2) |
|---|---|---|
| Actual monthly case volume and peak pattern (BD Q1) | Section 2 scenario choice, NFR-SCL-01 | CS Operations Manager |
| CS service hours and concurrent CS agent counts | H, NFR-SCL-05 | CS Operations Manager |
| SoR and carrier API SLAs and rate limits (BD Q6) | NFR-LAT-04, NFR-RTY-01, timeout budgets | Domain Data Owners |
| Statistic for the 10-minute target (BD Q16) | NFR-E2E-01 to 03 | Executive Sponsor |
| PP-2 and PP-3 approval | NFR-E2E measurement | Stakeholders (requires BD §8.1 amendment) |
| PP-1 approval | Case-system load, degradation row, NFR-SEC-02 scope | Case-system owner, IS, PC |
| Model pricing and provider limits (AI options) | Section 12, NFR-RTY-07 | Solution Engineer, procurement |
| Snowflake credit price and warehouse sizing | Section 12 | Data Platform Owner |
| Infrastructure unit prices | Section 12 | Engineering |
| Monthly budget per category | NFR-FIN-05 | Executive Sponsor |
| Retention and data residency (BD Q12, Q20) | NFR-PRV-04, 05 | Privacy / Compliance |
| Human handling cost per investigation | NFR-FIN-09 | CS Operations Manager |
| Prompt-injection test set | NFR-SEC-06 | Information Security, Solution Engineer |
