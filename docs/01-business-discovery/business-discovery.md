# NorthStar Retail Multi-Agent AI Platform: Business Discovery

| Field | Value |
|---|---|
| Document | Business Discovery |
| Phase | 01: Business Discovery |
| Status | Draft for stakeholder review |
| Prepared by | Principal AI Solution Engineer |
| Last updated | 2026-11-08 |

> **Important: what is real and what is assumed**
>
> NorthStar Retail is a **fictional company** used for this solution engineering engagement. Unless a statement is marked **[Decision]**, treat company details, team structures, volumes, baseline metrics and targets as **[Assumption]** or **[Proposal]**. None of them are verified facts. Validate them with the stakeholders listed in Section 4 before using them for funding, staffing or architecture commitments.
>
> Labels used in this document:
> - **[Decision]**: agreed for this engagement and recorded in Section 12.
> - **[Assumption]**: believed to be true but not yet verified.
> - **[Proposal]**: a recommended target or design direction that still needs approval.
> - **[Open]**: an unresolved question, tracked in Section 11.

---

## 1. Executive Summary

NorthStar Retail's customer service teams manually investigate order, shipping, inventory and payment exceptions across disconnected enterprise systems **[Decision D3]**. To find the cause of one exception, an agent may need to check the Order Management System (OMS), payment APIs, inventory APIs and shipping or carrier APIs, then gather the evidence, decide on a resolution and get any required approvals **[Assumption]**. We expect this process to be slow, inconsistent between agents and hard to audit **[Assumption: to be validated in discovery]**.

This engagement proposes an investigation platform **[Proposal]** that:

- Collects evidence from the authoritative operational systems, within the requesting user's permissions.
- Adds historical context from Snowflake, without using it as current operational status.
- Produces evidence-supported recommendations, or reports "insufficient evidence", for a human to review.
- Keeps least-privilege access, a full audit trail and required human approvals in place.

**Initial vertical slice [Decision D2]:** delayed delivery investigations.

**The initial slice is read-only [Decision D11]:** the platform investigates and recommends. Authorized humans perform any refunds, reships or order modifications in the owning systems. Write-enabled automation would need separate approval and design.

**Primary KPI [Decision D6]:** end-to-end investigation time, from case opening to a recorded, validated investigation outcome with all required human approvals. The exact start and stop events are defined in Section 8.1 **[Decision D13]**. Carrying out the resolution and later fulfillment are excluded.

| Metric | Value | Status |
|---|---|---|
| Baseline investigation time | 30 minutes | **[Assumption]** |
| Target investigation time | 10 minutes | **[Proposal]** |
| Reduction | 66.7% ((30 − 10) / 30) | **[Proposal]** |

The 10-minute target counts as met only when the quality and safety gates in Section 8.2 are also met **[Decision D14]**.

**Architecture approach [Decision D10, D17]:** before choosing a production design, compare a deterministic workflow, a single-agent design and a multi-agent design on the same scenarios, safety gates, quality, latency, cost and operational complexity (Section 8.5). The platform is not committed to an AI-based or multi-agent design in advance, even though "multi-agent" is in the project name.

---

## 2. Business Context and Problem Statement

### 2.1 Business Context

All statements in this subsection are **[Assumption]** unless labeled otherwise.

- NorthStar Retail is a fictional omnichannel retailer that sells online and fulfills orders through its own distribution centers and third-party carriers.
- Customers contact customer service through several channels about late deliveries, missing items, failed payments and stock problems.
- Operational data is spread across separate systems that different teams own. There is no single investigation workspace.
- Snowflake is the enterprise analytics platform and holds historical order, shipment, inventory and payment data **[Assumption]**. It provides historical analytics and investigation context and is not authoritative for current operational status **[Decision D5]**.

### 2.2 Problem Statement [Decision]

> Customer service teams manually investigate order, shipping, inventory, and payment exceptions across disconnected enterprise systems.

### 2.3 Consequences of the Problem [Assumption]

- **Slow resolution:** customers wait while agents switch between systems.
- **Inconsistent outcomes:** two agents may resolve the same kind of exception differently.
- **Weak evidence trail:** the reasons for a decision are not always recorded in a form that can be audited.
- **High handling cost:** experienced agents spend time collecting data instead of making decisions.
- **Escalation load:** cases that are incomplete or unclear go to specialist teams.

