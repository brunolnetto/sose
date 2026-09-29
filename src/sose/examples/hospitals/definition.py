from datetime import datetime
from sose.domain.config import DomainDefinition
from sose.persistence.base import Persistence
from .config import HospitalsConfig
from .patient_flow import (
    claim_next_ward_admission,
    discharge_from_ward,
    triage_and_queue,
)
from .runtime import build_runtime, seed_reference

def _build(persistence: Persistence, config: HospitalsConfig, now: datetime, tick: int):
    return build_runtime(persistence, now=now, tick=tick, step=config.tick_step, random_seed=config.random_seed)

def _seed(persistence: Persistence, config: HospitalsConfig):
    return seed_reference(
        persistence,
        now=config.start_at,
        acuity=config.acuity,
        service=config.service,
        ward_bed_capacity=config.ward_bed_capacity,
        icu_bed_capacity=config.icu_bed_capacity,
        clinical_team_capacity=config.clinical_team_capacity,
        procedure_suite_capacity=config.procedure_suite_capacity,
        triage_queue_capacity=config.triage_queue_capacity,
    )

def _reconcile_tick(persistence, engine, backend, config, entities):
    if not config.auto_progress_patient_flow:
        return
    admission = persistence.entity("hospital_admission", entities.admission_id)
    if admission is None:
        raise RuntimeError("configured admission was not persisted")
    claim_id = f"job:{entities.admission_id}"

    if admission.state == "admitted":
        triage_and_queue(
            persistence,
            engine,
            backend,
            admission_id=admission.id,
        )
        admission = persistence.entity("hospital_admission", admission.id)

    if admission is not None and admission.state == "waiting_bed":
        claim_next_ward_admission(
            persistence,
            engine,
            backend,
            claim_id=claim_id,
        )
        admission = persistence.entity("hospital_admission", admission.id)

    if admission is not None and admission.state == "treatment":
        discharge_from_ward(
            persistence,
            engine,
            backend,
            admission_id=admission.id,
            claim_id=claim_id,
        )

definition = DomainDefinition(
    name="hospitals",
    description="Hospital patient-flow and procedure-capacity reference domain.",
    config_model=HospitalsConfig,
    build_runtime=_build,
    seed=_seed,
    reconcile_tick=_reconcile_tick,
    runtime_mutable_fields=frozenset(["tick_step","random_seed","auto_progress_patient_flow"]),
)
