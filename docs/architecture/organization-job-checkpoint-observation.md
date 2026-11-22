# #496 W3b — admitted organizational job checkpoint observation

Dependency: **#540 W3a** owner-qualified StateChart event observation.
This is a separate read-side increment, not executable composition.

`observe_organization_jobs` first validates the exact
`OrganizationDefinition` against real catalog, process manifests and consumer
registrations. It reads one authoritative `Persistence.job_states()` tuple and
projects checkpoints only for exactly declared job IDs and matching component
domains. A declared job without a persisted `SimulationJobState` remains
`checkpoint_present=False` with absent runtime fields.

A *declared owner* is carried as topology configuration only, unless the checkpoint
was written by an attested component job (P496-B,
`organization-component-boundary-runtime-p496.md`). Such a checkpoint carries an
immutable `OrganizationJobAttestation`, and the projection reports `attested`
when it is exactly the binding this definition expects. It fails closed on a
binding to another organization, fingerprint, epoch or component. An attestation
is an identity binding written by trusted in-process code, not organizational
authorization (#545). It cannot attest that a boundary ACK, downstream Command, effect,
or physically reserved interval actually occurred. The persisted job status
and trigger IDs are observations, **not execution certificates**.

Tests cover Memory and SQLite/restart, missing checkpoints, unrelated jobs,
wrong component domain, duplicate observed identity, immutable inputs and a
single snapshot read. No global clock, lock, coordination or cross-owner cut is
invented. A combined entity/event/queue/job/resource snapshot with consistent
owner-qualified causal certificates is future #496 work, not this increment.
The heterogeneous #497 and prospective OD-5a #461 gates remain closed.