---

## 3. In-Scope and Out-of-Scope Processes

### 3.1 In Scope

| Area | Description | Phase |
|---|---|---|
| Delayed delivery investigations | Find out why a shipment is late, using order, inventory, payment and carrier evidence, and recommend a resolution | **Initial vertical slice, read-only [Decision D2, D11]** |
| Order exceptions | Order holds, failed order state transitions, partial fulfillment | In business scope **[Decision D1]**; phasing after the first slice **[Proposal]** |
| Shipping exceptions | Carrier delays, lost or damaged in transit, address issues | In business scope **[Decision D1]**; phasing after the first slice **[Proposal]** |
| Inventory exceptions | Stock mismatches, backorders, allocation failures | In business scope **[Decision D1]**; phasing after the first slice **[Proposal]** |
| Payment exceptions | Authorization failures, capture mismatches, refund status checks | In business scope **[Decision D1]**; phasing after the first slice **[Proposal]** |
| Evidence assembly and recommendation | Gather and summarize the evidence; propose the next best action, or report "insufficient evidence" and escalate | All phases |
| Human approval workflow | Send recommendations that need approval to authorized staff | All phases |
| Audit logging | Record the evidence, recommendation, approver and outcome for each investigation | All phases |

The example exception types in the descriptions above are illustrative **[Assumption]**.

### 3.2 Out of Scope

| Area | Rationale |
|---|---|
| Any platform execution of refunds, reshipments, compensation or order modifications in the initial slice | The initial slice is read-only. Authorized humans perform these actions in the owning systems. Write-enabled automation needs separate approval and design **[Decision D11]** |
| Changes to OMS, payment, inventory or carrier system logic | These systems stay authoritative and unchanged; the platform only reads from them **[Decision D4, D11]** |
| Using Snowflake as the source of current operational status | Snowflake is for historical and contextual data only **[Decision D5]** |
| Customer-facing chatbot or self-service | Internal use by customer service first **[Proposal]** |
| Fraud adjudication and chargeback disputes | Specialist domain with separate controls **[Proposal]** |
| Warehouse operations, carrier contract management, demand forecasting | Not exception investigation processes **[Proposal]** |
| Production deployment and CI/CD setup **during the discovery phase** | Excluded from the discovery phase only. Production deployment of the selected design may be part of later project phases **[Decision D19]** |

---

## 4. Stakeholders and Responsibilities

All roles below are **[Assumption]** for the fictional organization. Confirm the actual names and owners during discovery.

| Stakeholder | Responsibilities in this engagement |
|---|---|
| VP Customer Experience (Executive Sponsor) | Owns business outcomes and KPI targets; approves scope and funding |
| Customer Service Operations Manager | Owns the current investigation process, agent workflows and baseline measurement |
| Customer Service Agents / Team Leads | Daily users; validate recommendations; provide workflow feedback |
| Order Management (OMS) Product Owner | Data owner for order status; approves OMS read access |
| Payments Product Owner | Data owner for transactions; approves payment API scope and PCI boundaries |
| Inventory / Supply Chain Product Owner | Data owner for stock availability; approves inventory API access |
| Logistics / Carrier Integration Owner | Data owner for shipment tracking; approves carrier API usage and rate limits |
| Data Platform Owner (Snowflake) | Owns historical datasets, data quality and access policies |
| Information Security | Approves least-privilege access model, secrets handling and threat model |
| Privacy / Compliance / Legal | Approves PII handling, retention and regulatory obligations |
| Enterprise Architecture | Reviews architecture options and integration patterns |
| Principal AI Solution Engineer | Leads discovery, architecture evaluation, solution design and documentation |
| AI / ML Engineering | Builds and evaluates the investigation logic, prompts, tools and evaluation harness |
| Platform / SRE | Owns runtime, observability, reliability and incident response |

---

## 5. Current-State Investigation Workflow

**[Assumption]** This is a typical delayed delivery investigation today. Confirm it through process walkthroughs and agent shadowing (see Section 11). The KPI start and stop events follow the definitions in Section 8.1 **[Decision D13]**.

