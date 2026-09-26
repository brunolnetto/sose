# Maintenance / MRO scenarios

The MRO reference uses scenarios as durable external operating conditions.

## Asset failure / emergency arrival

Effect:

- activate `mro.asset.emergency`;
- route active maintenance through the normal durable preemption workflow;
- use scenario-owned emergency request identity.

On expiry, scenario-owned emergency capacity is released and interrupted work retries
normal bay acquisition until it can resume.

## Spare-parts disruption

Effect:

- set `mro.spare_parts.available = False` for the active window;
- make otherwise persisted stock unavailable for prerequisite evaluation;
- preserve the physical inventory records themselves.

## Technician capacity loss

Effect:

- set `mro.technician.available = False`;
- prevent new technician capacity claims;
- route affected work toward resource wait through normal reconciliation.

## Rules

- scenarios do not mutate StateCharts directly;
- activation and expiry are durable;
- historical preemption results remain audit evidence after scenario cleanup;
- restart during an active or expiring scenario must preserve equivalent behavior;
- scenario expiry must be idempotent.
