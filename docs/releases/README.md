# SOSE releases

Release documentation records the **contract of a release**, not only a list of
merged pull requests.

Each release note should explain:

- the milestone being closed;
- public/runtime compatibility changes;
- durable semantics added or changed;
- migration or deprecation notes;
- the executable gates used before tagging;
- explicitly deferred work.

## Current release line

| Version | Status | Milestone |
| --- | --- | --- |
| 0.7.0 | released | durable operational-runtime boundary |
| 0.8.0 | planned | stabilization, persistence portability, packaging, and adoption |

See:

- [v0.7.0](v0.7.0.md)
- [v0.8.0 plan](v0.8.0-plan.md)
- [release policy](release-policy.md)

## Release-note rule

A release document must describe only behavior that is implemented and covered by
executable evidence. Planned work belongs in a `*-plan.md` document until its
release gates are satisfied.

The changelog is the chronological change record. Release documents are the
architectural closure record.