| Step | Activity | System(s) | Notes | KPI boundary |
|---|---|---|---|---|
| 1 | Customer contacts support about a late order; agent opens a case | Case management tool **[Assumption]** | | **▶ KPI START:** case-opened timestamp |
| 2 | Agent looks up the order, line items, promised delivery date and fulfillment status | OMS | Manual search by order ID or customer | Inside |
| 3 | Agent checks that payment was authorized or captured and that no payment hold is blocking fulfillment | Payment APIs / payment console | May need a separate login or a specialist team | Inside |
| 4 | Agent checks stock and allocation for the items | Inventory APIs / inventory console | Backorders or allocation failures may explain the delay | Inside |
| 5 | Agent checks shipment tracking events and carrier exceptions | Shipping / carrier portals or APIs | Sometimes several carriers per order | Inside |
| 6 | Agent looks for similar past issues or patterns (for example, a regional carrier delay) | Ad hoc reports, Snowflake dashboards, tribal knowledge | Inconsistent across agents | Inside |
| 7 | Agent decides on the cause (or that evidence is insufficient) and a resolution (wait, reship, refund, escalate) | Agent judgment, policy documents | Applied inconsistently | Inside (review) |
| 8 | Agent asks for approval when policy requires it (for example, refund above a threshold) | Team lead / supervisor | Approval wait adds time | Inside (approval wait included) |
| 9 | Agent records the outcome: cause, evidence, resolution and any approvals | Case management tool | Evidence often summarized loosely | **■ KPI STOP:** validated outcome and all required approvals recorded |
| 10 | Authorized staff carry out the resolution in the owning system; fulfillment proceeds; case is closed | OMS, payment, carrier systems, case management tool | | **Outside the KPI** |

**Assumed baseline: 30 minutes per investigation [Assumption].** For this document, the 30 minutes is taken to cover steps 1–9 (KPI start to KPI stop), including approval wait time and excluding step 10. This boundary has not been checked against any existing handle-time data. The baseline measurement (Section 8.1) must use the same start and stop events. If the measured figure differs materially, revisit the business case (see risk R1).

---

## 6. Business Pain Points

All pain points are **[Assumption]**. Validate them through interviews and data.

1. **Swivel-chair investigation:** agents move between four or more systems, each with its own UI, login and data model.
2. **Repeated manual lookups:** the same order, payment, inventory and tracking checks happen by hand on every case.
3. **Inconsistent root-cause analysis:** results depend on agent experience and tribal knowledge.
4. **Limited use of historical context:** past patterns in Snowflake are not used consistently during an investigation.
5. **Approval delays:** escalations and approvals are handled informally and add wait time.
6. **Weak auditability:** evidence and reasoning are not captured in a structured, reviewable form.
7. **Access sprawl:** agents may hold broad system access to get through investigations, which goes against least-privilege principles.
8. **Onboarding burden:** new agents need a long time to learn the investigation process across systems.

---

## 7. Systems of Record and Data Ownership

### 7.1 Operational Systems of Record [Decision]

| Domain | System of record | Data used for investigations | Authoritative for current status? | Data owner **[Assumption]** |
|---|---|---|---|---|
| Orders | OMS | Order header, lines, status, promised dates, fulfillment events | **Yes** | OMS Product Owner |
| Payments | Payment APIs | Authorization, capture, refund status, payment holds | **Yes** | Payments Product Owner |
| Inventory | Inventory APIs | Stock on hand, allocation, backorder status | **Yes** | Inventory / Supply Chain Product Owner |
| Shipping | Shipping / Carrier APIs | Tracking events, carrier exceptions, estimated delivery | **Yes** | Logistics / Carrier Integration Owner |

### 7.2 Analytical Context [Decision]

| Domain | System | Role | Authoritative for current status? |
|---|---|---|---|
| Historical analytics | Snowflake | Historical patterns, prior similar cases, carrier/region delay trends, investigation context | **No.** Never use it to state current operational status |

### 7.3 Data Principles

