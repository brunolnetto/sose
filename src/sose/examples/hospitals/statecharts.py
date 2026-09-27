from statemachine import State, StateChart

from sose.probability import probabilistic_transitions


@probabilistic_transitions(
    {},
    excluded_events={
        "triage",
        "wait_bed",
        "allocate_bed",
        "start_treatment",
        "deteriorate",
        "allocate_icu",
        "ready_discharge",
        "discharge",
        "transfer",
    },
)
class AdmissionChart(StateChart):
    admitted = State(initial=True)
    triaged = State()
    waiting_bed = State()
    bed_allocated = State()
    treatment = State()
    waiting_icu = State()
    icu = State()
    discharge_ready = State()
    discharged = State(final=True)
    transferred = State(final=True)

    triage = admitted.to(triaged)
    wait_bed = triaged.to(waiting_bed)
    allocate_bed = waiting_bed.to(bed_allocated)
    start_treatment = bed_allocated.to(treatment)
    deteriorate = bed_allocated.to(waiting_icu) | treatment.to(waiting_icu)
    allocate_icu = waiting_icu.to(icu)
    ready_discharge = treatment.to(discharge_ready) | icu.to(discharge_ready)
    discharge = discharge_ready.to(discharged)
    transfer = waiting_bed.to(transferred) | waiting_icu.to(transferred)


@probabilistic_transitions(
    {},
    excluded_events={
        "queue",
        "start",
        "interrupt",
        "resume",
        "complete",
        "cancel",
    },
)
class TreatmentEpisodeChart(StateChart):
    planned = State(initial=True)
    waiting_capacity = State()
    in_progress = State()
    interrupted = State()
    completed = State(final=True)
    cancelled = State(final=True)

    queue = planned.to(waiting_capacity)
    start = waiting_capacity.to(in_progress)
    interrupt = in_progress.to(interrupted)
    resume = interrupted.to(in_progress)
    complete = in_progress.to(completed)
    cancel = (
        planned.to(cancelled)
        | waiting_capacity.to(cancelled)
        | interrupted.to(cancelled)
    )
