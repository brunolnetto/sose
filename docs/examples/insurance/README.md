# Insurance

Status: **Partial**

This reference domain models claims as a durable chain of coverage, documentation,
assessment, fraud review, reserve establishment, payment scheduling, and payout.

The implementation intentionally separates `Claim`, `Assessment`, `Reserve`,
`Payment`, and `FraudInvestigation` so that financial and investigative evidence
remains independently durable.

See [specification.md](specification.md) for the authoritative contract.

Executable evidence is present for happy path, document expiry, fraud review,
partial payout, rejected-claim reopening, finite scenarios, and restart boundaries.
Promotion remains pending CI and review.