- **Current status comes from operational systems of record only [Decision D4, D5].** If Snowflake and an operational system disagree, the operational system wins, and the disagreement is recorded as an observation **[Proposal]**.
- **An unavailable source means unknown status [Decision D15].** If an authoritative operational API is unavailable, times out or returns incomplete data, the affected current status is reported as "unknown" or "unverified". Snowflake data and stale cached data must not be substituted for current operational truth. Historical data may still be shown, clearly labeled as historical context.
- **Provenance and freshness for every factual claim [Decision D15].** Each factual claim in a recommendation is traceable to its source system, record identifier and retrieval timestamp, plus the source's own as-of timestamp where one is available.
- **Read-only initial slice [Decision D11].** The platform only reads from systems of record. Authorized humans perform refunds, reships and order modifications in the owning systems. Write-enabled automation needs separate approval and design.
- **Requesting-user permissions [Decision D16].** Every retrieval is authorized against the requesting user's own permissions, in addition to the least-privilege scope of the platform service or tool. The platform must not return data that the requesting user could not access directly.
- **Data minimization [Proposal].** Retrieve only the fields needed for the investigation. Payment card data and unnecessary PII are excluded.

---

## 8. Proposed Business Outcomes and Measurable KPIs

### 8.1 Primary KPI

| KPI | Definition | Baseline | Target | Status |
|---|---|---|---|---|
| End-to-end investigation time | Elapsed time from the KPI start event to the KPI stop event defined below | 30 min | 10 min (−66.7%) | Definition **[Decision D6, D13]**; baseline **[Assumption]**; target **[Proposal]**, conditional on Section 8.2 **[Decision D14]** |

**KPI events [Decision D13]:**

| Event | Definition |
|---|---|
| Start | The timestamp at which the delayed delivery case is opened in the case management system. |
| Stop | The first timestamp at which the case record contains every element of a validated investigation outcome (below), including all human approvals that policy requires. |

**Validated investigation outcome [Decision D13]:** a case record that contains all of the following:
1. The determined cause of the delay, or a statement of "insufficient evidence" together with the escalation taken.
2. The supporting evidence, with source system, record identifier, retrieval time and freshness for each factual claim.
3. The recommended resolution (for example wait, reship, refund or escalate).
4. Review and acceptance, or amendment, by an authorized human customer service agent.
5. Every human approval that policy requires for that resolution, recorded with approver identity and time.

- **Included in the measured time:** evidence gathering, analysis, human review, approval wait time and recording the outcome.
- **Excluded:** carrying out the approved resolution in the owning systems, later fulfillment and delivery, and any activity after the stop event.

**Measurement notes [Proposal]:**
- Measure from case-system timestamps for the start and stop events, not from self-reported time.
- Report the median and the 90th percentile, not only the mean.
- Measure the baseline on a representative sample of delayed delivery cases, using the same start and stop events, before the pilot.
- Also track approval wait time separately, so that platform performance is not confused with approval queue delays.

**Target condition [Decision D14]:** the 10-minute target counts as achieved only when every gate in Section 8.2 is met over the same measurement period.

### 8.2 Quality and Safety Gates

The 10-minute target is met only if every gate below is also met **[Decision D14]**. Thresholds marked **[Proposal]** are not yet approved. Gates are checked on the pre-production evaluation set and monitored during the pilot. Meeting a gate in evaluation does not mean production errors are impossible; any production occurrence is logged and reviewed as a defect.

| Gate | Measure | Threshold | Status |
|---|---|---|---|
| G1 Unsupported factual claims | Factual claims presented as verified without supporting evidence | 0 in the evaluation set | **[Decision D12]** |
| G2 Insufficient-evidence handling | Share of evaluation scenarios with missing or conflicting evidence where the output reports "insufficient evidence" and escalates | 100% | **[Proposal]** |
| G3 Evidence provenance | Share of factual claims recorded with source system, record identifier, retrieval time and freshness | 100% | **[Proposal]** |
| G4 Source-of-truth compliance | Cases where current status comes from Snowflake or stale cache, or is shown as verified while the authoritative API was unavailable | 0 | **[Decision D5, D15]** |
| G5 Approval compliance | Share of approval-required resolutions with the required approval recorded before the stop event, by a separate person where policy requires it | 100% | **[Decision D9, D18]** |
| G6 Authorization | Retrievals that return data outside the requesting user's permissions | 0 | **[Decision D16]** |
| G7 Audit completeness | Share of investigations with a complete audit record, as defined in Section 9.1 | 100% | **[Proposal]** |
| G8 Root-cause accuracy | Agreement with expert-reviewed outcomes on the evaluation set | Not lower than the measured human baseline; exact threshold to be set | **[Proposal]** |
| G9 Rework | Share of cases reopened because the investigation was wrong or incomplete | No increase over the measured baseline | **[Proposal]** |

