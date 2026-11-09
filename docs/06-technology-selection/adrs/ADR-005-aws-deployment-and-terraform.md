# ADR-005: AWS Deployment and Terraform

| Field | Value |
|---|---|
| Status | **Proposed** (not accepted; approval authority **[Open]**, BD Q15) |
| Date | 2026-11-08 |
| Applies to | All alternatives |
| Related | [Technology Selection](../technology-selection.md), ADR-001 to ADR-004 |

## 1. Context and Requirements

AWS as the cloud platform and Terraform as infrastructure as code were specified in the Step 1.6 request. They are **not yet recorded as decisions** in BD §12 **[Open]**. This ADR proposes how to deploy on AWS with Terraform within the project's boundaries.

| Requirement | Source |
|---|---|
| Availability 99.5% of service hours; RTO ≤ 4 h; RPO targets | NFR-AVL-01, 03, 04 **[Proposal]** |
| Design concurrency 5 / 25 / 70 by scenario; horizontal scaling | NFR §7; NFR-SCL-01 to 04 |
| Read-only access to systems of record; no write credentials | D11; NFR-SEC-02 |
| Encryption in transit and at rest; short-lived credentials; secrets manager | NFR-SEC-03, 04 |
| Data residency and region | BD Q12; NFR-PRV-05 (TBD) |
| Observability: correlation, tracing, burn-rate alerts | NFR-OBS-01 to 05 |
| Cost tagging and budget alerts | NFR-OBS-07; NFR-FIN-05 to 08 |
| Reproducible, reviewable infrastructure changes | AA §7 operational complexity |

## 2. Alternatives Considered

### 2.1 Compute

| Option | Strengths | Weaknesses |
|---|---|---|
| **Amazon ECS on AWS Fargate** | No servers to manage; long-running services and runs of up to minutes (NFR-LAT-01 p99 180 s); simple scaling | Less flexible than Kubernetes for complex scheduling |
| AWS Lambda | No servers; pay per use | Execution time limits and cold starts to evaluate against run duration; harder connection management to the database |
| Amazon EKS | Most flexible | Highest operational load for a small number of services |

### 2.2 Account and Network Layout

| Option | Strengths | Weaknesses |
|---|---|---|
| **Multi-account (AWS Organizations): separate dev, test and prod, plus security and log-archive accounts** | Blast-radius isolation; clean separation of audit logs; per-environment cost | More setup |
| Single account with environment tags | Simple | Weak isolation between environments |

### 2.3 Infrastructure as Code

| Option | Strengths | Weaknesses |
|---|---|---|
| **Terraform** | Specified in the request; wide provider coverage including Snowflake; plan review workflow | State management and locking to set up |
| AWS CloudFormation or CDK | AWS-native | Not the requested tool |

## 3. Proposed Decision and Rationale

**[Proposal]**

