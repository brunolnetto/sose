# Field Service / Workforce Reference

## Status

**Reference implementation.**

This Reference is grounded primarily in TM Forum Appointment Management (TMF646),
which defines an Appointment as an arrangement for provider personnel to perform
activity at a particular time and place. The executable slice also follows the
field-service concern raised in TM Forum implementation discussions: technician
selection may depend on required skills, availability, and location.

The slice proves a distinction SOSE did not previously exercise strongly:

- future appointment ownership is domain semantics;
- technician eligibility is multi-dimensional domain logic;
- actual technician execution capacity remains a core Resource lifecycle;
- a required physical part uses durable Store selection ownership;
- a no-access visit is immutable evidence and causes a new appointment rather
  than rewriting the failed appointment.

## Executable slice

One installation WorkOrder requires:

- skill: `fiber-installation`;
- territory: `west`;
- one `ont-router`;
- one customer appointment window.

Two technicians are seeded. Only one has the required skill/territory. The
qualified technician is booked into the appointment window. An overlapping
appointment cannot silently reuse that technician.

The happy path confirms the appointment, reserves the required part, acquires
the selected technician when the appointment starts, records one immutable
successful VisitOccurrence, and completes the WorkOrder.

The sad/recovery path records a no-access visit, closes the first appointment,
moves the WorkOrder to `reschedule_required`, creates a replacement
appointment, and finishes on the replacement without duplicating the part
selection.

## Why there is no QualifiedResource core primitive

Eligibility answers *which technician is semantically legal* for this job.
The durable Resource manager answers *whether the already-selected technician
capacity can be acquired/released safely*.

One domain is not enough evidence to freeze skill/territory matching into core.
A future domain should repeat the same nontrivial contract before extraction.

## References

- TMF646 Appointment Management API: https://www.tmforum.org/open-digital-architecture/open-apis/appointment-management-api-TMF646/v4.0
- TMF646 v5 preview: https://www.tmforum.org/open-digital-architecture/open-apis/appointment-management-api-TMF646/v5.0
- TM Forum field appointment/skills discussion: https://engage.tmforum.org/discussion/field-api-646

## Stopping rule

The Reference stops once tests prove:

- skill + territory + window technician eligibility;
- durable appointment-window ownership;
- resource acquisition only after domain selection;
- part selection ownership;
- immutable visit outcome;
- no-access rescheduling with lineage;
- restart equivalence across the replacement appointment.

Routing optimization, travel-time matrices, workforce rostering, SLA
optimization, and mobile UX remain breadth until they expose a new architecture
question.
