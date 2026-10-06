# Process Canonical Implementation Dependency Graph

## Purpose

This document fixes the implementation order for promoting SOSE business domains into complete and composable process canonicals.

The graph is a **development dependency graph**, not a runtime process diagram.

An edge means:

> the upstream capability must be sufficiently stable before the downstream implementation should be promoted.

## Development DAG

```mermaid
flowchart TD
    A[Existing SOSE kernel] --> B[W0 Process Canonical Contract]
    B --> C[W1 Catalog-wide Evidence Audit]
    B --> D[W3 Cross-domain Boundary Contract]

    C --> E[W2 Warehouse Fulfillment PC5]
    C --> F[W2 Warehouse Management PC5]

    E --> G[W4 Trading Backbone Domains PC5]
    F --> G
    C --> G

    D --> H[W5 Trading Company PC6]
    G --> H

    H --> I[W6 Manufacturing PC5/PC6]
    H --> J[W6 MRO PC5/PC6]

    I --> K[W7 Asset-intensive Enterprise]
    J --> K

    C --> L[W8 Mobility / Service Cluster]
    D --> L

    C --> M[W9 Digital-service Cluster]
    D --> M

    C --> N[W10 Regulated-finance Cluster]
    D --> N

    C --> O[W11 Healthcare]
    D --> O

    H --> P[W12 Organizational Dynamics]
    K --> P
    L --> P
    M --> P
    N --> P
    O --> P

    H --> Q[W13 Digital Twin Projections]
    K --> Q
    L --> Q
    M --> Q
    N --> Q
    O --> Q
```

## W4 internal dependency order

The trading-company backbone should not be implemented as seven isolated promotions in arbitrary order.

Recommended sequence:

```mermaid
flowchart LR
    WM[Warehouse Management PC5] --> P2P[Procure-to-Pay PC5]
    WM --> WF[Warehouse Fulfillment PC5]
    WF --> LOG[Logistics PC5]
    LOG --> O2C[Order-to-Cash PC5]
    O2C --> PAY[Cards & Payments PC5]
    P2P --> R2R[Record-to-Report PC5]
    O2C --> R2R
    PAY --> R2R
```

The arrows here express implementation leverage and intended ownership boundaries, not direct imports.

### Why Warehouse precedes P2P/O2C composition

P2P and O2C currently contain useful standalone inventory/fulfillment behavior. Before composed mode can be introduced, Warehouse ownership must be explicit enough that those domains can hand work off without mutating Warehouse-private state.

### Why R2R comes last in the PC5 backbone

R2R consumes accounting consequences from other processes. Its standalone close/reconciliation behavior can mature independently, but the useful cross-domain contract should be designed after P2P/O2C/Payments produce stable accounting outputs.

## Target runtime composition: trading company

This is the first intended PC6 composition.

```mermaid
flowchart LR
    Demand[Customer Demand] --> O2C[Order-to-Cash]
    O2C -->|FulfillmentRequest| WF[Warehouse Fulfillment]
    WM[Warehouse Management] -->|Inventory Available| WF
    WF -->|DispatchReady| LOG[Logistics]
    LOG -->|Delivered| O2C
    O2C -->|PaymentRequest| PAY[Cards & Payments]
    PAY -->|PaymentSettled| O2C

    WM -->|Replenishment Need| P2P[Procure-to-Pay]
    P2P -->|InventoryReceipt| WM

    P2P -->|Accounting Output| R2R[Record-to-Report]
    O2C -->|Accounting Output| R2R
    PAY -->|Accounting Output| R2R
```

## Target runtime composition: manufacturing + MRO

```mermaid
flowchart LR
    PROD[Manufacturing] -->|Material Requirement| WM[Warehouse Management]
    WM -->|Unavailable| P2P[Procure-to-Pay]
    P2P -->|Receipt| WM
    WM -->|Material Available| PROD
    PROD -->|Finished Goods| WM

    PROD -->|Equipment Failure| MRO[MRO]
    MRO -->|Spare Requirement| WM
    WM -->|Spare Available| MRO
    MRO -->|Asset Restored| PROD
```

## Ownership rules for graph edges

Every cross-domain edge must preserve these rules:

1. **Producer owns its durable internal entity.**
2. **Consumer receives a contract payload, not a mutable entity reference.**
3. **Correlation and causation survive persistence/restart.**
4. **Consumption is idempotent.**
5. **Retries cannot duplicate business effects.**
6. **The same business fact has one durable owner.**
7. **A projection may combine domains but does not become a source of truth.**

## Standalone versus composed mode

Promotion to PC6 must not destroy standalone demonstrations.

Example for O2C:

```text
Standalone:
credit -> local fulfillment -> invoice -> receivable -> collection

Composed:
credit -> FulfillmentRequest
       -> wait FulfillmentCompleted
       -> invoice
       -> PaymentRequest
       -> wait PaymentSettled
```

Example for P2P:

```text
Standalone:
PO -> supplier lead time -> receive -> inspect -> stock -> consume

Composed:
PO -> supplier lead time -> receive -> inspect -> InventoryReceipt
Warehouse Management -> putaway -> StockAvailable
```

The composed adapter replaces local ownership at the boundary; it does not duplicate it.

## Promotion gates

A downstream wave may start design work early, but it should not be promoted/merged as the architectural reference until its dependency gate is satisfied.

| Downstream work | Required gate |
|---|---|
| Catalog-wide domain promotion | W0 merged |
| Warehouse reference promotion | W1 evidence audit available |
| Cross-domain implementation | W3 contract defined |
| Trading Company PC6 | W4 participating domains PC5 |
| Manufacturing/MRO integration | Trading backbone composition demonstrated |
| Organizational Dynamics domain experiments | At least one credible PC6 composition |
| Digital Twin reference UI | PC5 projection contract, preferably PC6 for multi-domain view |

## What is deliberately not a dependency

The following must not block process completion:

- a 3D UI;
- Organizational Dynamics;
- empirical calibration of human agency;
- a generic enterprise ontology;
- a universal process DSL;
- a generic business-object hierarchy.

These may emerge later from mature process evidence.

## Next graph transition

Current transition:

```text
Existing kernel
    -> W0 Process Canonical Contract
    -> W1 Catalog-wide Evidence Audit
```

No new business domain should be selected for deep promotion until W1 has converted the current catalog into an evidence-backed gap matrix.
