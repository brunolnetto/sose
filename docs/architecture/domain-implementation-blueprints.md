# Domain Implementation Blueprints

## Purpose

SOSE is a strong fit for operational domains that combine:

- persistent entities;
- explicit state machines;
- commands and immutable domain events;
- causal relationships across entities;
- queues, contention, capacity, inventory or quantitative constraints;
- exceptions, retries, reversals and rework;
- probabilistic behavior;
- external scenarios and interventions;
- logical time and durable recovery.

This document defines **implementation blueprints** for applying SOSE to common
operational domains. It is architectural documentation, not a claim that every
domain below already has production code in the repository.

The current reference implementation is MRO under:

```text
src/sose/examples/mro/
```

The remaining domains describe how an implementation should map domain concepts
onto the same SOSE kernel.

---

## Canonical SOSE mapping

A domain implementation should normally decompose into the following layers.

```text
Persistent entity
    ↓
StateChart
    = legal lifecycle

ProbabilisticTransitionGraph
    = stochastic choice among legal transitions

Command
    = explicit intent

DomainEvent
    = immutable business fact

DurableScheduler
    = future execution intent

Scenario Engine
    = external conditions / interventions

Resource / Store / Container backend
    = ephemeral execution mechanics

Persistence
    = durable semantic truth
```

### Typical primitive selection

| Operational concept | SOSE primitive |
|---|---|
| Entity lifecycle | StateChart |
| Legal transition | StateChart event |
| Probabilistic branch | ProbabilisticTransitionGraph |
| Delayed work / SLA deadline | DurableScheduler |
| Human/machine capacity | Resource / PreemptiveResource |
| FIFO queue | Store |
| Priority queue | PriorityStore |
| Selective queue | FilterStore |
| Inventory / fluid / quantity | Container or domain entity + events |
| External disruption | Scenario Engine |
| Immutable audit trail | DomainEvent |
| Cross-entity cause | causation_id / correlation_id |
| Restart-safe progress | Persistence + SimulationPosition |

---

## Applicability matrix

| Domain | Canonical lifecycle | Dominant SOSE mechanics |
|---|---|---|
| Logistics & transport | order → pickup → hub → transfer → delivery → return | resources, queues, SLA, scenarios |
| E-commerce & retail | cart → order → payment → picking → shipment → return | statecharts, inventory, fraud, refunds |
| Supply chain | demand → plan → requisition → PO → receipt → consumption | lead time, shortage, supplier scenarios |
| Manufacturing | production order → setup → production → inspection → rework | resources, downtime, scrap, queues |
| Maintenance / MRO | asset/work order → execution → completion | resources, materials, failures |
| Banking | account/transaction → authorization → settlement → reconciliation | causality, reversals, fraud |
| Cards & payments | authorization → capture → settlement → dispute → chargeback | retries, reversals, dispute lifecycle |
| Credit & loans | application → analysis → approval → disbursement → collection | decisions, delinquency, renegotiation |
| Insurance | policy/claim → analysis → approval → payment | documents, fraud, reopen |
| Healthcare operations | patient → consultation → exam → procedure → discharge | queues, capacity, authorization |
| Hospitals | admission → triage → bed → treatment → discharge | scarce resources, transfers, waiting |
| Pharma / clinical supply | batch → production → QC → release → distribution → recall | quality gates, expiry, cold chain |
| Telecom | customer → activation → usage → billing → payment → suspension | churn, failures, billing |
| Utilities | consumer → metering → billing → payment → collection | estimated readings, disconnect/reconnect |
| Energy | asset → generation → dispatch → maintenance | capacity, availability, constraints |
| Oil & Gas | production → storage → transport → metering → sale | containers, inspection, incidents |
| Aviation | aircraft → flight → inspection → maintenance → release | preemption, AOG, parts, crew |
| Airports | flight → gate → baggage → turnaround → departure | queues, gates, congestion |
| Rail / public transport | vehicle → route → trip → incident → maintenance | headway, capacity, disruptions |
| SaaS | account → subscription → usage → invoice → renewal/churn | billing cycles, retries, churn |
| ITSM | incident → triage → assignment → resolution → closure | priority queues, SLA, escalation |
| DevOps / platform | deployment → validation → release → rollback | scheduled work, retries, incidents |
| Cybersecurity | event → alert → investigation → containment → remediation | correlation, escalation, queues |
| Fraud detection | transaction → scoring → review → approve/block → dispute | probabilistic rules, review queues |
| Government workflows | request → analysis → approval → licensing → inspection | documents, appeals, SLA |
| Construction | activity → material demand → procurement → execution → inspection | dependencies, resources, rework |
| Real estate | lead → visit → proposal → contract → payment → handover | dropout, financing, renegotiation |
| Education | enrollment → course → assessment → completion | prerequisites, retake, dropout |
| HR / recruiting | candidate → screening → interview → offer → hire | queues, no-show, withdrawal |
| Order-to-Cash | quote → order → fulfillment → invoice → collection | ERP causality, credit, fulfillment |
| Procure-to-Pay | requisition → PO → receipt → invoice → payment | approvals, suppliers, three-way match |
| Record-to-Report | transaction → posting → reconciliation → close | accounting periods, exceptions |

