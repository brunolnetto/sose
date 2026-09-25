# Factory architecture

Factories are a SOSE architectural boundary, not convenience wrappers.

They keep domain packs focused on business intent while centralizing the metadata
required for deterministic replay, auditability, process mining and persistence.

## EntityFactory

Creates stable operational identity from a semantic key:

```text
entity_type + business key -> deterministic entity id
```

It also owns creation timestamps and initial technical metadata.

## CommandFactory

Commands are intentions. The factory adds:

- deterministic command identity;
- issue time;
- simulation tick;
- target identity;
- causation;
- correlation.

A command may later be rescheduled without changing the identity of the intent.

## EventFactory

Events are immutable facts. All domain events pass through this factory so they
share one causal metadata model.

Specialized transition events use:

```text
entity.state_transition
```

with `trigger`, `from_state` and `to_state`, which provides a standard event-log
surface for process mining across every domain pack.

## ScheduleFactory

Scheduling is defined against logical simulation time. The factory deliberately
hides heap/priority-queue mechanics from domain code.

Future extensions belong here rather than in statecharts:

- business calendars;
- shifts;
- holidays;
- SLA clocks;
- capacity-aware delays;
- persisted schedules.

## StateChartFactory

The factory does not generate business lifecycle definitions. It binds an entity
to the registered statechart implementation and attaches runtime listeners.

This preserves the boundary:

```text
statechart definition -> domain semantics
statechart binding    -> SOSE runtime infrastructure
```

The integration is imported lazily so kernel/factory/persistence code can be
used and tested independently from a particular statechart backend.
