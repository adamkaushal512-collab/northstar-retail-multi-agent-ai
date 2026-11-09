# ADR-002: Workflow and Agent Orchestration

| Field | Value |
|---|---|
| Status | **Proposed** (not accepted; approval authority **[Open]**, BD Q15) |
| Date | 2026-11-08 |
| Applies to | Case workflow (all alternatives); analysis orchestration (A, B and C) |
| Related | [Technology Selection](../technology-selection.md), ADR-001, ADR-003, ADR-005 |

## 1. Context and Requirements

Two different orchestration problems exist:

1. **Case workflow**: long-running, human-paced states with review, approval and escalation waits (WF §10). Must be deterministic, auditable and identical for all alternatives.
2. **Analysis orchestration**: the short, machine-paced investigation run that gathers evidence and drafts a result. For A, a fixed lookup sequence and rules; for B, one model-driven loop; for C, a coordinator and a variable number of specialists.

| Requirement | Source |
|---|---|
| States and allowed transitions enforced, including no escalated → outcome-recorded jump if PP-2 is approved | FR-WFS-01; FR-ESC-04; WF §10 |
| Human review and approval steps; platform never approves; SoD | D9, D18; FR-REV-01, FR-REV-02, FR-APR-01 to 06 |
| Every lookup goes through the shared data access layer with user identity | D16; AA §2 |
| Output validator runs before any result is shown | D12; FR-EVD-02 |
| Version metadata for rules, prompts, models and orchestration config | D18; FR-REC-04 |
| Per-run token cap applies to the whole run, however many agents | NFR-FIN-02, NFR-FIN-03 |
| Platform processing latency and concurrency targets | NFR-LAT-01; NFR-SCL-01 to 03 |
| No fixed number of agents | D10, D17; AA §3.3 |

## 2. Alternatives Considered

### 2.1 Case Workflow

| Option | Strengths | Weaknesses |
|---|---|---|
| **AWS Step Functions (Standard workflows)** | Managed, durable state machine; human steps via task-token callbacks; execution history for diagnosis; native AWS integration | Execution history retention is limited (verify), so it cannot be the audit store; state machine definitions need their own versioning |
| Temporal (self-hosted or cloud) | Strong durable-execution model in code | Additional platform to run or a new external service; more operational load |
| State machine in application code over the database | Full control; no extra service | Must build timers, retries and durability; more custom code to test |
| Amazon SQS and EventBridge choreography | Loose coupling | Transitions spread across services; harder to enforce and audit the allowed-transition rules |
| LangGraph interrupts for human steps | One framework for AI and human steps | Would put review and approval inside the AI orchestration layer, blurring the boundary between machine analysis and human decisions (D9, D18); not durable across days without extra infrastructure |

### 2.2 Analysis Orchestration

| Option | Strengths | Weaknesses |
|---|---|---|
| **LangGraph (Python)** for B and C | Explicit, inspectable graph of steps; built-in state handling and checkpointing; supports single-agent loops and coordinator–specialist graphs with a configurable number of nodes; planned for hands-on implementation | Framework dependency and upgrade churn; must prove every tool call goes through the data access layer, is audited and counts toward the token cap; checkpoint storage must meet ADR-003 security rules |
| **Async Python** (asyncio, no framework) for A | Deterministic, simple, fast; parallel lookups; no AI or framework dependency; a fair baseline | Applies to A only; the team owns all control flow |
| Hand-written model loop on the Anthropic SDK for B and C | Fewest dependencies; full control | Re-implements state, branching and checkpointing that LangGraph provides; less suited to C |
| Anthropic Managed Agents | Anthropic runs the loop and hosts a sandbox | Not available on Bedrock; loop and execution hosted outside NorthStar's AWS accounts; conflicts with the structural-safety design (AA §2) for this release |
| Amazon Bedrock Agents | AWS-managed agent orchestration | Less control over the loop; same proof burden on tool routing, audit and caps (to evaluate) |

### 2.3 Roles of Each Technology

| Technology | Role | Not used for |
|---|---|---|
| **AWS Step Functions** | Case workflow states, transitions and **human callbacks** (review, approval, escalation findings) | AI reasoning; audit storage |
| **LangGraph** | **Candidate for stateful AI orchestration** in B (single agent) and C (coordinator and specialists) | Human review or approval; authorization; writing to any system |
| **Async Python** | **Deterministic baseline** A: parallel lookups and the versioned rules engine | Model calls |
| **Anthropic SDK** | **Model API access** (to Claude through Bedrock, ADR-001) from inside LangGraph nodes | Orchestration: it does **not** replace LangGraph |

## 3. Proposed Decision and Rationale

**[Proposal]**, every production choice pending validation (Section 5):

1. **Case workflow: AWS Step Functions Standard workflows**, one execution per investigation.
   - States and allowed transitions follow WF §10. Human steps (review, approval, escalation findings) use **task-token callbacks**, completed by the investigation service only after the human action is authenticated and authorized (ADR-004).
   - Approval is recorded only from an authenticated human action; no state accepts an approval from a service identity.
   - Every transition also writes an audit event to the database in the same operation (ADR-003), because Step Functions history is not the audit record.
