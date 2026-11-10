"""LLM provider adapters (Step 2.4B experiment).

Minimal, read-only access to a hosted model so alternatives B and C can be
benchmarked (docs/05-architecture-alternatives, section 8). The model only
reads evidence it is given and drafts text; it has no tools, no route to
systems of record and never approves anything [Decision D11, D18].

Provider choice is a [Proposal] (ADR-001, status Proposed). Nothing here
selects an architecture or an agent count. Credentials come only from the
standard AWS credential chain; none are stored in code.
"""