---

# Domain blueprints

## 1. Logistics and transport

### Core entities

```text
Shipment
Order
Package
Vehicle
Route
Hub
DeliveryAttempt
```

### Canonical lifecycle

```text
created
→ pickup_scheduled
→ picked_up
→ at_origin_hub
→ in_transfer
→ at_destination_hub
→ out_for_delivery
→ delivered

exception branches:
→ delayed
→ failed_delivery
→ reattempt
→ lost
→ damaged
→ returned
```

### SOSE mechanics

- StateCharts for Shipment and DeliveryAttempt.
- PriorityResource for docks, vehicles and couriers.
- Store/PriorityStore for hub queues.
- DurableScheduler for pickup windows, transfer departure and SLA deadlines.
- Scenario Engine for congestion, strikes, weather and capacity loss.
- Causal chains linking shipment, package, route and delivery-attempt events.

---

## 2. E-commerce and retail

### Core entities

```text
Customer
Cart
Order
Payment
Fulfillment
Shipment
Return
Refund
InventoryPosition
```

### Canonical lifecycle

```text
cart
→ submitted
→ payment_authorized
→ allocated
→ picking
→ packed
→ shipped
→ delivered

exception branches:
→ payment_declined
→ cancelled
→ stockout
→ fraud_review
→ returned
→ refunded
```

### SOSE mechanics

- Order StateChart as the orchestration backbone.
- Container or inventory entities for stock.
- FilterStore for SKU-specific picking queues.
- ProbabilisticTransitionGraph for cancellation, fraud and return behavior.
- Scenarios for demand spikes, stock shortages and carrier degradation.

---

## 3. Supply chain

### Core entities

```text
Demand
Plan
Requisition
PurchaseOrder
SupplierOrder
Receipt
InventoryPosition
Consumption
```

### Canonical lifecycle

```text
demand_detected
→ planned
→ requisitioned
→ ordered
→ supplier_confirmed
→ in_transit
→ received
→ stocked
→ consumed

exception branches:
→ shortage
→ backorder
→ late_supplier
→ partial_receipt
→ rejected_receipt
```

### SOSE mechanics

- Durable schedules for lead times and reorder dates.
- Containers for quantitative stock.
- Supplier capacity represented as resources.
- Scenario effects for supplier outages, lead-time inflation and demand shocks.
- Correlation across demand → requisition → PO → receipt.

---

## 4. Manufacturing

### Core entities

```text
ProductionOrder
Operation
Machine
MaterialLot
Inspection
ReworkOrder
```

### Canonical lifecycle

