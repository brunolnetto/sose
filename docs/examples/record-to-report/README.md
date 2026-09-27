# Record-to-Report

Status: **Reference implementation**

This domain is being promoted from Blueprint into a reference-grade accounting-close example.

It separates journal posting, reconciliation, adjustment, close-task execution, and accounting-period state so that calendars, manual exception capacity, audit evidence, and controlled reopening remain independently durable.

See [specification.md](specification.md) for the authoritative contract.

The reference implementation includes posting, reconciliation, adjustment causality, durable close scheduling, posting/reconciliation/close capacity, controlled reopening, finite scenarios, illegal prerequisite rejection, post-commit cleanup recovery, and restart equivalence.
