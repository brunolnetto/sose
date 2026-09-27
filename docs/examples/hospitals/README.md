# Hospitals

Status: **Reference implementation**

This domain is being promoted from Blueprint into a reference-grade hospital-operations example.
It focuses on triage ordering, scarce ward/ICU capacity, treatment episodes, clinically allowed
procedure preemption, transfers, discharge, and restart-safe recovery.

See [specification.md](specification.md) for the authoritative contract.

The reference implementation includes acuity-ordered triage, scarce ward/ICU/team capacity,
Admission-gated procedures, durable emergency preemption evidence and recovery, transfer cleanup,
finite occupancy/emergency scenarios, and restart equivalence.