```text
planned
→ released
→ setup
→ producing
→ inspection
→ completed

exception branches:
→ waiting_material
→ machine_down
→ quality_hold
→ rework
→ scrap
```

### SOSE mechanics

- Resource/PreemptiveResource for machines and labor.
- PriorityStore for work-center queues.
- Containers for WIP or bulk material.
- Scenarios for downtime, yield degradation and maintenance windows.
- Probabilistic transitions for quality outcomes and scrap/rework.

---

## 5. Maintenance industrial / MRO

MRO is the current reference domain in the repository.

### Core entities

```text
Asset
WorkOrder
Technician
MaterialDemand
InventoryPosition
PurchaseOrder
Supplier
Inspection
```

### Canonical lifecycle

```text
planned
→ released
→ in_progress
→ completed
→ closed

exception branches:
→ waiting_material
→ waiting_resource
→ cancelled
→ reopened
```

### SOSE mechanics

- StateChart implementation already exists for WorkOrder.
- Durable resources model technicians/bays.
- Scenario Engine can alter availability and transition weights.
- Resource demand/reservation/release intent is durable.
- SimPy remains execution-only and reconstructible.

---

## 6. Banking

### Core entities

```text
Customer
Account
Transaction
Authorization
Settlement
ReconciliationItem
FraudCase
```

### Canonical lifecycle

```text
initiated
→ authorized
→ posted
→ settled
→ reconciled

exception branches:
→ declined
→ held
→ reversed
→ blocked
→ fraud_review
```

### SOSE mechanics

- Strong correlation IDs across transaction lifecycle.
- Immutable events for auditability.
- Durable scheduling for settlement/reconciliation windows.
- Priority review queues for suspicious transactions.
- Scenarios for liquidity constraints or service outages.

---

## 7. Cards and payments

### Core entities

```text
Payment
Authorization
Capture
Settlement
Refund
Dispute
Chargeback
```

### Canonical lifecycle

```text
authorization_requested
→ authorized
→ captured
→ settled

exception branches:
→ declined
→ retry
→ reversed
→ refunded
→ disputed
→ chargeback
```

### SOSE mechanics

- Probabilistic decline/retry/fraud paths.
- Durable retry schedules.
- Causal event chains from authorization through settlement.
- Dispute and chargeback modeled as independent persistent entities correlated to Payment.

---

## 8. Credit and loans

### Core entities

```text
LoanApplication
CreditDecision
Loan
Installment
DelinquencyCase
CollectionCase
```

### Canonical lifecycle

```text
submitted
→ under_analysis
→ approved
→ disbursed
→ servicing
→ paid_off

exception branches:
→ rejected
→ delinquent
→ renegotiated
→ defaulted
→ collection
```

### SOSE mechanics

- Probabilistic approval and delinquency behavior.
- Scheduled installments and collection actions.
- Scenario Engine for macroeconomic stress.
- Durable causal chain from application to servicing events.

---

## 9. Insurance

### Core entities

```text
Policy
InsuredEvent
Claim
DocumentRequest
Assessment
Payment
FraudInvestigation
```

### Canonical lifecycle

```text
claim_opened
→ documentation
→ analysis
→ approved
→ payment_scheduled
→ paid

exception branches:
→ rejected
→ fraud_review
→ pending_documents
→ reopened
```

### SOSE mechanics

- FilterStore for specialist queues by claim type.
- Priority queues for severity.
- Scheduled document deadlines and payment dates.
- Scenarios for catastrophe-driven claim surges.

---

## 10. Healthcare operations

### Core entities

```text
Patient
Appointment
Exam
Procedure
Authorization
Admission
BillingCase
```

### Canonical lifecycle

```text
scheduled
→ checked_in
→ consultation
→ exam/procedure
→ completed
→ billed

exception branches:
→ no_show
→ rescheduled
→ authorization_pending
→ readmitted
```

