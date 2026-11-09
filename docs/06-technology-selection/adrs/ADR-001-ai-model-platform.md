# ADR-001: AI Model Platform

| Field | Value |
|---|---|
| Status | **Proposed** (not accepted; approval authority **[Open]**, BD Q15) |
| Date | 2026-11-08 |
| Applies to | Alternatives B and C only (AA §3). Not needed in production if A is selected; still needed to benchmark B and C |
| Related | [Technology Selection](../technology-selection.md), ADR-002, ADR-005 |

## 1. Context and Requirements

Alternatives B and C need a large language model for analysis. The model only *reads* evidence through the shared data access layer and *proposes* results through the output validator (AA §2). It never holds credentials, writes to systems of record or approves anything **[Decision D11, D18]**.

| Requirement | Source |
|---|---|
| Tool use with schema-validated arguments, so every lookup goes through the data access layer | AA §2; FR-AUTH-02 |
| Structured output that cites evidence item IDs | FR-EVD-02, FR-REC-01 |
| Version metadata per result (model ID and version) | D18; FR-REC-04 |
| Data stays within controlled boundaries; no training on NorthStar data | NFR-PRV-03; BD §9.3 |
| Private network path and IAM-based access, preferably within AWS | ADR-005; NFR-SEC-03, 04 |
| Data residency compatible with the chosen region | BD Q12; NFR-PRV-05 |
| Token counting and per-run caps; cost attribution | NFR-FIN-01 to 04; NFR-OBS-07 |
| Rate limits that support the design concurrency | NFR-RTY-07; NFR-SCL-01 |
| Degrades to evidence-only view when unavailable | NFR §4 |

## 2. Alternatives Considered

| Option | Strengths | Weaknesses |
|---|---|---|
| **1. Claude models via Amazon Bedrock** | AWS-operated inside NorthStar's AWS accounts; IAM access, CloudTrail logging, AWS billing; supports the features needed here (Messages, tool use, structured outputs and strict tool use, prompt caching, token counting, citations) per the Claude API platform-availability reference | Partner-operated: new models and features may arrive later than on the first-party API; a feature subset (for example no Message Batches, no server-side refusal fallbacks, no `inference_geo` parameter); model availability varies by region (to verify); model IDs carry an `anthropic.` prefix |
| **2. Claude Platform on AWS** | Anthropic-operated with same-day API parity; AWS IAM (SigV4) and AWS Marketplace billing; supports `inference_geo` for data residency control | Endpoint is operated by Anthropic rather than inside Bedrock; private connectivity options and data-handling terms to verify with Security and Compliance |
| **3. First-party Anthropic API** | Full and earliest feature access | Outside AWS IAM and billing; separate credential management; network egress to an external API |
| **4. Self-hosted open-weight models on AWS** | Full data control | Highest operational load (GPU capacity, serving, upgrades, safety evaluation); quality against gates unknown |
| **5. Another provider's models in another cloud** | Possible quality or cost options | Cross-cloud networking, identity and data-transfer complexity; conflicts with the AWS direction |

## 3. Proposed Decision and Rationale

**[Proposal]**

1. **Access Claude models through Amazon Bedrock** for the benchmark and, if B or C is selected, for production. Keep **Claude Platform on AWS** as the evaluated alternative if Bedrock lacks a needed model, feature or region.
2. **Use a provider-neutral internal interface** (`analyze(evidence handle, tools) → draft result`) so the provider or model can change without touching the control plane.
3. **Benchmark candidate models on identical scenarios** (AA §8), starting with the most capable current Opus model and including current lower-cost tiers:

   | Candidate | First-party model ID | Bedrock form |
   |---|---|---|
   | Claude Opus 5.5 (default starting point) | `claude-opus-5-5` | `anthropic.claude-opus-5-5` |
   | Claude Sonnet 5.5 | `claude-sonnet-5-5` | `anthropic.claude-sonnet-5-5` |
   | Claude Haiku 5.5 | `claude-haiku-5-5` | `anthropic.claude-haiku-5-5` |

   Regional availability of each on Bedrock is **TBD** (to verify). A lower-cost model is chosen only if it passes the same hard gates and quality criteria; cost alone never justifies a downgrade.

**Rationale:** Bedrock keeps inference inside NorthStar's AWS security model (IAM, logging, private networking) with the fewest new trust relationships, which fits the read-only, least-privilege boundaries. The features the platform needs are available there. The provider-neutral interface limits lock-in.

### 3.1 Design Constraints From Current Models

These affect how B and C are built, not whether they are allowed:

| Constraint | Consequence |
|---|---|
| Forced tool choice (`any` or a named tool) is not supported on Claude Opus 5.5 and Sonnet 5.5 | Use automatic tool choice with strict tool schemas and structured outputs; the data access layer still validates every call |
| Thinking cannot be switched off on Claude Opus 5.5; depth is controlled with effort | Effort level is a tuning parameter recorded in version metadata |
| Sampling parameters (temperature and similar) are not available on current models | Run-to-run variability cannot be removed by settings; it is measured (AA §8.4 consistency) |
| Models can return a `refusal` stop reason | Treat a refusal as "insufficient evidence" or route to manual investigation; never retry with weakened safety instructions |
| Server-side refusal fallbacks are not available on Bedrock | Use client-side handling if a fallback model is approved |

## 4. Consequences and Risks

| Consequence or risk | Mitigation |
|---|---|
| Model or feature arrives later on Bedrock | Provider-neutral interface; Claude Platform on AWS as alternative |
| Region lacks a candidate model | Verify before region selection (ADR-005) |
| Bedrock pricing differs from first-party pricing | NFR §12 placeholders stay until Bedrock pricing for the chosen region is confirmed (TBD) |
| Prompt injection through free text | Free text passed as data; output validator; NFR-SEC-06 test set |
| Provider outage | Evidence-only degradation (NFR §4) |
| Model version changes alter behavior | Pin model versions; re-run benchmark and regression set before changing (FR-REC-04) |

## 5. Validation Requirements

| Validation | Pass criterion | Test |
|---|---|---|
| Hard gates with each candidate model | G1–G7 and NFR-SEC-06 pass | AA §8; NT-10 |
| Quality | G8 not lower than human baseline **[Proposal]** | AA §8.3 |
| Latency within platform budget | NFR-LAT-01 met | NT-07 |
| Tokens and cost per run measured | Reported by model; within NFR-FIN-02 cap | NT-13, NT-14 |
| Model has no route to systems of record | Network and IAM review shows Bedrock is the only model endpoint; tools only via data access layer | NT-10 |
| Data-handling terms | Privacy/Compliance confirm no training on NorthStar data and acceptable retention | NT-11 |
| Region availability | Each shortlisted model available in the chosen region | Pre-benchmark check |
| Refusal handling | Refusals produce "insufficient evidence" or manual routing, never unsupported claims | Benchmark fixtures |

## 6. Status and Traceability

**Status:** Proposed. Acceptance requires the validation above and sign-off by the approval authority (BD Q15).

| Traces to | IDs |
|---|---|
| Decisions | D10, D11, D12, D16, D17, D18 |
| Functional | FR-AUTH-02, FR-EVD-02, FR-EVD-07, FR-REC-01, FR-REC-04 |
| Non-functional | NFR-LAT-01, NFR-SEC-03, NFR-SEC-04, NFR-SEC-06, NFR-PRV-03, NFR-PRV-05, NFR-RTY-07, NFR-SCL-01, NFR-FIN-01 to 04, NFR-OBS-07 |
| Architecture | AA §2, §3.2, §3.3, §8 |
| Open | BD Q12, BD Q15, model pricing and region availability (NFR §14) |
