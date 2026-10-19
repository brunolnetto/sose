# #496 W3c — selected boundary protocol evidence (read-only)

Parent: **#541 W3b**, stacked after #540 W3a. This bounded observer is NOT
runnable organizational composition or a new authoritative scheduler.

`inspect_selected_boundary_protocol(..., message_ids=...)` validates the
exact OrganizationDefinition against audited catalog/manifests/handlers and
reads publication, delivery and ACK snapshots inside one persistence UoW.
It then checks each caller-selected immutable message against an admitted
producer-domain/destination-domain/contract/version edge, the matching
deterministic delivery identity/metadata and ACK-vs-delivery consistency.

* The caller **must explicitly select message IDs**; we do not scan all
  same-domain messages and falsely attribute unrelated organizations' traffic.
  Selection is a query filter, not independent ownership proof.
* `BoundaryMessage.source_identity` is not an owner-qualified component key,
  and `BoundaryDelivery.claim_owner_id` is a claim worker, not an organizational
  operational owner. Projected component IDs are **declared topology labels**.
* A consumed ACK may reference an opaque `consumer_effect_id`, but it is not
  automatically a durable `BusinessEffectApplied` or causal Command. W3c does
  not certify effects, persistence ownership, temporal resource capacity,
  or independent runnable jobs.
* No cross-store/global snapshot, lock or clock is synthesized. Local
  UoW consistency is bounded to one persistence store, not to multiple
  independent organizations.

Memory/SQLite tests cover pending, claimed, ACK-consumed, retry identities,
restart, unadmitted edge/manifest, unselected unrelated messages, and malformed
durable delivery/receipt relations. The WM ingress registration in tests is a
hypothetical explicitly scoped fixture; it does **not** amend built-in
ProcessManifest audit results. #496 and #497 remain open.