### SOSE mechanics

- Resources for doctors, rooms and equipment.
- Priority queues for urgency.
- Scheduled appointments and deadlines.
- Scenario Engine for demand peaks and capacity loss.

---

## 11. Hospitals

### Core entities

```text
Admission
TriageCase
BedStay
TreatmentEpisode
Transfer
Discharge
```

### Canonical lifecycle

```text
admitted
→ triaged
→ waiting_bed
→ bed_allocated
→ treatment
→ discharge_ready
→ discharged

exception branches:
→ transfer
→ deterioration
→ ICU
→ readmission
```

### SOSE mechanics

- PreemptiveResource where clinical policy permits emergency prioritization.
- Resource for beds and clinical teams.
- PriorityStore for triage queues.
- Scenarios for occupancy surges and unit closures.

---

## 12. Pharma / clinical supply

### Core entities

```text
Batch
MaterialLot
QualityControl
ReleaseDecision
Shipment
Recall
```

### Canonical lifecycle

```text
planned
→ produced
→ quality_control
→ released
→ distributed

exception branches:
→ quarantined
→ rejected
→ expired
→ recalled
```

### SOSE mechanics

- Durable time for expiry and QC windows.
- Containers for bulk quantities.
- StateCharts for batch release and recall.
- Scenarios for cold-chain excursion or supplier quality incidents.

---

## 13. Telecom

### Core entities

```text
Customer
Subscription
Activation
UsageSession
Invoice
Payment
NetworkIncident
```

### Canonical lifecycle

```text
ordered
→ activated
→ active
→ billed
→ paid
→ renewed

exception branches:
→ activation_failed
→ delinquent
→ suspended
→ churned
```

### SOSE mechanics

- Billing cycles through DurableScheduler.
- Probabilistic churn.
- Scenarios for network failure or billing disruption.
- Causal correlation between incidents, usage impact and support cases.

---

## 14. Utilities

### Core entities

```text
ConsumerAccount
Meter
Reading
Bill
Payment
CollectionCase
ServiceConnection
```

### Canonical lifecycle

```text
metered
→ billed
→ due
→ paid

exception branches:
→ estimated_reading
→ overdue
→ collection
→ disconnected
→ reconnected
```

### SOSE mechanics

- Periodic schedules for readings and bills.
- Scenario Engine for meter failures and tariff changes.
- Resource queues for field-service crews.

---

## 15. Energy

### Core entities

```text
GenerationAsset
DispatchInstruction
GenerationInterval
Outage
MaintenanceWork
MeterReading
```

### Canonical lifecycle

```text
available
→ dispatched
→ generating
→ measured

exception branches:
→ constrained
→ derated
→ unavailable
→ maintenance
```

### SOSE mechanics

- Containers for energy quantities where appropriate.
- Resources for constrained generation capacity.
- Scenario effects for demand, weather and asset outage.
- Durable schedules for dispatch intervals.

---

## 16. Oil & Gas

### Core entities

```text
Well
ProductionBatch
Tank
PipelineMovement
Inspection
Measurement
Sale
Incident
```

### Canonical lifecycle

```text
produced
→ stored
→ transferred
→ measured
→ sold

exception branches:
→ quality_hold
→ leak
→ inspection_hold
→ shutdown
```

### SOSE mechanics

- Container for tanks and bulk flows.
- Resource constraints for pipelines/loading points.
- Scenarios for shutdowns and quality events.
- Durable inspection and maintenance schedules.

---

## 17. Aviation

### Core entities

```text
Aircraft
Flight
Turnaround
Inspection
MaintenanceWorkOrder
PartDemand
CrewAssignment
```

### Canonical lifecycle

```text
scheduled
→ boarding/turnaround
→ ready
→ airborne
→ landed
→ inspection
→ released

exception branches:
→ delayed
→ AOG
→ maintenance
→ part_wait
→ crew_wait
```

### SOSE mechanics

