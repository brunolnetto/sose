from datetime import datetime

from sose.domain.config import DomainDefinition
from sose.persistence.base import Persistence

from .config import CreditLoansConfig
from .simulation import (
    build_runtime,
    cure_delinquency,
    disburse_and_schedule,
    installment_id,
    loan_id,
    post_payment,
    reconcile_collection,
    reconcile_underwriting,
    schedule_overdue,
    seed_reference,
)


def _build(
    persistence: Persistence,
    config: CreditLoansConfig,
    now: datetime,
    tick: int,
):
    return build_runtime(
        persistence,
        now=now,
        tick=tick,
        step=config.tick_step,
        random_seed=config.random_seed,
    )


def _seed(persistence: Persistence, config: CreditLoansConfig):
    return seed_reference(
        persistence,
        now=config.start_at,
        principal=config.principal,
        installment_count=config.installment_count,
    )

def _application_or_error(persistence, entities):
    application = persistence.entity(
        "loan_application",
        entities.application_id,
    )
    if application is None:
        raise RuntimeError("loan application was not persisted")
    return application


def _remaining_installment_amount(installment) -> float:
    return float(installment.attributes["amount"]) - float(
        installment.attributes["paid_amount"]
    )


def _process_due_installment(
    persistence,
    engine,
    backend,
    config,
    *,
    installment,
) -> bool:
    if installment.state not in {"due", "partially_paid"}:
        return False
    if config.auto_pay_due_installments:
        remaining = _remaining_installment_amount(installment)
        if remaining > 0:
            post_payment(
                persistence,
                engine,
                installment_id_value=installment.id,
                payment_ordinal=1,
                amount=remaining,
            )
    else:
        schedule_overdue(
            persistence,
            engine,
            backend,
            installment_id_value=installment.id,
            delay=config.overdue_grace,
        )
    return True


def _process_overdue_installment(
    persistence,
    engine,
    backend,
    config,
    *,
    installment,
) -> bool:
    if installment.state != "overdue":
        return False
    reconcile_collection(
        persistence,
        engine,
        backend,
        installment_id_value=installment.id,
    )
    if not config.auto_pay_due_installments:
        return True
    remaining = _remaining_installment_amount(installment)
    if remaining > 0:
        post_payment(
            persistence,
            engine,
            installment_id_value=installment.id,
            payment_ordinal=1,
            amount=remaining,
        )
    cure_delinquency(
        persistence,
        engine,
        installment_id_value=installment.id,
    )
    return True


def _reconcile_application_state(
    persistence,
    engine,
    backend,
    config,
    *,
    entities,
    application,
):
    if application.state in {"submitted", "under_analysis"}:
        reconcile_underwriting(
            persistence,
            engine,
            backend,
            entities=entities,
            approve=config.approve_application,
        )
        return None, True

    if application.state == "rejected":
        return None, True

    loan = persistence.entity("loan", loan_id(application.id))
    if application.state == "approved" and (
        loan is None or loan.state in {"approved", "disbursed"}
    ):
        disburse_and_schedule(
            persistence,
            engine,
            backend,
            entities=entities,
            first_due_delay=config.first_due_delay,
            installment_interval=config.installment_interval,
        )
        return None, True
    return loan, False


def _reconcile_installments(
    persistence,
    engine,
    backend,
    config,
    *,
    loan,
) -> None:
    for ordinal in range(1, int(loan.attributes["installment_count"]) + 1):
        installment = persistence.entity(
            "loan_installment",
            installment_id(loan.id, ordinal),
        )
        if installment is None:
            continue
        if _process_due_installment(
            persistence,
            engine,
            backend,
            config,
            installment=installment,
        ):
            return
        if _process_overdue_installment(
            persistence,
            engine,
            backend,
            config,
            installment=installment,
        ):
            return


def _reconcile_tick(persistence, engine, backend, config, entities) -> None:
    application = _application_or_error(persistence, entities)
    loan, handled = _reconcile_application_state(
        persistence,
        engine,
        backend,
        config,
        entities=entities,
        application=application,
    )
    if handled:
        return

    if loan is None:
        return

    _reconcile_installments(
        persistence,
        engine,
        backend,
        config,
        loan=loan,
    )


definition = DomainDefinition(
    name="credit_loans",
    description="Credit and loans reference domain.",
    config_model=CreditLoansConfig,
    build_runtime=_build,
    seed=_seed,
    reconcile_tick=_reconcile_tick,
    runtime_mutable_fields=frozenset(["tick_step","random_seed","approve_application","auto_pay_due_installments","first_due_delay","installment_interval","overdue_grace"]),
)