1. **Region:** one AWS region, chosen to satisfy data residency (BD Q12) and model availability (ADR-001). **TBD.**
2. **Accounts:** AWS Organizations with separate **dev, test and prod** workload accounts, a **security** account and a **log-archive** account. Service control policies deny disabling CloudTrail, deleting audit archives and creating public endpoints for platform data.
3. **Availability:** Multi-AZ within the region for compute (ECS Fargate across at least two AZs), Aurora (ADR-003) and load balancing. This targets 99.5% of service hours (NFR-AVL-01). Cross-region disaster recovery is **not** proposed for the initial release; RTO 4 h is met by restore from backups in the same region, to be confirmed by NT-02. **[Proposal]**
4. **Compute:** ECS on Fargate for the investigation service (UI back end, workflow callbacks, analysis layer and data access layer). Step Functions for the case workflow (ADR-002).
5. **Network:** workloads in private subnets. VPC endpoints for AWS services used (Bedrock, Secrets Manager, S3, KMS, CloudWatch, Step Functions, Verified Permissions) where available in the region (to verify). Outbound access to systems of record and Snowflake through a controlled egress path with an allow-list, or private connectivity where those systems offer it. The UI is reachable only through an internal load balancer.
6. **Security services:** KMS customer-managed keys; Secrets Manager with rotation; CloudTrail organization trail to the log-archive account; AWS WAF on the load balancer if the UI is exposed beyond the corporate network (**[Open]**).
7. **Observability:** CloudWatch metrics, logs and alarms; OpenTelemetry tracing (AWS Distro for OpenTelemetry); burn-rate alarms per NFR-OBS-04.
8. **Cost:** mandatory tags (environment, component, architecture option, cost category) enforced in Terraform; AWS Budgets alerts at 50%, 80% and 100%; Cost Anomaly Detection for the 2× daily spike rule (NFR-FIN-06, 07).
9. **Terraform:**
   - Remote state in an encrypted, versioned S3 bucket with state locking, in the security or a dedicated tooling account.
   - Modules per component; one root configuration per environment account.
   - CI runs `terraform fmt`, `validate`, `plan` and policy-as-code checks on every change; `apply` to prod requires a recorded human approval.
   - Policy checks block: write-capable IAM permissions to system-of-record credentials, unencrypted storage, public endpoints for platform data, and missing cost tags.
   - CI authenticates to AWS with short-lived OIDC federation, never long-lived access keys.
   - No secrets, credentials or account-specific sensitive values in the repository.

**Rationale:** managed, serverless-style services (Fargate, Aurora, Step Functions, Verified Permissions) keep the operational surface small, which AA §7 treats as a cost. Multi-AZ in one region matches the proposed 99.5% service-hours target without the cost and complexity of multi-region. Terraform with policy checks makes the read-only and encryption boundaries enforceable before deployment.

## 4. Consequences and Risks

| Consequence or risk | Mitigation |
|---|---|
| **The repository is public** | Decide repository visibility before adding Terraform (**[Open]**); keep account IDs, CIDRs and endpoints in private variable files or a private repository |
| Single-region outage exceeds RTO | Accept for initial release **[Proposal]**; revisit if NT-02 or stakeholders require multi-region |
| Region lacks a needed service or model | Verify ADR-001 models and all VPC endpoints before choosing the region |
| Egress to external systems of record | Allow-list; per-dependency rate limits (NFR-RTY-01); monitoring |
| Terraform state contains sensitive values | Encrypted state bucket; restricted access; no secrets in state where avoidable |
| Fargate run-duration and scaling limits | Validate with NT-09 at design concurrency |

## 5. Validation Requirements

| Validation | Pass criterion | Test |
|---|---|---|
| Policy-as-code | CI blocks a change granting write access to a system-of-record credential, an unencrypted store, a public endpoint, or missing tags | Seeded failing changes in CI |
| Availability and recovery | NFR-AVL-01 in pilot; RTO and RPO in drill | NT-01, NT-02 |
| Scaling | NFR-SCL-01 to 03 at the chosen scenario | NT-09 |
| Network isolation | No public endpoint for platform data; egress only to allow-listed destinations | NT-10 |
| Encryption and credentials | NFR-SEC-03, 04 met | NT-10 |
| Observability and alerts | NFR-OBS-01 to 05 met | NT-12 |
| Cost controls | Tags complete; budget and anomaly alerts fire | NT-13, NT-15 |

## 6. Status and Traceability

**Status:** Proposed. Acceptance requires the user to record AWS and Terraform as decisions, the validation above, and sign-off by the approval authority (BD Q15).

| Traces to | IDs |
|---|---|
| Decisions | D5, D11, D16 |
| Non-functional | NFR-AVL-01, NFR-AVL-03, NFR-AVL-04, NFR-SCL-01 to 04, NFR-SEC-02 to 04, NFR-PRV-05, NFR-RTY-01, NFR-OBS-01 to 05, NFR-OBS-07, NFR-FIN-05 to 08 |
| Functional | FR-RO-01, FR-AUD-03 |
| Architecture | AA §2, §7 |
| Open | AWS and Terraform decisions, BD Q12, BD Q15, repository visibility, WAF exposure |
