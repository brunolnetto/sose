# #462 — legacy OD-2 cutover audit (offline, non-authorizing)

The old canonical actor enrollment only wrote a calendar marker when a calendar
was provided; older calendar-free actors have no immutable admission marker.
Original calendar records do not contain new admitted-at/replacement attributes.
Older assign Command/events may lack attested work-ID even when mutable actor
state contains one. Historical frozen v1 replay does not prove old OD-2 upgrades.

This audit classifies **quarantined_unattested**, **legacy_calendar_evidence_only**,
or **conflicting_legacy_marker**. It never writes, never signs an operator cutover
and returns admits_transition=False in every case. It only examines a coherent,
caller-provided offline actor+event snapshot. An event published from a public
UoW is not a protected signature; a matching calendar marker is a migration
**candidate** requiring independently anchored provenance. Never use this audit
as a replacement for #545 trusted issuance or as a permission to cancel/exit.

Next: authorized migration with signed source inventory, immutable versioned
admission and separately versioned work-ID cutover; adversarial real PostgreSQL
restart/worker fencing and test upgrade from actual archived v1 fixtures.
