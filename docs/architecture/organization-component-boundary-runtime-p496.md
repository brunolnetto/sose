# P496-A — owner-qualified organizational boundary runtime

**Status:** implementation slice in Draft PR; it is not the complete #496 runtime.

## Scope

This increment adds an explicit organization/component binding to a durable
boundary publication and routes the delivery through the exact admitted
component pair. It composes existing SOSE primitives; it does not introduce a
super-StateChart, organization-wide clock, second business-state owner or
distributed transaction.

A v2 scoped **BoundaryMessage** carries:

- organization ID, admitted definition fingerprint and configuration epoch;
- producer component ID and immutable process owner ID;
- consumer component ID and immutable process owner ID.

All seven values are mandatory together. Partial bindings fail closed. The
v2 deterministic message identity includes the organization, definition
fingerprint, epoch, both component identities and both owner identities, so
the same semantic occurrence in a second organization or under another owner
cannot collide with the first.

Messages created without these fields retain the existing v1 message-ID
derivation. The persistence codec also omits the new optional None fields
for v1 messages, preserving their tagged serialization shape. The v1
domain-level **BoundaryConsumerRegistry** remains available and is not silently
upgraded into organization ownership evidence.

## Admission and execution path

**OrganizationBoundaryConsumerRegistry** is built from:

1. an immutable **OrganizationDefinition**;
2. the actual **DomainCatalog** and audited process manifests;
3. the existing domain-level **BoundaryConsumerRegistry**, used only as the
   audited contract catalogue.

The constructor reruns **validate_organization_admission**. A runtime handler
must then be registered for an exact (producer_component_id,
consumer_component_id, contract_name, contract_version) edge declared by
that definition. Dispatch verifies the message fingerprint and epoch,
component IDs, domains, owners and contract edge before returning the handler.

**OrganizationBoundaryService.publish** requires typed causation:

- a **DomainEvent** whose owner, entity type, correlation and logical time agree
  with the declared producer component; or
- a previously persisted, organization-scoped boundary message whose
  destination component/owner is exactly the next message's source
  component/owner and whose organization and correlation match.

The existing **BoundaryService** still owns durable publication, deterministic
delivery identity, causal ordering, lease epochs, ACK and atomic handler/ACK
transaction. The organization facade claims by exact **message_id**; it does
not use the generic domain-wide scan to pick a message that may belong to a
different component or organization.

## Explicit non-claims / remaining P496 work

This slice proves component-bound route identity and owner-qualified source
causation, not every acceptance criterion in #496.

- ~~It does not yet instantiate **SimulationJob** objects from **ScheduledJob**,
  persist organization/component attestation on job checkpoints, or run
  independent component triggers.~~ Done in **P496-B**; see below.
- It does not yet provide the canonical multi-source organization snapshot
  across StateCharts, Commands, queues, boundary protocol, physical
  reservations and job positions.
- It does not admit operational policy/calendar/cost references or physical
  pools; static admission intentionally remains fail-closed for those.
- Static admission still rejects multiple components from the same domain
  participating in active boundaries because the audited process-manifest
  contract catalogue remains domain-keyed. The v2 router is component/owner
  qualified after admission; it does not silently waive that existing guard.
- Generic boundary paths refuse or skip v2 messages: `BoundaryService.publish`
  refuses a v2 message without a `scoped_validator`, `claim_next` never leases
  a v2 delivery without an `organization_scope`, and `BoundaryConsumerRegistry`
  will not resolve a v2 message. Only the organization facade supplies those
  hooks; the validator runs inside the advisory-locked `boundary_transaction`.
- **Trust contract:** `scoped_validator` and `organization_scope` are hooks for
  trusted in-process code, not a security boundary. Code that calls them with a
  permissive validator or another component's scope bypasses the router, just
  as it could write through the unit of work directly. A handler is likewise
  trusted domain code. This route envelope is not a protected writer-role/key
  boundary and does not replace #545 authority hardening.
- Routing isolation (scoped publish/claim/consume, end-to-end boundary chains)
  is tested on memory, SQLite and PostgreSQL. It does not prove PostgreSQL
  multi-worker/restart equivalence, finite shared pool arbitration or #477
  physical-causal admission; those remain separate gates.

The next P496 increment must bind each declared job to its exact organization,
component and owner through durable execution evidence, then compose a
consistent derived read model without a new writable organization aggregate.
P578/#477 remain the physical execution gates; P497 remains the O2C/MRO
heterogeneous falsifier. Frozen Manufacturing/O2C/MRO v1 evidence is untouched.

## P496-B — attested, independently triggered component jobs

`sose.composition.organization_jobs.build_attested_component_jobs` admits an
**OrganizationDefinition** (rerunning **validate_organization_admission**) and builds
one **SimulationJob** per declared **ScheduledJob**. Each job runs behind its own
job-scoped writer lease (**PersistentJobRunner**). Components are triggered
independently: no organization-wide clock, global lock or namespace-wide lease is
taken, and a takeover of one component's job fences only that job.

