# Release policy

## Purpose

SOSE uses releases to freeze a tested architectural contract.

Version numbers should not advance merely because several features merged. A
release is ready when its declared compatibility and durability gates are green.

## Versioning before 1.0

SOSE is pre-1.0.

- patch releases (`0.x.y`) are compatible bug fixes and documentation corrections;
- minor releases (`0.x.0`) may intentionally evolve public APIs, but every such
  change must be documented with migration guidance;
- removals of previously documented public surfaces require an explicit
  deprecation/migration note even before 1.0.

The goal of the 0.x line is to discover and stabilize the public contract. 1.0
will mean that the supported public API follows ordinary semantic-versioning
compatibility expectations.

## Release artifacts

A release candidate should produce and validate:

1. source distribution;
2. wheel;
3. installation of the built wheel in a clean environment;
4. package metadata/version consistency;
5. test matrix for supported Python versions;
6. branch-coverage gate;
7. release note and changelog entry.

Publishing to an external package index is a separate, explicit action from
building and validating artifacts.

## Release documentation

For a version `X.Y.Z`:

- `CHANGELOG.md` records user-visible changes;
- `docs/releases/vX.Y.Z.md` records the completed milestone and compatibility
  contract;
- a pre-release plan may live at `docs/releases/vX.Y.Z-plan.md`, but must not
  claim incomplete gates are satisfied.

## Compatibility surfaces

Release notes must distinguish:

- **public** — documented for domain/integration authors and compatibility-relevant;
- **advanced** — supported but lower-level and more likely to evolve before 1.0;
- **internal** — implementation detail, no compatibility promise;
- **compatibility-only** — retained for older callers but not recommended for new
  code.

The canonical classification is maintained in
`docs/architecture/public-api.md` once that contract is introduced.

## Tagging gate

A release tag is created only after:

- CI is green at the release commit;
- built artifacts install and pass smoke validation;
- version metadata is consistent;
- release documentation describes the actual commit;
- no known release-blocking defect remains open.

## Roll-forward policy

SOSE prefers corrective roll-forward releases over rewriting published tags.
Published release artifacts and release tags are immutable.
