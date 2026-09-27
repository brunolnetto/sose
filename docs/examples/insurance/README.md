# Insurance

Status: **Reference implementation**

This reference domain models claims as a durable chain of coverage, documentation,
assessment, fraud review, reserve establishment, payment scheduling, and payout.

The implementation intentionally separates `Claim`, `Assessment`, `Reserve`,
`Payment`, and `FraudInvestigation` so that financial and investigative evidence
remains independently durable.

See [specification.md](specification.md) for the authoritative contract.

The reference implementation includes happy path, document expiry, severity-prioritized assessment, fraud review and reassessment, reserve-before-payment, partial payout, rejected-claim reopening, finite scenarios, post-commit crash recovery, and restart equivalence.