- Preemptive resources for AOG-critical maintenance.
- Priority stores for parts/work queues.
- Durable schedules for flight and turnaround deadlines.
- Scenario Engine for weather and airport disruption.

---

## 18. Airports

### Core entities

```text
FlightTurnaround
GateAssignment
BaggageFlow
GroundServiceTask
DepartureSlot
```

### Canonical lifecycle

```text
arrival
→ gate_assigned
→ deboarding
→ servicing
→ boarding
→ pushback
→ departure

exception branches:
→ gate_hold
→ baggage_delay
→ slot_delay
→ gate_reallocation
```

### SOSE mechanics

- Resources for gates and service teams.
- PriorityStore for runway/gate queues.
- Scenarios for congestion and weather.

---

## 19. Rail / public transport

### Core entities

```text
Vehicle
Trip
Route
StationCall
Incident
MaintenanceWork
```

### Canonical lifecycle

```text
scheduled
→ dispatched
→ en_route
→ station_call
→ completed

exception branches:
→ delayed
→ short_turned
→ failed
→ maintenance
```

### SOSE mechanics

- Durable timetable scheduling.
- Resource contention for tracks/platforms.
- Scenarios for incidents and congestion.
- Headway measured from immutable events.

---

## 20. SaaS / software products

### Core entities

```text
Account
Subscription
UsagePeriod
Invoice
Payment
Renewal
```

### Canonical lifecycle

```text
trial
→ active
→ invoiced
→ paid
→ renewed

exception branches:
→ payment_failed
→ grace_period
→ downgraded
→ suspended
→ churned
```

### SOSE mechanics

- Recurring durable schedules.
- Probabilistic churn/upgrade/downgrade.
- Retry policies for failed payments.
- Scenario Engine for pricing or product interventions.

---

## 21. IT service management

### Core entities

```text
Incident
Assignment
Escalation
Problem
Change
SLA
```

### Canonical lifecycle

```text
opened
→ triaged
→ assigned
→ in_progress
→ resolved
→ closed

exception branches:
→ escalated
→ SLA_breached
→ reopened
```

### SOSE mechanics

- PriorityStore for severity queues.
- Resources for support teams.
- DurableScheduler for SLA milestones.
- Scenarios for incident storms and staff shortages.

---

## 22. DevOps / platform operations

### Core entities

```text
Deployment
Validation
Release
Rollback
Environment
Incident
```

### Canonical lifecycle

```text
planned
→ deploying
→ validating
→ released

exception branches:
→ failed
→ retrying
→ rolled_back
→ incident_opened
```

### SOSE mechanics

- Durable schedules for rollout stages and retries.
- Resource constraints for environments/deployment slots.
- Causal chains from deployment to incident and rollback.
- Probabilistic failure injection through scenarios.

---

## 23. Cybersecurity

### Core entities

```text
SecurityEvent
Alert
Investigation
Incident
ContainmentAction
RemediationTask
```

### Canonical lifecycle

```text
event_detected
→ alert_opened
→ triaged
→ investigated
→ contained
→ remediated
→ closed

exception branches:
→ false_positive
→ escalated
→ reopened
```

### SOSE mechanics

- Correlation across multiple alerts into incidents.
- Priority queues by severity.
- Resources for analyst capacity.
- Scenarios for attack waves or telemetry loss.

---

## 24. Fraud detection

### Core entities

```text
Transaction
RiskScore
ReviewCase
Decision
Dispute
```

### Canonical lifecycle

```text
submitted
→ scored
→ approved

or

submitted
→ scored
→ manual_review
→ approved/blocked

post-decision:
→ disputed
```

### SOSE mechanics

- Probabilistic adversarial and customer behavior.
- Priority review queues.
- Scenario-driven shifts in fraud prevalence.
- Immutable causal chain from score to decision.

---

## 25. Government workflows

### Core entities