**Attestation.** Each job's checkpoint carries an immutable
**OrganizationJobAttestation**:

- organization ID;
- admitted definition fingerprint;
- config epoch;
- component ID;
- owner ID;
- domain.

It works as follows:

- **Written once.** It is committed when the job is first registered, through
  `PersistentJobRunner.initialize`, inside the job's fenced transaction. Every
  later write keeps it, because job state changes go through `replace`.
- **Fail closed.** A runner whose binding differs is refused. This covers an
  attested job run by a plain runner, a plain job adopted by the organization, and
  another fingerprint or epoch.
  - **Before the lease.** Once a checkpoint exists, the check runs before the lease
    is claimed, so a refused runner cannot fence out the correctly bound worker.
  - **At first registration** there is no checkpoint to check against, so both
    runners pass the preflight, and the first writer wins the identity for good.
    If a misbound runner writes first, its binding is the one recorded, and the
    correctly bound runner is refused from then on: start the correct binding
    first, or remove the misbound checkpoint deliberately. The loser is refused by
    the recheck inside the registration transaction, but its claim may already
    have fenced the winner's in-flight initialization, which resumes on retry.
    The recorded binding is never mixed; only liveness is affected.
- **Observation.** `observe_organization_jobs` reports `attested`. It fails closed
  on a checkpoint attested to another binding.
- **Storage.** The field is optional and the codec omits it when `None`, so the
  checkpoints of unbound jobs keep their tagged bytes.

**Owner rule.** A **SimulationJob** writes as Engine owner `job_id`, so a declared
job must use its component's process `owner_id` as its `job_id`. Otherwise the
attested owner would not be the owner that writes, and the job is refused.

**Limitations of P496-B:**

- **A definition change locks the existing jobs.** The attestation stores the
  admitted fingerprint and `config_epoch`. The fingerprint covers the whole
  definition: components, boundaries, jobs and pools. Any admitted change therefore
  makes every existing component job refuse to run, and `observe_organization_jobs`
  refuse the organization. Examples are adding an unrelated component or boundary,
  or bumping `config_epoch`. Because `job_id` must equal the owner, the identity
  cannot be reused either. The explicit migration is
  `sose.composition.organization_reattest.reattest_component_jobs` (below).
- **One job per component.** The owner rule makes `job_id` equal the component's
  owner, so a component can have at most one job. ADR-0009 allows an organization
  to own several independently scheduled jobs. The runtime cannot honour that until
  a `SimulationJob` can write as an owner other than its `job_id`.
- **Older SOSE cannot read the namespace.** Decoding an attested checkpoint needs
  `OrganizationJobAttestation`. The SQLite, PostgreSQL and DuckDB adapters reload
  the whole namespace on refresh, so a SOSE version from before P496-B fails on
  every read of a namespace that holds one.
- **Partial lease API.** `PersistentJobRunner` has no `update_config`,
  `apply_config`, `pause` or `resume`. The `SimulationJob` versions of those
  operations still check and keep the attestation.

**Not claimed by P496-B:**

- **Not authority.** The attestation is an identity binding written by trusted
  in-process code, not a protected writer-role/key boundary; persistence-level
  authority remains #545.
- **Not equivalence.** It does not prove PostgreSQL multi-worker/restart
  equivalence of the composed run.
- **Not the snapshot.** It does not provide the canonical organization snapshot.
- **No trigger use.** `ScheduledJob.trigger_namespace` is still not used to derive
  trigger identities.

### Re-attesting jobs after a definition change (#496, N1)

`reattest_component_jobs(new_definition, persistence, expected_previous=(fingerprint,
config_epoch), ...)` is an explicit compare-and-swap that moves the declared jobs to
a new admitted definition.

1. **Admission.** It admits the new definition.
2. **Checks every declared job before writing anything.** A job is refused when:
   - its checkpoint is plain (never attested);
   - its component's identity changed: organization, component, owner or domain;
   - its recorded fingerprint and epoch are not exactly `expected_previous`;
   - an occurrence is in flight.

   One refusal writes nothing for any job and moves no epoch.
3. **Rewrite.** It swaps each job's attestation under that job's own scoped lease.
   It re-reads the checkpoint inside the fenced transaction and refuses a job that
   changed after planning. `job_id`, run counts and history are kept.

**Result.** It returns one report per job: `reattested`, `already_current` (running it
again is a no-op) or `absent` (declared but never registered; it registers fresh
under the new definition).

**Operational notes:**
- Claiming the lease fences a worker still running the job under the old binding.
  That worker is then refused and must be rebuilt from the new definition.
- Jobs the new definition no longer declares are left as they are.
- It is a library operation; there is no CLI command yet.

