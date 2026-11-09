# ADR-004: Workforce Identity and Authorization

| Field | Value |
|---|---|
| Status | **Proposed** (not accepted; approval authority **[Open]**, BD Q15) |
| Date | 2026-11-08 |
| Applies to | All alternatives (shared control plane) |
| Related | [Technology Selection](../technology-selection.md), ADR-002, ADR-005 |

## 1. Context and Requirements

CS agents, approvers and reviewers must sign in with their workforce identity. Every retrieval must be authorized against **both** the requesting user's permissions and the platform's service scope **[Decision D16]**. Approvals must come from humans, with separation of duties where policy requires **[Decision D18]**. Authorization must never be decided by a model.

| Requirement | Source |
|---|---|
| Authenticated identity on every investigation and human action | FR-AUTH-01 |
| Dual check (user and service) on every retrieval, including Snowflake and, under PP-1, the case system | D16; FR-AUTH-02 |
| Denials reveal no restricted content | FR-AUTH-03 |
| Data cached for one user not shown to another without re-authorization | FR-AUTH-02 AC2 |
| Approver authority by role and amount; SoD; platform never approves | D18; FR-APR-01, FR-APR-03, FR-APR-04 |
| Read-only service credentials | D11; FR-RO-01; NFR-SEC-02 |
| Short-lived tokens; secrets in a secrets manager | NFR-SEC-04 |
| Quarterly access review | NFR-SEC-08 |
| Snowflake read-only and historical only | D5; FR-HIST-02 |

## 2. Alternatives Considered

### 2.1 Workforce Sign-in

| Option | Strengths | Weaknesses |
|---|---|---|
| **Federate the existing enterprise IdP via OIDC** | Single sign-on; joiner/mover/leaver handled centrally; MFA and conditional access already governed | Depends on which IdP NorthStar uses (**[Open]**) and its group data quality |
| Amazon Cognito user pool federated to the IdP | Managed token handling for the app | Extra hop; still depends on the enterprise IdP |
| AWS IAM Identity Center | AWS-native workforce identity | Aimed at AWS access, not application end users |
| Local application accounts | None significant | Duplicates identity; poor lifecycle control; rejected |

### 2.2 Authorization (Policy Decision Point)

| Option | Strengths | Weaknesses |
|---|---|---|
| **Amazon Verified Permissions (Cedar policies)** | Managed policy engine; policies separate from code; decision logging; analyzable policy language | AWS-specific; policy and entity modeling effort |
| Open Policy Agent (Rego) | Portable, widely used | Self-hosted engine to run and secure |
| Role checks in application code | Simple to start | Policies scattered; hard to review and audit |
| Rely only on each system of record's own authorization (user token forwarding) | Uses the owner's authoritative rules | Only works where systems accept delegated user tokens (unknown, BD Q6); Snowflake and other sources may not; no single place to enforce SoD |

## 3. Proposed Decision and Rationale

**[Proposal]**

1. **Sign-in:** federate NorthStar's existing workforce IdP via OIDC at the internal load balancer and application. MFA per corporate policy. The IdP product is **[Open]**.
2. **Policy decision point:** **Amazon Verified Permissions**, called by the **data access layer** before every retrieval and by the **workflow service** before every review, approval and escalation action.
   - Inputs: user identity and groups from the IdP token, the resource domain (order, payment, inventory, shipping, historical, case), the action, and case attributes.
   - Separation of duties and approver amount limits are policies, not code branches.
   - Every decision is logged as `authorization.checked` or `authorization.denied` (WF §11).
3. **Defense in depth:** where a system of record supports delegated user tokens (for example OAuth token exchange), forward the user's context so the owner also enforces its rules. Where it does not, the platform uses a read-only service credential **and** the policy check. Which systems support delegation is **TBD** (BD Q6).
4. **Service identities:** one IAM role per component with least privilege. System-of-record and Snowflake credentials are read-only, stored in AWS Secrets Manager and rotated (NFR-SEC-04). Snowflake access uses a dedicated read-only role limited to approved historical datasets.
5. **Models are never in the authorization path.** The analysis layer has no credentials; tools call the data access layer, which applies the policy check with the requesting user's identity (ADR-002).

**Rationale:** a central, analyzable policy engine makes D16 and D18 reviewable in one place and testable independently of the alternative chosen. Reusing the enterprise IdP avoids a parallel identity store.

## 4. Consequences and Risks

| Consequence or risk | Mitigation |
|---|---|
| IdP groups do not map cleanly to data entitlements | Data owners define entitlement mapping (WF RACI row 15); quarterly review |
| Policy and system-of-record rules diverge | Delegated tokens where supported; reconciliation checks during access reviews |
| Policy service outage | Fail closed: no retrievals (NFR §4) |
| Approver limits and SoD scope undecided | Policies parameterized; values **[Open]** (WF Q2) |
| PP-1 rejected | No case-system policies or credentials needed |

## 5. Validation Requirements

| Validation | Pass criterion | Test |
|---|---|---|
| Every retrieval authorized twice | 100% coverage; 0 violations (gate G6) | NT-10; TS-N01 |
| No cross-user leakage | Second user without permission does not see cached data | TS-N02 |
| Unauthenticated access refused | No retrieval without identity | TS-N03 |
| SoD and approver limits | Self-approval and over-limit approvals refused | TS-N14, TS-N16 |
| Read-only credentials | 0 write-capable credentials for systems of record | NT-10; NFR-SEC-02 |
| Fail closed | Policy service outage blocks retrievals | NT-05 |
| Policy review | InfoSec and data owners approve the policy set | Access review record |

## 6. Status and Traceability

**Status:** Proposed. Acceptance requires the validation above and sign-off by the approval authority (BD Q15) and Information Security.

| Traces to | IDs |
|---|---|
| Decisions | D5, D9, D11, D16, D18 |
| Functional | FR-AUTH-01 to 03, FR-RO-01, FR-RO-03, FR-APR-01, FR-APR-03, FR-APR-04, FR-HIST-02 |
| Non-functional | NFR-SEC-01, NFR-SEC-02, NFR-SEC-04, NFR-SEC-08, NFR-DEG-01 |
| Architecture | AA §2 |
| Pending / open | PP-1, BD Q6, BD Q15, enterprise IdP product, WF Q2 |