```text
Request
Case
DocumentSubmission
Review
Approval
License
Inspection
Appeal
```

### Canonical lifecycle

```text
submitted
→ completeness_check
→ analysis
→ approved
→ licensed
→ inspected

exception branches:
→ pending_documents
→ rejected
→ appealed
→ reopened
```

### SOSE mechanics

- Durable deadlines.
- FilterStore for specialized case routing.
- Resource queues for analysts/inspectors.
- Audit-friendly immutable events.

---

## 26. Construction

### Core entities

```text
Activity
MaterialDemand
PurchaseOrder
CrewAssignment
Inspection
Measurement
Rework
```

### Canonical lifecycle

```text
planned
→ ready
→ executing
→ inspected
→ measured
→ completed

exception branches:
→ blocked_dependency
→ material_shortage
→ delayed
→ rework
```

### SOSE mechanics

- Resource constraints for crews/equipment.
- Supply-chain subflows for materials.
- Durable schedules for dependencies and milestones.
- Scenario effects for weather, productivity and procurement delays.

---

## 27. Real estate

### Core entities

```text
Lead
Visit
Proposal
Contract
FinancingCase
Payment
Handover
```

### Canonical lifecycle

```text
lead
→ qualified
→ visit
→ proposal
→ contract
→ payment
→ handover

exception branches:
→ lost
→ withdrawn
→ renegotiation
→ financing_rejected
```

### SOSE mechanics

- Probabilistic conversion/dropout.
- Scheduled visits and payment dates.
- Financing as correlated sub-lifecycle.

---

## 28. Education

### Core entities

```text
Student
Enrollment
CourseAttempt
Assessment
Completion
```

### Canonical lifecycle

```text
enrolled
→ active
→ assessed
→ passed
→ completed

exception branches:
→ prerequisite_block
→ failed
→ retake
→ dropout
```

### SOSE mechanics

- Durable academic-period schedules.
- StateCharts for enrollment and course attempts.
- Probabilistic dropout/retake.
- Resources for limited seats/labs.

---

## 29. HR / recruiting

### Core entities

```text
Candidate
Application
Screening
Interview
Offer
Hire
```

### Canonical lifecycle

```text
applied
→ screened
→ interview
→ offer
→ accepted
→ hired

exception branches:
→ rejected
→ withdrawn
→ no_show
→ offer_declined
```

### SOSE mechanics

- Resources for interviewers.
- Scheduled interviews and offer deadlines.
- Probabilistic withdrawal/no-show/acceptance.
- Correlated candidate/application events.

---

## 30. Order-to-Cash

### Core entities

```text
Quote
SalesOrder
Fulfillment
Shipment
Invoice
Receivable
CollectionCase
```

### Canonical lifecycle

```text
quoted
→ ordered
→ fulfilled
→ shipped
→ invoiced
→ collected

exception branches:
→ credit_hold
→ partial_fulfillment
→ overdue
→ dispute
```

### SOSE mechanics

- Correlation across commercial and financial entities.
- Durable payment/collection schedules.
- Inventory/resource mechanics for fulfillment.
- Scenario Engine for demand, credit and logistics disruption.

---

## 31. Procure-to-Pay

The concrete v0.8 reference-domain contract is documented under
[`docs/examples/procure-to-pay/`](../examples/procure-to-pay/README.md).

### Core entities

```text
Requisition
PurchaseOrder
Receipt
SupplierInvoice
MatchResult
Payable
Payment
```

### Canonical lifecycle

```text
requested
→ approved
→ ordered
→ received
→ invoiced
→ matched
→ payable
→ paid

exception branches:
→ rejected
→ partial_receipt
→ mismatch
→ invoice_hold
```

### SOSE mechanics

- Strong fit with the MRO procurement subdomain.
- Durable supplier lead times.
- Causal linkage through requisition → PO → receipt → invoice.
- Filter/priority queues for exception handling.

---

## 32. Record-to-Report

