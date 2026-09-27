# Hospitals

Status: **Partial**

This domain is being promoted from Blueprint into a reference-grade hospital-operations example.
It focuses on triage ordering, scarce ward/ICU capacity, treatment episodes, clinically allowed
procedure preemption, transfers, discharge, and restart-safe recovery.

See [specification.md](specification.md) for the authoritative contract.

Reference-grade promotion still requires durable triage queues, bed/team resources, procedure
preemption execution/recovery, occupancy-surge behavior, happy/sad paths, and restart equivalence.
