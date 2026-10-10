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

## First red-first evidence: PostgreSQL namespace bootstrap
The initial concurrency run was red: one worker timed out at the ingress barrier because the other failed *before constructing its Persistence adapter*, with PostgreSQL `UniqueViolation` on `pg_type_typname_nsp_index` when two sessions simultaneously attempted `CREATE TABLE IF NOT EXISTS <namespace>_record_meta`. `IF NOT EXISTS` is not transactional inter-session schema synchronization. The fix acquires an exclusive namespace-scoped transaction advisory lock before adapter schema setup or migration (same lock family as boundary writer shared locks). A four-writer simultaneous fresh-namespace test now guards this invariant. An ingress-barrier timeout alone is not a causal statechart failure; diagnose independent worker exceptions first.

## Second red-first evidence: R2R shared-capacity contention
Once PostgreSQL bootstrap was serialized, the overlap test progressed to the real domains and one worker failed with `R2R journal posting failed`. The R2R posting processor has durable capacity 1 and `submit_and_post_journal` returns False when its resource is temporarily unavailable. The single-path composition had treated this transient contention as a fatal failure. The PostgreSQL adapter now exposes a namespace-and-resource-scoped **session advisory guard**, held across the journal's multiple underlying durable transactions; competing workers cannot simultaneously claim the posting capacity. A separate two-connection test verifies that the second worker blocks until release. The lock is released on normal exit or PostgreSQL connection death. This is short-lived processing coordination, not a substitute for authoritative resource reservations, persisted commands, causation or writer fencing; non-PostgreSQL adapters preserve their existing semantics.

Future work: replace application-level posting coordination with a generalized, durable wait/retry scheduling contract for resource contention across adapters, including failure after partial posting and network interruption.

## Third red-first evidence: cross-flow delivery theft
With PostgreSQL bootstrap serialized and the R2R posting processor coordinated, simultaneous real customer statecharts reached business certification but both failed with `business effect target/contract contradicts causal boundary payload`. The compositor's own `_consume_next` used an unrestricted global `claim_next()`, so customer A could ACK customer B's earlier delivery with a registry that bound the effect to A's entity ID. The BusinessEffectApplied predicate correctly rejected the causal mismatch.

Fix: add **optional exact `message_id` predicate** to authoritative BoundaryService.claim_next, checked within the same transaction that leases the delivery. The customer composer now always claims the precise immutable boundary message it just published, validates the lease identity, then ACKs. The default global claim API and destination/contract-scoped runner behavior are unchanged. Memory and SQLite tests falsify cross-customer theft when a foreign message at the same destination has higher claim priority. Do not weaken certificate/ACK identity invariants to resolve an incorrect selection.