2. **Analysis orchestration** behind one internal interface used by all alternatives (`analyze(evidence handle, tools) → draft result citing evidence IDs`):
   - **A: async Python.** A fixed set of lookups through the data access layer (run concurrently where independent), then a versioned rules engine. This is the deterministic baseline.
   - **B: LangGraph single-agent graph.** A model node and a tool node in a loop. The model node calls Claude through the **Anthropic SDK** (ADR-001). Tool nodes are thin wrappers over the data access layer.
   - **C: LangGraph coordinator–specialist graph.** A coordinator node routes to a **configurable** number of specialist subgraphs (possibly in parallel) and merges their drafts. The specialist count is a benchmark parameter, starting with the fewest that cover the needed domains (AA §9). No fixed agent count.
   - All three return their draft to the same deterministic **output validator**.
3. **LangGraph is the planned hands-on implementation** for the B and C benchmark candidates. Its use in production is **Proposed** and depends on passing the validations in Section 5 and the AA §8 benchmark.
4. **Boundaries LangGraph must keep:**
   - LangGraph runs are bounded and machine-paced. They contain **no human review or approval steps**; those stay in Step Functions.
   - Graph nodes have **no credentials** and no network path to systems of record; only data-access-layer tools are registered. No built-in or third-party tools with their own network access.
   - The **token cap** (NFR-FIN-02) is enforced across the whole graph run, including all specialists.
   - LangGraph checkpoints, if used, are stored per ADR-003 security rules (encrypted, access-controlled, retention TBD) and are not the audit record.
   - The graph definition and LangGraph version are part of the version metadata (FR-REC-04).
5. **No managed agent runtime** in the initial release. Bedrock Agents or Managed Agents may be reconsidered only if they pass the same validations.

**Rationale:** Step Functions keeps human-paced work durable and auditable, separate from AI reasoning. LangGraph provides explicit, inspectable state and branching for B and C without hand-building orchestration, while the Anthropic SDK stays a narrow model-access dependency. Async Python gives A the simplest deterministic implementation. All three sit behind the shared control plane, so the read-only, authorization and approval boundaries do not depend on the alternative or the framework.

## 4. Consequences and Risks

| Consequence or risk | Mitigation |
|---|---|
| LangGraph dependency and upgrade churn | Pin versions; record in version metadata; re-run regression set on upgrade |
| A framework abstraction could route a tool call around the data access layer | Register only data-access-layer tools; NT-10 checks every tool call produced `authorization.checked` and `evidence.retrieved` events |
| LangGraph checkpoints may contain evidence or PII | Encrypted, access-controlled storage per ADR-003; minimized fields (FR-EVD-08) |
| Operational complexity differs across A, B and C | Measured with AA §7 proxies, including the LangGraph dependency |
| Step Functions history retention and quotas | Audit kept in ADR-003 stores; verify quotas against NFR-SCL design concurrency |
| Divergence between workflow state and database state | Database is the source of truth for audit; reconciliation check in NT-03 |
| How the outcome-recorded state is detected | **[Open]** (FR-WFS-01 AC4); depends on PP-1 |
| Escalation transitions depend on PP-2 | State machine supports both PP-2 outcomes via configuration |
| C token use grows with specialists | Whole-run cap (NFR-FIN-02) enforced across the graph |

## 5. Validation Requirements

| Validation | Pass criterion | Test |
|---|---|---|
| Transition rules | Disallowed transitions refused (TS-N17, TS-N18, TS-N19) | FR test suite |
| No service-identity approvals | Every `approval.granted` has a human approver | TS-N14; gate G5 |
| All model tool calls via data access layer (B and C) | 100% of LangGraph tool calls appear as `authorization.checked` and `evidence.retrieved` events | NT-10; gate G6 |
| No human steps inside LangGraph | Review and approval occur only through Step Functions callbacks | Design review; TS-P05, TS-P15 |
| Validator always runs | 0 results shown without validation; G1 = 0 | Gate G1; TS-N11 |
| Token cap across the whole graph run | Run stops at cap with "insufficient evidence" | NT-14 |
| Same interface for A, B and C | All three run the identical AA §8.2 scenarios unchanged | AA §8 |
| Latency and concurrency | NFR-LAT-01 at design concurrency | NT-07, NT-09 |
| Fault tolerance | Degradation rows behave as specified | NT-05 |

## 6. Status and Traceability

**Status:** Proposed. Acceptance requires the validation above and sign-off by the approval authority (BD Q15).

| Traces to | IDs |
|---|---|
| Decisions | D9, D10, D12, D16, D17, D18 |
| Functional | FR-WFS-01, FR-REV-01, FR-REV-02, FR-APR-01 to 06, FR-ESC-01 to 04, FR-EVD-02, FR-EVD-08, FR-REC-04 |
| Non-functional | NFR-LAT-01, NFR-SCL-01 to 03, NFR-REL-01, NFR-DEG-01, NFR-FIN-02, NFR-FIN-03 |
| Architecture | AA §2, §3, §7, §8, §9 |
| Pending / open | PP-1, PP-2, FR-WFS-01 AC4, BD Q15 |
