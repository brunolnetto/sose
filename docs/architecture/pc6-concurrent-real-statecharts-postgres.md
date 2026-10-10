# PC6 concurrent domain-statechart PostgreSQL falsification

## PRD
After #429 (two real sequential statechart paths) and #430 (full-state PostgreSQL reconnect equivalence), test **overlapping** execution of two independent but equal-price Trading Company customers in the SAME OLTP namespace. Frozen v1 artifacts and reference defaults are immutable.

## TRD and TDD
Start two independent PostgresPersistence connections in two Python worker threads. Inject a deterministic two-party barrier immediately after each instance's first durable `o2c.fulfillment_requested.v2` transport ACK and before either first domain intent is executed. This guarantees real worker overlap; neither flow can finish before the other starts. Both subsequently execute normal customer domain statecharts and durable composition services rather than fabricated terminal entity records.

Falsification predicates:
- Both ingress correlations reached the barrier and are unique
- Two distinct orders, shipments, payments, journals, stock records, sixteen boundary message IDs and effect IDs
- Eight immutable BusinessEffectApplied certificates, no missing certificates or pending effects, sixteen ACK receipts
- All final business entities have expected states, and each has at least one attributable domain event
- Event IDs remain unique and settlement identity cannot alias at the same USD 250 amount
- Reopen a third/fourth independent PostgreSQL connection to compare complete entities/events, DAG, certificates, reservations, schedules, durable store/container state, sink checkpoints and job states across reconstruction

## ADR and scientific limitations
This test intentionally allows actual concurrent execution after the two-party ingress barrier rather than enforcing a sequential reference schedule. It checks local safety/convergence and restart durability; it does **not** assert equal event timestamps or physical worker lease history versus a sequential baseline. Worker death, OS SIGKILL, PostgreSQL server unavailability, network partition, and multi-run schedule equivalence are separate falsification gates. Concurrent process clocks and shared resource capacity may legitimately expose a design defect; keep the PR red until the actual architectural cause is resolved rather than weakening its assertions.

CI requires full Python 3.12–3.14 matrix, authoritative coverage >=95%, PostgreSQL, chaos, historical organizational v1 replay, benchmarks and review before merge. Issue #406 stays open.