### 8.3 Supporting KPIs [Proposal]

These are monitored but are not gates. None of their targets have been approved; set them after the baseline is measured.

| KPI | Purpose | Proposed direction |
|---|---|---|
| Recommendation acceptance rate | Share of recommendations that agents accept without material change | Increase; target to be set after baseline |
| Approval wait time | Time between approval request and recorded approval | Tracked separately from platform time |
| Escalation rate | Share of cases escalated to specialist teams | Decrease |
| Agent satisfaction | Agent-reported usefulness and trust | Increase |
| Cost per investigation | Platform runtime cost plus agent time | Measured and compared across architecture options |

### 8.4 Business Outcomes [Proposal]

- Faster, more consistent resolution of delayed delivery cases.
- A structured, auditable evidence record for every investigation.
- Less need for agents to hold broad access to many systems.
- A reusable foundation that can extend to order, inventory and payment exceptions.

### 8.5 Architecture Evaluation Requirements [Decision D10, D17]

The three candidate approaches are compared fairly before the production design is selected:

- **Candidates:** a deterministic workflow, a single-agent design and a multi-agent design.
- **Same scenarios:** every candidate runs the same evaluation set of delayed delivery cases, including insufficient-evidence, conflicting-data and source-unavailable scenarios.
- **Same safety gates:** every candidate must pass gates G1–G7 and apply the same read-only, permission and audit controls (Section 9). A candidate that fails a safety gate is not eligible, regardless of speed or cost.
- **Compared dimensions:** quality (G8, G9 and recommendation acceptance), end-to-end investigation time, latency, cost per investigation and operational complexity (for example number of components, observability, failure modes and support effort).
- **Same conditions:** comparable data access, tooling and measurement method for all candidates.
- **Selection [Proposal]:** prefer the simplest candidate that passes the gates and meets the time target. Scoring weights and final approval authority are open (Section 11).

---

## 9. Security, Compliance, and Approval Boundaries

### 9.1 Principles [Decision]

- **Evidence-supported recommendations [Decision D9, D12]:** every factual claim cites its source evidence. Unsupported factual claims are never presented as verified. When evidence is insufficient, the platform reports "insufficient evidence" and escalates through the agreed escalation path **[Open: path to be defined]**.
- **Least-privilege access [Decision D9, D16]:** each component or tool gets only the minimum read scopes it needs, and only for the domains it handles. In addition, every retrieval is checked against the requesting user's own permissions.
- **Audit trails [Decision D9]:** record inputs, retrieval calls, retrieved evidence with provenance and freshness, recommendations, reviewer and approver identity, decisions and timestamps.
- **Version metadata [Decision D18]:** for each recommendation, record the version of the logic that produced it. For AI-based components this means the model identifier and version, prompt or template version, configuration version and tool versions. For deterministic components it means the rule set and configuration version.
- **Required human approvals [Decision D9, D11]:** humans approve, where policy requires, and perform every customer-impacting or financial action in the owning systems.
- **Separation of duties [Decision D18]:** where policy requires it, the approver must be a different person from the agent who requested the action. The platform is never an approver.

### 9.2 Action and Approval Boundaries

The read-only rule is **[Decision D11]**. Approval rules per action are **[Proposal]** unless labeled otherwise.