### Core entities

```text
AccountingTransaction
JournalEntry
ReconciliationItem
Adjustment
CloseTask
AccountingPeriod
```

### Canonical lifecycle

```text
transaction
→ posting
→ reconciliation
→ adjustment_if_needed
→ close_ready
→ closed

exception branches:
→ unmatched
→ rejected_posting
→ adjustment_required
→ reopened_period
```

### SOSE mechanics

- Durable accounting calendars and close schedules.
- StateCharts for reconciliation and close tasks.
- Immutable audit events.
- Resource queues for manual exception resolution.

---

# Cross-domain implementation patterns

## Pattern A — transactional lifecycle

Best suited to banking, cards, fraud, SaaS billing and ERP flows.

```text
intent
→ authorization
→ execution
→ settlement
→ reconciliation
```

Key SOSE mechanisms:

- commands;
- immutable events;
- causation/correlation;
- durable retry schedules;
- reversal StateChart paths.

## Pattern B — physical flow network

Best suited to logistics, airports, rail, manufacturing and construction.

```text
entity
→ queue
→ constrained resource
→ transformation/movement
→ next queue
```

Key mechanisms:

- Store/PriorityStore;
- Resource/PreemptiveResource;
- DurableScheduler;
- Scenario-driven capacity disruption.

## Pattern C — inventory and replenishment

Best suited to supply chain, MRO, retail, pharma and Oil & Gas.

```text
demand
→ shortage detection
→ replenishment
→ receipt
→ stock
→ consumption
```

Key mechanisms:

- Container or durable inventory entities;
- supplier StateCharts;
- lead-time schedules;
- shortage scenarios.

## Pattern D — case management

Best suited to insurance, government, ITSM, cybersecurity and HR.

```text
case opened
→ triage
→ assignment
→ work
→ decision
→ closure
```

Key mechanisms:

- persistent case entity;
- PriorityStore / FilterStore;
- SLA schedules;
- escalation transitions.

## Pattern E — capacity-constrained service

Best suited to hospitals, healthcare, aviation and field services.

```text
arrival
→ queue
→ resource acquisition
→ service
→ release
```

Key mechanisms:

- Resource / PreemptiveResource;
- queue ordering;
- service-time scheduling;
- occupancy scenarios.

---

# Implementation status convention

Domain documentation should use the following status vocabulary.

| Status | Meaning |
|---|---|
| Reference implementation | Executable example exists in the repository |
| Blueprint | Formal architecture documented but no complete executable vertical slice |
| Partial | Some reusable subflows exist through another domain |
| Experimental | Prototype exists but is not yet part of the stable example surface |

At the time of this document:

- **Maintenance / MRO**: Reference implementation.
- **Procure-to-Pay**: Partial, with a concrete v0.8 reference-domain contract under implementation.
- All other domains in this document: Blueprint.

---

# Requirements for promoting a blueprint to an implementation

A domain should not be called implemented until it has, at minimum:

1. persistent entity types;
2. one or more StateCharts;
3. explicit commands and domain events;
4. deterministic identities and causal metadata;
5. at least one time-dependent workflow;
6. representative exception paths;
7. tests for legal/illegal transitions;
8. restart-equivalence coverage when durable scheduling/resources are used;
9. at least one scenario intervention;
10. documentation describing semantic truth versus backend execution mechanics.

A stronger reference implementation should additionally include:

- resource/queue contention;
- probabilistic behavior;
- cross-entity correlation;
- failure/retry/reversal semantics;
- deterministic replay/restart tests;
- a compact end-to-end example.

---

# Architectural constraint

Across every domain, the same rule applies:

```text
domain semantics are durable
backend execution state is ephemeral
```

Do not persist backend-native objects such as:

- SimPy Environment;
- SimPy Process;
- native request/event queues;
- Python generator continuation state.

Persist the semantic intent required to reconstruct those mechanics instead.