| Action | Platform may | Requires human approval |
|---|---|---|
| Retrieve order, payment, inventory or tracking data (read-only, within the requesting user's permissions) | Yes | No |
| Query Snowflake for historical context (not current status) | Yes | No |
| Summarize evidence and propose a root cause, or report "insufficient evidence" | Yes | No (the human agent reviews and validates the outcome) |
| Recommend a resolution (wait, reship, refund, escalate) | Yes | Agent review required |
| Issue a refund, credit or compensation | No. An authorized human performs it in the owning system | **Yes**, with thresholds by role **[Open]** |
| Create a reshipment or modify an order | No. An authorized human performs it in the owning system | **Yes** |
| Contact the carrier or open a carrier claim | No. A human performs it | **Yes** **[Open]** |
| Send a message to the customer | No. The customer service agent sends it | **Yes** (agent sends) **[Assumption]** |

### 9.3 Compliance Considerations [Assumption]

- **PCI DSS:** keep cardholder data out of scope. Use only tokenized or masked payment references.
- **Privacy regulations (for example GDPR or CCPA, depending on jurisdiction):** minimize PII, follow purpose limitation, set retention rules and support data subject requests.
- **Data residency and model hosting:** confirm where model inference and logs are processed and stored.
- **Audit log retention:** set retention periods with Compliance and make logs tamper-evident.
- **Model risk:** document evaluation results, known limitations and human-oversight controls before production.

### 9.4 Security Controls to Design [Proposal]

- Service identities scoped per component or tool, with short-lived credentials.
- Authorization of every retrieval against the requesting user's permissions **[Decision D16]**.
- No write credentials to systems of record in the initial slice **[Decision D11]**.
- Secrets kept in an enterprise secrets manager and never placed in prompts, configuration files or logs.
- If an AI-based design is selected: prompt-injection safeguards for any free-text content retrieved from systems (for example, carrier notes or customer messages).
- Output validation before any recommendation is shown, including a check that every factual claim has cited evidence.
- Rate limiting and circuit breakers on operational APIs. When a source is unavailable, the affected status is reported as unknown **[Decision D15]**.

---

## 10. Assumptions and Risks

### 10.1 Key Assumptions

| ID | Assumption |
|---|---|
| A1 | NorthStar Retail and its organizational details are fictional and illustrative. |
| A2 | Current investigation time is about 30 minutes per case between the KPI start and stop events in Section 8.1 (not yet measured). |
| A3 | A 10-minute target (−66.7%) is achievable for delayed delivery cases (proposed, not validated). |
| A4 | OMS, payment, inventory and carrier APIs expose read endpoints suitable for programmatic access. |
| A5 | Snowflake contains enough historical data to provide useful investigation context. |
| A6 | The case management system can reliably timestamp both the KPI start event and the KPI stop event. |
| A7 | Delayed delivery cases are a large enough share of exception volume to justify the first slice. |
| A8 | Customer service agents will remain the decision-makers and validate all outcomes. |

### 10.2 Key Risks

| ID | Risk | Impact | Mitigation [Proposal] |
|---|---|---|---|
| R1 | Baseline time differs significantly from the 30-minute assumption | Target and business case may be wrong | Measure the baseline before committing targets |
| R2 | Operational APIs are missing, rate-limited or inconsistent | Incomplete evidence; slower investigations; more "unknown" statuses | Early API discovery; report affected status as unknown or unverified; never substitute Snowflake or stale cache (D15) |
| R3 | Snowflake data is used as if it were current status | Wrong recommendations | Enforce the source-of-truth rule in design and evaluation (gate G4) |
| R4 | Recommendations contain unsupported or hallucinated claims | Loss of trust; customer harm | Evidence citation requirement; "insufficient evidence" output; gate G1; human validation; production defect review |
| R5 | Over-privileged access for the platform | Security exposure; users seeing data they are not entitled to | Least-privilege scopes; requesting-user permission checks (gate G6); no write credentials; security review; access audits |
| R6 | Approval wait time dominates the KPI | Target missed for reasons outside the platform | Track approval time separately; review approval policy with ops |
| R7 | A multi-agent design adds latency, cost or complexity without benefit | Higher run cost; harder operations | Fair comparison of all three options before committing (Section 8.5) |
| R8 | Low agent adoption or trust | Benefits not realized | Involve agents early; explainable evidence; feedback loop |
| R9 | PII or payment data exposure in prompts or logs | Compliance breach | Data minimization; masking; log redaction; compliance review |
| R10 | Prompt injection through free-text system data | Manipulated recommendations | Input handling safeguards; output validation; no autonomous actions |

---

## 11. Open Discovery Questions

### Business and Operations
1. What is the actual case volume per month by exception type (order, shipping, inventory, payment)?
2. What is the measured current investigation time (median and P90) for delayed delivery cases?
3. What resolution policies and thresholds apply to delayed deliveries (wait, reship, refund, credit)?
4. Which actions need approval, at what thresholds, and from which roles?
5. How are outcomes and approvals recorded in the case system today, and can it capture the KPI stop event defined in Section 8.1?

### Systems and Data
6. Which OMS, payment, inventory and carrier APIs are available? What are their auth models, rate limits and SLAs?
7. How many carriers are in scope, and do they provide standardized tracking events?
8. Which Snowflake datasets are available? What is their freshness, quality and governance status?
9. Which case management system is used, and can it be integrated for case context and timestamps?

### Security and Compliance
10. Which jurisdictions and regulations apply to customer and payment data?
11. What are the requirements for audit log retention and tamper evidence?
12. Are there restrictions on model hosting location or third-party model providers?

### Architecture Evaluation
13. Which specific scenarios, scoring weights and pass thresholds will the comparison in Section 8.5 use?
14. What latency and cost-per-investigation limits apply?
15. Who has final approval authority for the production architecture decision?

### KPI and Controls
16. Does the 10-minute target apply to the median, the 90th percentile or both?
17. Is the customer update part of the validated outcome, or an activity after the KPI stop event?
18. What escalation path applies when evidence is insufficient, and to which teams?
19. Which policies require separation of duties, and for which actions?
20. How long must version metadata and audit records be kept?

---

## 12. Decisions Recorded

The original agreement dates were not captured. These decisions were recorded in this document on 2026-11-08; that is not necessarily the date they were agreed.

| ID | Decision | Status |
|---|---|---|
| D1 | Business scope covers order, shipping, inventory and payment exceptions. | Agreed |
| D2 | The initial vertical slice is delayed delivery investigations. | Agreed |
| D3 | Business problem: customer service teams manually investigate exceptions across disconnected enterprise systems. | Agreed |
| D4 | Operational systems of record: OMS (orders), Payment APIs (transactions), Inventory APIs (stock availability), Shipping/Carrier APIs (tracking). | Agreed |
| D5 | Snowflake provides historical analytics and investigation context and is **not** authoritative for current operational status. | Agreed |
| D6 | Primary KPI is end-to-end investigation time, from case opening through validated investigation outcome and required human approvals. | Agreed |
| D7 | The assumed baseline is 30 minutes per investigation (**assumption, to be measured**). | Agreed as assumption |
| D8 | The proposed target is 10 minutes, a 66.7% reduction (**proposal, to be validated**). | Agreed as proposal |
| D9 | Quality and safety requirements: evidence-supported recommendations, least-privilege access, audit trails and required human approvals. | Agreed |
| D10 | Deterministic, single-agent and multi-agent approaches will be compared before the production design is chosen. | Agreed |
| D11 | The initial delayed delivery slice is read-only. The platform investigates and recommends; authorized humans perform any refunds, reships or order modifications in the owning systems. Future write-enabled automation requires separate approval and design. | Agreed |
| D12 | Unsupported factual claims are never presented as verified. When evidence is insufficient, the platform reports "insufficient evidence" and escalates. The evaluation target for unsupported factual claims is zero; this does not imply that production errors are impossible. | Agreed |
| D13 | KPI start and stop events and the definition of a validated investigation outcome are as stated in Section 8.1. Required review, approval and outcome recording are included; later fulfillment is excluded. | Agreed |
| D14 | The 10-minute target counts as met only when the quality and safety gates in Section 8.2 are met. | Agreed (unapproved gate thresholds remain proposals) |
| D15 | When an authoritative operational API is unavailable, current status is unknown or unverified. Snowflake and stale cached data are not substituted for current operational truth. Freshness and provenance are recorded. | Agreed |
| D16 | Each retrieval enforces the requesting user's permissions, in addition to service and tool least privilege. | Agreed |
| D17 | Deterministic, single-agent and multi-agent approaches are compared fairly on the same scenarios, safety gates, quality, latency, cost and operational complexity. | Agreed |
| D18 | Recommendations record model, prompt and configuration version metadata (or rule set version for deterministic logic). Separation of duties applies where policy requires it. | Agreed |
| D19 | Production deployment is excluded from the discovery phase, not necessarily from the overall project. | Agreed |

---

*End of document. Next phase: architecture option evaluation (deterministic vs. single-agent vs. multi-agent) for the delayed delivery vertical slice.*
